mod models;
mod resource_installer;
mod runtime;
mod storage;
mod workflow;

use models::{LogBundle, RuntimeStatus, WorkspaceProfile};
use resource_installer::managed_version;
use runtime::{
    prepare_runtime as prepare_runtime_environment, read_logs, shutdown, start_workspace,
    stop_all_workspaces, stop_workspace, DependencyStatus, RuntimeManager,
};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use storage::ProfileStore;
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{
    AppHandle, Manager, PhysicalPosition, Rect, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_dialog::{
    DialogExt, MessageDialogButtons, MessageDialogKind, MessageDialogResult,
};
use workflow::{
    decide_approval as decide_workflow_approval, read_snapshot as read_workflow_snapshot,
    WorkflowSnapshot,
};

const TRAY_ID: &str = "coding-tools-mcp";
const WORKSPACE_NAME_MAX_CHARS: usize = 22;

#[derive(Clone)]
struct DesktopState {
    store: Arc<Mutex<ProfileStore>>,
    runtime: Arc<Mutex<RuntimeManager>>,
}

#[derive(Serialize)]
struct DesktopSnapshot {
    language: String,
    profiles: Vec<WorkspaceProfile>,
    statuses: HashMap<String, RuntimeStatus>,
    dependencies: DependencyStatus,
    workflow: HashMap<String, WorkflowSnapshot>,
}

#[tauri::command]
fn desktop_snapshot(state: tauri::State<'_, DesktopState>) -> Result<DesktopSnapshot, String> {
    let (profiles, language) = {
        let store = state
            .store
            .lock()
            .map_err(|_| "Profile store is unavailable.")?;
        (store.profiles(), store.language().to_string())
    };
    let mut runtime = state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.")?;
    let statuses = profiles
        .iter()
        .map(|profile| (profile.id.clone(), runtime.status(profile)))
        .collect();
    let dependencies = runtime.dependency_status();
    let workflow_root = runtime.workflow_state_root();
    drop(runtime);
    let workflow = profiles
        .iter()
        .map(|profile| {
            let snapshot = read_workflow_snapshot(&workflow_root, Path::new(&profile.path))
                .unwrap_or_else(WorkflowSnapshot::failure);
            (profile.id.clone(), snapshot)
        })
        .collect();
    Ok(DesktopSnapshot {
        language,
        profiles,
        statuses,
        dependencies,
        workflow,
    })
}

#[tauri::command]
fn decide_approval(
    profile_id: String,
    approval_id: String,
    approved: bool,
    state: tauri::State<'_, DesktopState>,
) -> Result<(), String> {
    let profile = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?
        .get(&profile_id)
        .ok_or("Workspace profile was not found.")?;
    let workflow_root = state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.")?
        .workflow_state_root();
    decide_workflow_approval(
        &workflow_root,
        Path::new(&profile.path),
        &approval_id,
        approved,
    )
}

#[tauri::command]
fn create_profile(
    path: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<WorkspaceProfile, String> {
    let mut store = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?;
    let requested =
        std::fs::canonicalize(&path).unwrap_or_else(|_| std::path::PathBuf::from(&path));
    if let Some(existing) = store.profiles().into_iter().find(|profile| {
        std::fs::canonicalize(&profile.path)
            .unwrap_or_else(|_| std::path::PathBuf::from(&profile.path))
            == requested
    }) {
        return Ok(existing);
    }
    let profile = WorkspaceProfile::new(path, store.next_port())?;
    store.insert(profile)
}

#[tauri::command]
fn create_full_access_profile(
    path: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<WorkspaceProfile, String> {
    let mut store = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?;
    create_full_access_profile_in_store(&mut store, path)
}

fn create_full_access_profile_in_store(
    store: &mut ProfileStore,
    path: String,
) -> Result<WorkspaceProfile, String> {
    let requested =
        std::fs::canonicalize(&path).unwrap_or_else(|_| std::path::PathBuf::from(&path));
    if let Some(existing) = store.profiles().into_iter().find(|profile| {
        std::fs::canonicalize(&profile.path)
            .unwrap_or_else(|_| std::path::PathBuf::from(&profile.path))
            == requested
    }) {
        return if existing.runtime.permission_mode == "host" {
            Ok(existing)
        } else {
            Err("This workspace already exists. Change its access mode to Full Access in Workspace settings.".into())
        };
    }
    let profile = WorkspaceProfile::new_full_access(path, store.next_port())?;
    store.insert(profile)
}

#[tauri::command]
fn save_profile(
    mut profile: WorkspaceProfile,
    state: tauri::State<'_, DesktopState>,
) -> Result<WorkspaceProfile, String> {
    normalize_allowed_paths(&mut profile)?;
    if state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.")?
        .status(&profile)
        .is_active()
    {
        return Err("Stop the workspace before changing its configuration.".into());
    }
    state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?
        .update(profile)
}

fn normalize_allowed_paths(profile: &mut WorkspaceProfile) -> Result<(), String> {
    let workspace = std::fs::canonicalize(&profile.path).map_err(|error| error.to_string())?;
    let mut seen = HashSet::<PathBuf>::new();
    let mut normalized = Vec::<PathBuf>::new();
    for raw in std::mem::take(&mut profile.runtime.allowed_paths) {
        let path = std::fs::canonicalize(&raw)
            .map_err(|error| format!("Could not resolve allowed folder {raw}: {error}"))?;
        if path.starts_with(&workspace)
            || !seen.insert(path.clone())
            || normalized.iter().any(|root| path.starts_with(root))
        {
            continue;
        }
        normalized.retain(|root| !root.starts_with(&path));
        normalized.push(path);
    }
    profile.runtime.allowed_paths = normalized
        .into_iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect();
    Ok(())
}

#[tauri::command]
fn delete_profile(profile_id: String, state: tauri::State<'_, DesktopState>) -> Result<(), String> {
    let profile = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?
        .get(&profile_id)
        .ok_or("Workspace profile was not found.")?;
    if state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.")?
        .status(&profile)
        .is_active()
    {
        return Err("Stop the workspace before deleting it.".into());
    }
    state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?
        .remove(&profile_id)
}

#[tauri::command]
async fn start_profile(
    app: AppHandle,
    profile_id: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<RuntimeStatus, String> {
    schedule_tray_refresh(app, Duration::from_secs(1));
    let store = Arc::clone(&state.store);
    let runtime = Arc::clone(&state.runtime);
    tauri::async_runtime::spawn_blocking(move || {
        let (profile, log_dir) = {
            let mut store = store.lock().map_err(|_| "Profile store is unavailable.")?;
            let profile = store.prepare_for_start(&profile_id)?;
            let log_dir = store.log_dir(&profile_id)?;
            (profile, log_dir)
        };
        start_workspace(&runtime, &profile, &log_dir)
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
async fn stop_profile(
    profile_id: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<RuntimeStatus, String> {
    let store = Arc::clone(&state.store);
    let runtime = Arc::clone(&state.runtime);
    tauri::async_runtime::spawn_blocking(move || {
        let profile = store
            .lock()
            .map_err(|_| "Profile store is unavailable.")?
            .get(&profile_id)
            .ok_or("Workspace profile was not found.")?;
        stop_workspace(&runtime, &profile)
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
async fn stop_all_profiles(
    state: tauri::State<'_, DesktopState>,
) -> Result<HashMap<String, RuntimeStatus>, String> {
    let profiles = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?
        .profiles();
    let runtime = Arc::clone(&state.runtime);
    tauri::async_runtime::spawn_blocking(move || {
        stop_all_workspaces(&runtime)?;
        let mut runtime = runtime
            .lock()
            .map_err(|_| "Runtime manager is unavailable.")?;
        Ok(profiles
            .iter()
            .map(|profile| (profile.id.clone(), runtime.status(profile)))
            .collect())
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
fn profile_status(
    profile_id: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<RuntimeStatus, String> {
    let profile = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?
        .get(&profile_id)
        .ok_or("Workspace profile was not found.")?;
    Ok(state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.")?
        .status(&profile))
}

#[tauri::command]
fn profile_logs(
    profile_id: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<LogBundle, String> {
    let directory = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?
        .log_dir(&profile_id)?;
    read_logs(&directory)
}

#[tauri::command]
fn open_logs(app: AppHandle, profile_id: String) -> Result<(), String> {
    open_logs_folder(&app, &profile_id)
}

#[tauri::command]
fn set_language(
    app: AppHandle,
    language: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<(), String> {
    if !matches!(language.as_str(), "en" | "zh-CN") {
        return Err("Unknown desktop language.".into());
    }
    state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .set_language(&language)?;
    refresh_tray_menu(&app)
}

#[tauri::command]
async fn pick_workspace_folder(
    app: AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<Option<String>, String> {
    let language = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .language()
        .to_string();
    app.dialog()
        .file()
        .set_title(menu_text(
            &language,
            "Choose workspace folder",
            "选择工作区文件夹",
        ))
        .blocking_pick_folder()
        .map(|folder| {
            folder
                .into_path()
                .map(|path| path.to_string_lossy().to_string())
                .map_err(|error| error.to_string())
        })
        .transpose()
}

#[tauri::command]
async fn pick_allowed_folder(
    app: AppHandle,
    state: tauri::State<'_, DesktopState>,
) -> Result<Option<String>, String> {
    let language = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .language()
        .to_string();
    app.dialog()
        .file()
        .set_title(menu_text(
            &language,
            "Choose allowed folder",
            "选择允许访问的文件夹",
        ))
        .blocking_pick_folder()
        .map(|folder| {
            folder
                .into_path()
                .map(|path| path.to_string_lossy().to_string())
                .map_err(|error| error.to_string())
        })
        .transpose()
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    app.exit(0);
}

#[tauri::command]
fn open_resource(target: String) -> Result<(), String> {
    let url = match target.as_str() {
        "uv" => "https://docs.astral.sh/uv/getting-started/installation/",
        "cloudflared" => "https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/",
        "github" => "https://github.com/xyTom/coding-tools-mcp",
        _ => return Err("Unknown external resource.".into()),
    };
    launch_external_url(url)
}

#[tauri::command]
async fn install_resource(app: AppHandle, target: String) -> Result<String, String> {
    if !matches!(target.as_str(), "uv" | "cloudflared") {
        return Err("Only uv and cloudflared are managed by the desktop installer.".into());
    }
    let data_dir = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
    let target_for_result = target.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let cancelled = std::sync::atomic::AtomicBool::new(false);
        let path = resource_installer::install(&target, &data_dir, &cancelled)?;
        let version = managed_version(&target).unwrap_or("managed");
        Ok::<String, String>(format!(
            "Installed {target_for_result} {version} at {}",
            path.display()
        ))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn repair_dependencies(app: AppHandle) -> Result<String, String> {
    let data_dir = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
    tauri::async_runtime::spawn_blocking(move || {
        let cancelled = std::sync::atomic::AtomicBool::new(false);
        resource_installer::install("uv", &data_dir, &cancelled)?;
        resource_installer::install("cloudflared", &data_dir, &cancelled)?;
        Ok::<String, String>("Managed dependencies repaired.".into())
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn prepare_runtime(
    repair: bool,
    state: tauri::State<'_, DesktopState>,
) -> Result<String, String> {
    let runtime = Arc::clone(&state.runtime);
    tauri::async_runtime::spawn_blocking(move || prepare_runtime_environment(&runtime, repair))
        .await
        .map_err(|e| e.to_string())?
}

#[cfg(target_os = "macos")]
fn launch_external_url(url: &str) -> Result<(), String> {
    Command::new("open")
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open the link: {error}"))
}

#[cfg(target_os = "windows")]
fn launch_external_url(url: &str) -> Result<(), String> {
    Command::new("explorer.exe")
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open the link: {error}"))
}

#[cfg(all(unix, not(target_os = "macos")))]
fn launch_external_url(url: &str) -> Result<(), String> {
    Command::new("xdg-open")
        .arg(url)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open the link: {error}"))
}

#[cfg(target_os = "macos")]
fn open_path(path: &Path) -> Result<(), String> {
    Command::new("open")
        .arg(path)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open the folder: {error}"))
}

#[cfg(target_os = "windows")]
fn open_path(path: &Path) -> Result<(), String> {
    Command::new("explorer.exe")
        .arg(path)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open the folder: {error}"))
}

#[cfg(all(unix, not(target_os = "macos")))]
fn open_path(path: &Path) -> Result<(), String> {
    Command::new("xdg-open")
        .arg(path)
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not open the folder: {error}"))
}

fn draw_stroke(rgba: &mut [u8], size: u32, from: (f32, f32), to: (f32, f32), thickness: f32) {
    let radius = thickness / 2.0;
    let min_x = (from.0.min(to.0) - radius).floor().max(0.0) as u32;
    let max_x = (from.0.max(to.0) + radius)
        .ceil()
        .min(size.saturating_sub(1) as f32) as u32;
    let min_y = (from.1.min(to.1) - radius).floor().max(0.0) as u32;
    let max_y = (from.1.max(to.1) + radius)
        .ceil()
        .min(size.saturating_sub(1) as f32) as u32;
    let delta_x = to.0 - from.0;
    let delta_y = to.1 - from.1;
    let length_squared = delta_x * delta_x + delta_y * delta_y;

    for y in min_y..=max_y {
        for x in min_x..=max_x {
            let pixel_x = x as f32 + 0.5;
            let pixel_y = y as f32 + 0.5;
            let projection = if length_squared == 0.0 {
                0.0
            } else {
                (((pixel_x - from.0) * delta_x + (pixel_y - from.1) * delta_y) / length_squared)
                    .clamp(0.0, 1.0)
            };
            let closest_x = from.0 + projection * delta_x;
            let closest_y = from.1 + projection * delta_y;
            let distance_x = pixel_x - closest_x;
            let distance_y = pixel_y - closest_y;
            if distance_x * distance_x + distance_y * distance_y <= radius * radius {
                let offset = ((y * size + x) * 4) as usize;
                rgba[offset..offset + 4].copy_from_slice(&[0, 0, 0, 255]);
            }
        }
    }
}

fn draw_polyline(rgba: &mut [u8], size: u32, points: &[(f32, f32)], thickness: f32) {
    for pair in points.windows(2) {
        draw_stroke(rgba, size, pair[0], pair[1], thickness);
    }
}

fn tray_template_icon() -> Image<'static> {
    const SIZE: u32 = 32;
    const STROKE: f32 = 3.6;
    let mut rgba = vec![0; (SIZE * SIZE * 4) as usize];
    let cloud = [
        (11.0, 20.0),
        (7.0, 20.0),
        (4.5, 18.0),
        (4.5, 15.0),
        (6.5, 12.0),
        (9.5, 11.0),
        (11.5, 11.0),
        (12.5, 8.0),
        (15.5, 6.0),
        (19.0, 6.0),
        (22.0, 8.0),
        (23.0, 11.0),
        (26.0, 12.0),
        (28.0, 15.0),
        (28.0, 18.0),
        (25.5, 20.0),
        (21.0, 20.0),
    ];
    draw_polyline(&mut rgba, SIZE, &cloud, STROKE);
    draw_polyline(
        &mut rgba,
        SIZE,
        &[
            (12.0, 19.0),
            (12.0, 22.0),
            (14.0, 24.0),
            (18.0, 24.0),
            (20.0, 22.0),
            (20.0, 19.0),
        ],
        STROKE,
    );
    draw_stroke(&mut rgba, SIZE, (12.0, 19.0), (20.0, 19.0), STROKE);
    draw_stroke(&mut rgba, SIZE, (14.0, 15.5), (14.0, 19.0), STROKE);
    draw_stroke(&mut rgba, SIZE, (18.0, 15.5), (18.0, 19.0), STROKE);
    draw_stroke(&mut rgba, SIZE, (16.0, 24.0), (16.0, 28.0), STROKE);
    Image::new_owned(rgba, SIZE, SIZE)
}

fn running_indicator_icon() -> Image<'static> {
    const SIZE: u32 = 16;
    let mut rgba = vec![0; (SIZE * SIZE * 4) as usize];
    let center = (SIZE as f32 - 1.0) / 2.0;
    let radius = 4.0_f32;
    for y in 0..SIZE {
        for x in 0..SIZE {
            let dx = x as f32 - center;
            let dy = y as f32 - center;
            if dx * dx + dy * dy <= radius * radius {
                let offset = ((y * SIZE + x) * 4) as usize;
                rgba[offset..offset + 4].copy_from_slice(&[52, 199, 89, 255]);
            }
        }
    }
    Image::new_owned(rgba, SIZE, SIZE)
}

fn quick_tunnel_profile(mut profile: WorkspaceProfile) -> WorkspaceProfile {
    profile.tunnel.r#type = "cloudflare".into();
    profile.tunnel.cloudflare_mode = "quick".into();
    profile.tunnel.domain.clear();
    profile.tunnel.public_url.clear();
    profile.tunnel.frp_server.clear();
    profile.tunnel.frp_subdomain.clear();
    profile.tunnel.cloudflare_token.clear();
    profile.auth.r#type = "oauth".into();
    profile
}

fn public_endpoint(profile: &WorkspaceProfile, resolved_url: &str) -> String {
    let mut base = resolved_url.trim().to_string();
    if base.is_empty()
        && profile.tunnel.r#type == "cloudflare"
        && profile.tunnel.cloudflare_mode == "named"
    {
        base = profile.tunnel.public_url.trim().to_string();
    }
    if base.is_empty()
        && profile.tunnel.r#type == "frp"
        && !profile.tunnel.frp_server.trim().is_empty()
        && !profile.tunnel.frp_subdomain.trim().is_empty()
    {
        base = format!(
            "https://{}.{}",
            profile.tunnel.frp_subdomain.trim(),
            profile.tunnel.frp_server.trim()
        );
    }
    if base.is_empty() {
        String::new()
    } else {
        format!("{}/mcp", base.trim_end_matches('/'))
    }
}

fn menu_error(error: impl ToString) -> String {
    error.to_string()
}

fn menu_text<'a>(language: &str, english: &'a str, chinese: &'a str) -> &'a str {
    if language == "zh-CN" {
        chinese
    } else {
        english
    }
}

fn status_text(status: &RuntimeStatus, language: &str) -> &'static str {
    if status.state == "starting" {
        menu_text(language, "Preparing runtime", "正在准备运行时")
    } else if status.pid.is_none() {
        menu_text(language, "Stopped", "已停止")
    } else if status.state == "running" {
        menu_text(language, "Running", "运行中")
    } else {
        menu_text(language, "Starting / connection issue", "启动中 / 连接异常")
    }
}

fn permission_mode_label(mode: &str, language: &str) -> &'static str {
    match mode {
        "safe" => menu_text(language, "Safe", "安全"),
        "trusted" => menu_text(language, "Trusted", "受信任"),
        "dangerous" => menu_text(language, "Dangerous", "危险"),
        "host" => menu_text(language, "Host · full access", "主机 · 完全访问"),
        _ => menu_text(language, "Unknown", "未知"),
    }
}

fn desktop_access_label(mode: &str, language: &str) -> String {
    match mode {
        "trusted" => menu_text(language, "Standard", "标准").to_string(),
        "host" => menu_text(language, "Full Access", "完全访问").to_string(),
        legacy => format!(
            "{} · {}",
            menu_text(language, "Legacy", "旧模式"),
            permission_mode_label(legacy, language)
        ),
    }
}

fn readiness_label(ready: bool, language: &str) -> &'static str {
    if ready {
        menu_text(language, "Ready", "就绪")
    } else {
        menu_text(language, "Setup needed", "需要设置")
    }
}

fn truncate_workspace_name(name: &str) -> String {
    let count = name.chars().count();
    if count <= WORKSPACE_NAME_MAX_CHARS {
        return name.to_string();
    }
    let keep = WORKSPACE_NAME_MAX_CHARS.saturating_sub(1);
    let mut truncated = name.chars().take(keep).collect::<String>();
    truncated.push('…');
    truncated
}

fn build_tray_menu(app: &AppHandle) -> Result<Menu<tauri::Wry>, String> {
    let state = app.state::<DesktopState>();
    let (profiles, language) = {
        let store = state
            .store
            .lock()
            .map_err(|_| "Profile store is unavailable.".to_string())?;
        (store.profiles(), store.language().to_string())
    };
    let (statuses, dependencies, workflow_root) = {
        let mut runtime = state
            .runtime
            .lock()
            .map_err(|_| "Runtime manager is unavailable.".to_string())?;
        let statuses = profiles
            .iter()
            .map(|profile| (profile.id.clone(), runtime.status(profile)))
            .collect::<HashMap<_, _>>();
        let dependencies = runtime.dependency_status();
        let workflow_root = runtime.workflow_state_root();
        (statuses, dependencies, workflow_root)
    };

    let menu = Menu::new(app).map_err(menu_error)?;
    let title = MenuItem::with_id(
        app,
        "menu:title",
        format!("Coding Tools MCP · {}", env!("CARGO_PKG_VERSION")),
        false,
        None::<&str>,
    )
    .map_err(menu_error)?;
    menu.append(&title).map_err(menu_error)?;
    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    menu.append(&separator).map_err(menu_error)?;

    if profiles.is_empty() {
        let empty = MenuItem::with_id(
            app,
            "menu:empty",
            menu_text(&language, "No workspaces yet", "尚无工作区"),
            false,
            None::<&str>,
        )
        .map_err(menu_error)?;
        menu.append(&empty).map_err(menu_error)?;
    }

    if !profiles.is_empty() {
        let workspaces_title = MenuItem::with_id(
            app,
            "menu:workspaces",
            menu_text(&language, "Workspaces", "工作区"),
            false,
            None::<&str>,
        )
        .map_err(menu_error)?;
        menu.append(&workspaces_title).map_err(menu_error)?;
    }

    for profile in &profiles {
        let status = statuses
            .get(&profile.id)
            .cloned()
            .unwrap_or_else(|| RuntimeStatus::stopped(profile.runtime.local_port));
        let running = status.is_active();
        let workspace = Submenu::with_id_and_icon(
            app,
            format!("workspace:{}", profile.id),
            format!(
                "{} · {}",
                truncate_workspace_name(&profile.name),
                status_text(&status, &language)
            ),
            true,
            running.then(running_indicator_icon),
        )
        .map_err(menu_error)?;

        let status_item = MenuItem::with_id(
            app,
            format!("status:{}", profile.id),
            format!(
                "{} · {}",
                menu_text(&language, "Status", "状态"),
                status_text(&status, &language)
            ),
            false,
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&status_item).map_err(menu_error)?;

        let path_item = MenuItem::with_id(
            app,
            format!("path:{}", profile.id),
            format!(
                "{} · {}",
                menu_text(&language, "Folder", "文件夹"),
                profile.path
            ),
            false,
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&path_item).map_err(menu_error)?;
        let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
        workspace.append(&separator).map_err(menu_error)?;

        let access_menu = Submenu::with_id(
            app,
            format!("access-menu:{}", profile.id),
            format!(
                "{} · {}",
                menu_text(&language, "Access", "访问权限"),
                desktop_access_label(&profile.runtime.permission_mode, &language)
            ),
            true,
        )
        .map_err(menu_error)?;
        if !matches!(profile.runtime.permission_mode.as_str(), "trusted" | "host") {
            let legacy = MenuItem::with_id(
                app,
                format!("access-legacy:{}", profile.id),
                desktop_access_label(&profile.runtime.permission_mode, &language),
                false,
                None::<&str>,
            )
            .map_err(menu_error)?;
            access_menu.append(&legacy).map_err(menu_error)?;
            let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
            access_menu.append(&separator).map_err(menu_error)?;
        }
        let standard = CheckMenuItem::with_id(
            app,
            format!("access:standard:{}", profile.id),
            menu_text(&language, "Standard", "标准"),
            !running,
            profile.runtime.permission_mode == "trusted",
            None::<&str>,
        )
        .map_err(menu_error)?;
        access_menu.append(&standard).map_err(menu_error)?;
        let full_access = CheckMenuItem::with_id(
            app,
            format!("access:full:{}", profile.id),
            menu_text(&language, "Full Access", "完全访问"),
            !running,
            profile.runtime.permission_mode == "host",
            None::<&str>,
        )
        .map_err(menu_error)?;
        access_menu.append(&full_access).map_err(menu_error)?;
        workspace.append(&access_menu).map_err(menu_error)?;

        let workflow_snapshot = read_workflow_snapshot(&workflow_root, Path::new(&profile.path))
            .unwrap_or_else(WorkflowSnapshot::failure);
        let pending_approvals = workflow_snapshot
            .approvals
            .iter()
            .filter(|approval| approval.status == "pending")
            .collect::<Vec<_>>();
        if !pending_approvals.is_empty() {
            let approvals_menu = Submenu::with_id(
                app,
                format!("approvals-menu:{}", profile.id),
                format!(
                    "⚠ {} · {}",
                    menu_text(&language, "Approval required", "需要审批"),
                    pending_approvals.len()
                ),
                true,
            )
            .map_err(menu_error)?;
            for approval in &pending_approvals {
                let approval_menu = Submenu::with_id(
                    app,
                    format!("approval-menu:{}:{}", profile.id, approval.approval_id),
                    format!("{} · {}", approval.tool_name, approval.permission),
                    true,
                )
                .map_err(menu_error)?;
                let reason = MenuItem::with_id(
                    app,
                    format!("approval-reason:{}:{}", profile.id, approval.approval_id),
                    approval.reason.chars().take(80).collect::<String>(),
                    false,
                    None::<&str>,
                )
                .map_err(menu_error)?;
                approval_menu.append(&reason).map_err(menu_error)?;
                let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
                approval_menu.append(&separator).map_err(menu_error)?;
                let approve = MenuItem::with_id(
                    app,
                    format!("approval-approve:{}:{}", profile.id, approval.approval_id),
                    menu_text(&language, "Approve", "批准"),
                    true,
                    None::<&str>,
                )
                .map_err(menu_error)?;
                approval_menu.append(&approve).map_err(menu_error)?;
                let deny = MenuItem::with_id(
                    app,
                    format!("approval-deny:{}:{}", profile.id, approval.approval_id),
                    menu_text(&language, "Deny", "拒绝"),
                    true,
                    None::<&str>,
                )
                .map_err(menu_error)?;
                approval_menu.append(&deny).map_err(menu_error)?;
                approvals_menu.append(&approval_menu).map_err(menu_error)?;
            }
            workspace.append(&approvals_menu).map_err(menu_error)?;
        }

        let open_logs = MenuItem::with_id(
            app,
            format!("logs-open:{}", profile.id),
            menu_text(&language, "Open logs…", "打开日志…"),
            true,
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&open_logs).map_err(menu_error)?;

        let power_item = MenuItem::with_id(
            app,
            format!("{}:{}", if running { "stop" } else { "start" }, profile.id),
            if status.state == "starting" {
                menu_text(&language, "Cancel startup", "取消启动")
            } else if running {
                menu_text(&language, "Stop workspace", "停止工作区")
            } else {
                menu_text(&language, "Start workspace", "启动工作区")
            },
            true,
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&power_item).map_err(menu_error)?;

        let endpoint = if running {
            public_endpoint(profile, &status.public_url)
        } else {
            String::new()
        };
        let copy_url = MenuItem::with_id(
            app,
            format!("copy-url:{}", profile.id),
            menu_text(&language, "Server URL · Copy", "服务器 URL · 复制"),
            !endpoint.is_empty(),
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&copy_url).map_err(menu_error)?;
        let copy_passcode = MenuItem::with_id(
            app,
            format!("copy-passcode:{}", profile.id),
            menu_text(
                &language,
                "Authorization passcode · Copy",
                "授权口令 · 复制",
            ),
            !profile.auth.oauth_password.is_empty(),
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&copy_passcode).map_err(menu_error)?;

        let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
        workspace.append(&separator).map_err(menu_error)?;
        let remove = MenuItem::with_id(
            app,
            format!("remove:{}", profile.id),
            menu_text(&language, "Remove workspace…", "移除工作区…"),
            !running,
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&remove).map_err(menu_error)?;
        menu.append(&workspace).map_err(menu_error)?;
    }

    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    menu.append(&separator).map_err(menu_error)?;
    let add = MenuItem::with_id(
        app,
        "add-workspace",
        menu_text(&language, "Add workspace…", "添加工作区…"),
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    menu.append(&add).map_err(menu_error)?;
    let refresh = MenuItem::with_id(
        app,
        "refresh",
        menu_text(&language, "Refresh", "刷新"),
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    menu.append(&refresh).map_err(menu_error)?;

    let resources = Submenu::with_id(
        app,
        "resources",
        menu_text(&language, "Resources", "资源"),
        true,
    )
    .map_err(menu_error)?;
    let runtime_ready = dependencies.runtime_ready && dependencies.cloudflared;
    let runtime_summary = MenuItem::with_id(
        app,
        "resource-summary:runtime",
        format!(
            "{} · {}",
            menu_text(&language, "Runtime", "运行环境"),
            readiness_label(runtime_ready, &language)
        ),
        false,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&runtime_summary).map_err(menu_error)?;
    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    resources.append(&separator).map_err(menu_error)?;
    let runtime_action_id = if dependencies.runtime_ready {
        "resource:repair-runtime"
    } else {
        "resource:prepare-runtime"
    };
    let runtime_action_label = if dependencies.runtime_ready {
        menu_text(&language, "Repair runtime…", "修复运行时…")
    } else {
        menu_text(&language, "Prepare runtime…", "准备运行时…")
    };
    let runtime_action = MenuItem::with_id(
        app,
        runtime_action_id,
        runtime_action_label,
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&runtime_action).map_err(menu_error)?;

    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    resources.append(&separator).map_err(menu_error)?;
    let diagnostics = Submenu::with_id(
        app,
        "resource:diagnostics-menu",
        menu_text(&language, "Diagnostics", "诊断"),
        true,
    )
    .map_err(menu_error)?;
    for (id, label) in [
        (
            "resource-status:runtime",
            format!(
                "MCP Runtime · {}{}",
                if dependencies.runtime_ready {
                    menu_text(&language, "Ready", "就绪")
                } else {
                    menu_text(&language, "Not prepared", "未准备")
                },
                dependencies
                    .runtime_version
                    .as_deref()
                    .map(|version| format!(" · {version}"))
                    .unwrap_or_default()
            ),
        ),
        (
            "resource-status:uv",
            format!("uv · {}", readiness_label(dependencies.uv, &language)),
        ),
        (
            "resource-status:cloudflared",
            format!(
                "cloudflared · {}",
                readiness_label(dependencies.cloudflared, &language)
            ),
        ),
    ] {
        let item = MenuItem::with_id(app, id, label, false, None::<&str>).map_err(menu_error)?;
        diagnostics.append(&item).map_err(menu_error)?;
    }
    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    diagnostics.append(&separator).map_err(menu_error)?;
    let repair_dependencies = MenuItem::with_id(
        app,
        "resource:repair-dependencies",
        menu_text(
            &language,
            "Repair uv & cloudflared…",
            "修复 uv 与 cloudflared…",
        ),
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    diagnostics
        .append(&repair_dependencies)
        .map_err(menu_error)?;
    let uv = MenuItem::with_id(
        app,
        "resource:uv",
        menu_text(&language, "Install / Repair uv…", "安装 / 修复 uv…"),
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    diagnostics.append(&uv).map_err(menu_error)?;
    let cloudflared = MenuItem::with_id(
        app,
        "resource:cloudflared",
        menu_text(
            &language,
            "Install / Repair cloudflared…",
            "安装 / 修复 cloudflared…",
        ),
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    diagnostics.append(&cloudflared).map_err(menu_error)?;
    resources.append(&diagnostics).map_err(menu_error)?;
    let github = MenuItem::with_id(
        app,
        "resource:github",
        menu_text(&language, "Source on GitHub…", "GitHub 源代码…"),
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&github).map_err(menu_error)?;
    menu.append(&resources).map_err(menu_error)?;

    let language_menu = Submenu::with_id(
        app,
        "language-menu",
        menu_text(&language, "Language", "语言"),
        true,
    )
    .map_err(menu_error)?;
    for (code, label) in [("en", "English"), ("zh-CN", "简体中文")] {
        let item = CheckMenuItem::with_id(
            app,
            format!("language:{code}"),
            label,
            true,
            language == code,
            None::<&str>,
        )
        .map_err(menu_error)?;
        language_menu.append(&item).map_err(menu_error)?;
    }
    menu.append(&language_menu).map_err(menu_error)?;

    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    menu.append(&separator).map_err(menu_error)?;
    let quit = MenuItem::with_id(
        app,
        "quit",
        menu_text(&language, "Quit Coding Tools MCP", "退出 Coding Tools MCP"),
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    menu.append(&quit).map_err(menu_error)?;
    Ok(menu)
}

fn refresh_tray_menu(app: &AppHandle) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        let menu = build_tray_menu(app)?;
        let tray = app
            .tray_by_id(TRAY_ID)
            .ok_or_else(|| "Tray icon is unavailable.".to_string())?;
        return tray.set_menu(Some(menu)).map_err(menu_error);
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = app;
        Ok(())
    }
}

fn refresh_tray_menu_on_main(app: AppHandle) {
    let callback_app = app.clone();
    let _ = app.run_on_main_thread(move || {
        if let Err(error) = refresh_tray_menu(&callback_app) {
            show_error(&callback_app, error);
        }
    });
}

fn schedule_tray_refresh(app: AppHandle, delay: Duration) {
    std::thread::spawn(move || {
        std::thread::sleep(delay);
        refresh_tray_menu_on_main(app);
    });
}

fn show_error(app: &AppHandle, message: impl Into<String>) {
    app.dialog()
        .message(message.into())
        .kind(MessageDialogKind::Error)
        .show(|_| {});
}

fn add_workspace_from_menu(app: &AppHandle) {
    let language = app
        .state::<DesktopState>()
        .store
        .lock()
        .map(|store| store.language().to_string())
        .unwrap_or_else(|_| "en".to_string());
    let standard_label = menu_text(&language, "Standard", "标准");
    let full_label = menu_text(&language, "Full Access", "完全访问");
    let cancel_label = menu_text(&language, "Cancel", "取消");
    let dialog_app = app.clone();
    let callback_app = app.clone();
    app.dialog()
        .message(menu_text(
            &language,
            "Choose access before creating a profile. Both modes ask you to select the workspace they use as project context.",
            "请先选择访问模式。两种模式都会请你选择用作项目上下文的工作区。",
        ))
        .buttons(MessageDialogButtons::YesNoCancelCustom(
            standard_label.into(),
            full_label.into(),
            cancel_label.into(),
        ))
        .show_with_result(move |choice| match choice {
            MessageDialogResult::Custom(selected) if selected == standard_label => {
                pick_workspace_from_menu(&dialog_app, &language, false);
            }
            MessageDialogResult::Custom(selected) if selected == full_label => {
                pick_workspace_from_menu(&callback_app, &language, true);
            }
            _ => {}
        });
}

fn pick_workspace_from_menu(app: &AppHandle, language: &str, full_access: bool) {
    let callback_app = app.clone();
    app.dialog()
        .file()
        .set_title(menu_text(
            language,
            "Choose workspace folder",
            "选择工作区文件夹",
        ))
        .pick_folder(move |folder| {
            let Some(folder) = folder else {
                return;
            };
            let path = match folder.into_path() {
                Ok(path) => path.to_string_lossy().to_string(),
                Err(error) => {
                    show_error(&callback_app, error.to_string());
                    return;
                }
            };
            let result = (|| {
                let state = callback_app.state::<DesktopState>();
                let mut store = state
                    .store
                    .lock()
                    .map_err(|_| "Profile store is unavailable.".to_string())?;
                if full_access {
                    create_full_access_profile_in_store(&mut store, path)?;
                } else {
                    let profile = WorkspaceProfile::new(path, store.next_port())?;
                    store.insert(profile)?;
                }
                drop(store);
                Ok::<(), String>(())
            })();
            if let Err(error) = result {
                show_error(&callback_app, error);
                return;
            }
            refresh_tray_menu_on_main(callback_app);
        });
}

fn start_workspace_from_menu(app: AppHandle, profile_id: String) {
    schedule_tray_refresh(app.clone(), Duration::from_secs(1));
    tauri::async_runtime::spawn_blocking(move || {
        let result = (|| {
            let state = app.state::<DesktopState>();
            let (profile, log_dir) = {
                let mut store = state
                    .store
                    .lock()
                    .map_err(|_| "Profile store is unavailable.".to_string())?;
                let profile = store
                    .get(&profile_id)
                    .ok_or_else(|| "Workspace profile was not found.".to_string())?;
                let profile = store.update(quick_tunnel_profile(profile))?;
                let profile = store.prepare_for_start(&profile.id)?;
                let log_dir = store.log_dir(&profile.id)?;
                (profile, log_dir)
            };
            start_workspace(&state.runtime, &profile, &log_dir)?;
            Ok::<(), String>(())
        })();
        if let Err(error) = result {
            if error != "Workspace startup was cancelled." {
                show_error(&app, error);
            }
        }
        refresh_tray_menu_on_main(app.clone());
        schedule_tray_refresh(app.clone(), Duration::from_secs(2));
        schedule_tray_refresh(app, Duration::from_secs(5));
    });
}

fn stop_workspace_from_menu(app: AppHandle, profile_id: String) {
    tauri::async_runtime::spawn_blocking(move || {
        let result = (|| {
            let state = app.state::<DesktopState>();
            let profile = state
                .store
                .lock()
                .map_err(|_| "Profile store is unavailable.".to_string())?
                .get(&profile_id)
                .ok_or_else(|| "Workspace profile was not found.".to_string())?;
            stop_workspace(&state.runtime, &profile)?;
            Ok::<(), String>(())
        })();
        if let Err(error) = result {
            show_error(&app, error);
        }
        refresh_tray_menu_on_main(app);
    });
}

fn set_permission_mode_from_menu(
    app: &AppHandle,
    profile_id: &str,
    permission_mode: &str,
) -> Result<(), String> {
    if !matches!(permission_mode, "safe" | "trusted" | "dangerous" | "host") {
        return Err("Unknown permission mode.".into());
    }
    let state = app.state::<DesktopState>();
    let mut profile = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .get(profile_id)
        .ok_or_else(|| "Workspace profile was not found.".to_string())?;
    if state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.".to_string())?
        .status(&profile)
        .is_active()
    {
        return Err("Stop the workspace before changing its permission mode.".into());
    }
    profile.runtime.permission_mode = permission_mode.into();
    state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .update(profile)?;
    Ok(())
}

fn copy_server_url(app: &AppHandle, profile_id: &str) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let profile = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .get(profile_id)
        .ok_or_else(|| "Workspace profile was not found.".to_string())?;
    let status = state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.".to_string())?
        .status(&profile);
    let endpoint = public_endpoint(&profile, &status.public_url);
    if endpoint.is_empty() {
        return Err(
            "Server URL is not ready yet. Start the workspace or wait for Cloudflare to connect."
                .into(),
        );
    }
    app.clipboard().write_text(endpoint).map_err(menu_error)?;
    Ok(())
}

fn copy_passcode(app: &AppHandle, profile_id: &str) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let profile = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .get(profile_id)
        .ok_or_else(|| "Workspace profile was not found.".to_string())?;
    if profile.auth.oauth_password.is_empty() {
        return Err("Authorization passcode is unavailable.".into());
    }
    app.clipboard()
        .write_text(profile.auth.oauth_password)
        .map_err(menu_error)?;
    Ok(())
}

fn open_logs_folder(app: &AppHandle, profile_id: &str) -> Result<(), String> {
    let directory = app
        .state::<DesktopState>()
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .log_dir(profile_id)?;
    open_path(&directory)
}

fn copy_recent_logs(app: &AppHandle, profile_id: &str) -> Result<(), String> {
    let directory = app
        .state::<DesktopState>()
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .log_dir(profile_id)?;
    let logs = read_logs(&directory)?;
    let text = format!(
        "=== stdout ===\n{}\n\n=== stderr ===\n{}\n\n=== cloudflared ===\n{}",
        logs.stdout, logs.stderr, logs.cloudflared
    );
    app.clipboard().write_text(text).map_err(menu_error)
}

fn decide_approval_from_menu(
    app: &AppHandle,
    profile_id: &str,
    approval_id: &str,
    approved: bool,
) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let profile = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .get(profile_id)
        .ok_or_else(|| "Workspace profile was not found.".to_string())?;
    let workflow_root = state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.".to_string())?
        .workflow_state_root();
    decide_workflow_approval(
        &workflow_root,
        Path::new(&profile.path),
        approval_id,
        approved,
    )
}

fn add_managed_worktree_from_menu(
    app: &AppHandle,
    profile_id: &str,
    worktree_id: &str,
) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    let profile = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .get(profile_id)
        .ok_or_else(|| "Workspace profile was not found.".to_string())?;
    let workflow_root = state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.".to_string())?
        .workflow_state_root();
    let snapshot = read_workflow_snapshot(&workflow_root, Path::new(&profile.path))?;
    let worktree = snapshot
        .worktrees
        .into_iter()
        .find(|worktree| worktree.worktree_id == worktree_id)
        .ok_or_else(|| "Managed worktree was not found.".to_string())?;
    let mut store = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?;
    if store
        .profiles()
        .iter()
        .any(|existing| existing.path == worktree.path)
    {
        return Ok(());
    }
    let profile = WorkspaceProfile::new(worktree.path, store.next_port())?;
    store.insert(profile)?;
    Ok(())
}

fn set_menu_language(app: &AppHandle, language: &str) -> Result<(), String> {
    if !matches!(language, "en" | "zh-CN") {
        return Err("Unknown menu language.".to_string());
    }
    app.state::<DesktopState>()
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .set_language(language)
}

fn remove_workspace_from_menu(app: &AppHandle, profile_id: String) {
    let profile_and_language = {
        let state = app.state::<DesktopState>();
        state.store.lock().ok().and_then(|store| {
            store
                .get(&profile_id)
                .map(|profile| (profile, store.language().to_string()))
        })
    };
    let Some((profile, language)) = profile_and_language else {
        show_error(app, "Workspace profile was not found.");
        return;
    };
    let dialog_app = app.clone();
    let callback_app = app.clone();
    dialog_app
        .dialog()
        .message(format!(
            "{} “{}”?",
            menu_text(&language, "Remove workspace", "移除工作区"),
            profile.name
        ))
        .buttons(MessageDialogButtons::OkCancelCustom(
            menu_text(&language, "Remove", "移除").into(),
            menu_text(&language, "Cancel", "取消").into(),
        ))
        .show(move |confirmed| {
            if !confirmed {
                return;
            }
            let result = (|| {
                let state = callback_app.state::<DesktopState>();
                let running = state
                    .runtime
                    .lock()
                    .map_err(|_| "Runtime manager is unavailable.".to_string())?
                    .status(&profile)
                    .is_active();
                if running {
                    return Err("Stop the workspace before removing it.".into());
                }
                state
                    .store
                    .lock()
                    .map_err(|_| "Profile store is unavailable.".to_string())?
                    .remove(&profile_id)?;
                Ok::<(), String>(())
            })();
            if let Err(error) = result {
                show_error(&callback_app, error);
                return;
            }
            refresh_tray_menu_on_main(callback_app);
        });
}

fn handle_tray_menu_event(app: &AppHandle, id: &str) {
    if id == "add-workspace" {
        add_workspace_from_menu(app);
    } else if id == "refresh" {
        if let Err(error) = refresh_tray_menu(app) {
            show_error(app, error);
        }
    } else if id == "resource:prepare-runtime" || id == "resource:repair-runtime" {
        let repair = id == "resource:repair-runtime";
        let runtime = Arc::clone(&app.state::<DesktopState>().runtime);
        let callback_app = app.clone();
        tauri::async_runtime::spawn(async move {
            let result = tauri::async_runtime::spawn_blocking(move || {
                prepare_runtime_environment(&runtime, repair)
            })
            .await
            .map_err(|error| error.to_string())
            .and_then(|result| result);
            if let Err(error) = result {
                show_error(&callback_app, error);
            }
            refresh_tray_menu_on_main(callback_app);
        });
    } else if id == "resource:repair-dependencies" {
        let callback_app = app.clone();
        tauri::async_runtime::spawn(async move {
            if let Err(error) = repair_dependencies(callback_app.clone()).await {
                show_error(&callback_app, error);
            }
            refresh_tray_menu_on_main(callback_app);
        });
    } else if id == "resource:uv" || id == "resource:cloudflared" {
        let target = id.trim_start_matches("resource:").to_string();
        let callback_app = app.clone();
        tauri::async_runtime::spawn(async move {
            if let Err(error) = install_resource(callback_app.clone(), target).await {
                show_error(&callback_app, error);
            }
            refresh_tray_menu_on_main(callback_app);
        });
    } else if id == "resource:github" {
        if let Err(error) = open_resource("github".into()) {
            show_error(app, error);
        }
    } else if let Some(language) = id.strip_prefix("language:") {
        if let Err(error) = set_menu_language(app, language) {
            show_error(app, error);
        }
        let _ = refresh_tray_menu(app);
    } else if id == "quit" {
        app.exit(0);
    } else if let Some(profile_id) = id.strip_prefix("start:") {
        start_workspace_from_menu(app.clone(), profile_id.to_string());
    } else if let Some(profile_id) = id.strip_prefix("stop:") {
        stop_workspace_from_menu(app.clone(), profile_id.to_string());
    } else if let Some(profile_id) = id.strip_prefix("copy-url:") {
        if let Err(error) = copy_server_url(app, profile_id) {
            show_error(app, error);
        }
        let _ = refresh_tray_menu(app);
    } else if let Some(profile_id) = id.strip_prefix("copy-passcode:") {
        if let Err(error) = copy_passcode(app, profile_id) {
            show_error(app, error);
        }
    } else if let Some(profile_id) = id.strip_prefix("logs-open:") {
        if let Err(error) = open_logs_folder(app, profile_id) {
            show_error(app, error);
        }
    } else if let Some(profile_id) = id.strip_prefix("logs-copy:") {
        if let Err(error) = copy_recent_logs(app, profile_id) {
            show_error(app, error);
        }
    } else if let Some(selection) = id.strip_prefix("approval-approve:") {
        if let Some((profile_id, approval_id)) = selection.split_once(':') {
            if let Err(error) = decide_approval_from_menu(app, profile_id, approval_id, true) {
                show_error(app, error);
            }
            let _ = refresh_tray_menu(app);
        }
    } else if let Some(selection) = id.strip_prefix("approval-deny:") {
        if let Some((profile_id, approval_id)) = selection.split_once(':') {
            if let Err(error) = decide_approval_from_menu(app, profile_id, approval_id, false) {
                show_error(app, error);
            }
            let _ = refresh_tray_menu(app);
        }
    } else if let Some(selection) = id.strip_prefix("worktree-add:") {
        if let Some((profile_id, worktree_id)) = selection.split_once(':') {
            if let Err(error) = add_managed_worktree_from_menu(app, profile_id, worktree_id) {
                show_error(app, error);
            }
            let _ = refresh_tray_menu(app);
        }
    } else if let Some(profile_id) = id.strip_prefix("access:standard:") {
        if let Err(error) = set_permission_mode_from_menu(app, profile_id, "trusted") {
            show_error(app, error);
        }
        let _ = refresh_tray_menu(app);
    } else if let Some(profile_id) = id.strip_prefix("access:full:") {
        if let Err(error) = set_permission_mode_from_menu(app, profile_id, "host") {
            show_error(app, error);
        }
        let _ = refresh_tray_menu(app);
    } else if let Some(selection) = id.strip_prefix("permission:") {
        if let Some((permission_mode, profile_id)) = selection.split_once(':') {
            if let Err(error) = set_permission_mode_from_menu(app, profile_id, permission_mode) {
                show_error(app, error);
            }
            let _ = refresh_tray_menu(app);
        }
    } else if let Some(profile_id) = id.strip_prefix("remove:") {
        remove_workspace_from_menu(app, profile_id.to_string());
    }
}

fn toggle_panel(app: &AppHandle, tray_rect: Rect) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "Desktop panel is unavailable.".to_string())?;
    if window.is_visible().map_err(menu_error)? {
        window.hide().map_err(menu_error)?;
        return Ok(());
    }

    let scale_factor = window.scale_factor().map_err(menu_error)?;
    let tray_position = tray_rect.position.to_physical::<f64>(scale_factor);
    let tray_size = tray_rect.size.to_physical::<f64>(scale_factor);
    let window_size = window.outer_size().map_err(menu_error)?;
    let tray_center_x = tray_position.x + tray_size.width / 2.0;
    let tray_center_y = tray_position.y + tray_size.height / 2.0;
    let monitor = window
        .available_monitors()
        .map_err(menu_error)?
        .into_iter()
        .find(|monitor| {
            let position = monitor.position();
            let size = monitor.size();
            tray_center_x >= f64::from(position.x)
                && tray_center_x < f64::from(position.x) + f64::from(size.width)
                && tray_center_y >= f64::from(position.y)
                && tray_center_y < f64::from(position.y) + f64::from(size.height)
        });

    let mut x = tray_center_x - f64::from(window_size.width) / 2.0;
    let mut y = tray_position.y + tray_size.height + 6.0;
    if let Some(monitor) = monitor {
        let position = monitor.position();
        let size = monitor.size();
        let min_x = f64::from(position.x) + 8.0;
        let max_x =
            f64::from(position.x) + f64::from(size.width) - f64::from(window_size.width) - 8.0;
        x = x.clamp(min_x, max_x.max(min_x));
        let monitor_mid_y = f64::from(position.y) + f64::from(size.height) / 2.0;
        if tray_center_y > monitor_mid_y {
            y = tray_position.y - f64::from(window_size.height) - 6.0;
        }
    }
    window
        .set_position(PhysicalPosition::new(x.round() as i32, y.round() as i32))
        .map_err(menu_error)?;
    window.show().map_err(menu_error)?;
    window.set_focus().map_err(menu_error)
}

pub fn run() {
    let store = ProfileStore::open_default()
        .unwrap_or_else(|error| panic!("Could not initialize desktop storage: {error}"));
    let state = DesktopState {
        store: Arc::new(Mutex::new(store)),
        runtime: Arc::new(Mutex::new(RuntimeManager::new())),
    };
    let cleanup = state.runtime.clone();
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(state)
        .setup(|app| {
            let resources = if cfg!(debug_assertions) {
                std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources")
            } else {
                app.path().resource_dir()?
            };
            app.state::<DesktopState>()
                .runtime
                .lock()
                .map_err(|_| std::io::Error::other("Runtime manager is unavailable"))?
                .configure_environment(resources, app.path().app_local_data_dir()?);
            #[cfg(target_os = "macos")]
            {
                app.set_activation_policy(tauri::ActivationPolicy::Accessory);
                app.handle().set_dock_visibility(false)?;
            }
            let panel =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("Coding Tools MCP")
                    .inner_size(320.0, 374.0)
                    .minimizable(false)
                    .maximizable(false)
                    .closable(false)
                    .resizable(false)
                    .decorations(false)
                    .transparent(true)
                    .shadow(false)
                    .always_on_top(true)
                    .visible_on_all_workspaces(true)
                    .skip_taskbar(true)
                    .accept_first_mouse(true)
                    .visible(cfg!(debug_assertions))
                    .build()?;
            let focus_panel = panel.clone();
            panel.on_window_event(move |event| {
                if !cfg!(debug_assertions) && matches!(event, WindowEvent::Focused(false)) {
                    let _ = focus_panel.hide();
                }
            });
            let fallback_menu = build_tray_menu(app.handle()).map_err(std::io::Error::other)?;
            #[cfg(not(target_os = "linux"))]
            let _ = fallback_menu;
            let tray_builder = TrayIconBuilder::with_id(TRAY_ID)
                .icon(tray_template_icon())
                .icon_as_template(cfg!(target_os = "macos"))
                .tooltip(format!("Coding Tools MCP {}", env!("CARGO_PKG_VERSION")));
            #[cfg(target_os = "linux")]
            let tray_builder = tray_builder.menu(&fallback_menu);
            tray_builder
                .show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        rect,
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        if let Err(error) = toggle_panel(tray.app_handle(), rect) {
                            show_error(tray.app_handle(), error);
                        }
                    }
                })
                .on_menu_event(|app, event| {
                    handle_tray_menu_event(app, event.id().as_ref());
                })
                .build(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            desktop_snapshot,
            decide_approval,
            create_profile,
            create_full_access_profile,
            save_profile,
            delete_profile,
            start_profile,
            stop_profile,
            stop_all_profiles,
            profile_status,
            profile_logs,
            open_logs,
            set_language,
            pick_workspace_folder,
            pick_allowed_folder,
            open_resource,
            install_resource,
            repair_dependencies,
            prepare_runtime,
            quit_app
        ])
        .build(tauri::generate_context!())
        .expect("error while building Coding Tools MCP Desktop")
        .run(move |_app, event| match event {
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. } => {
                let _ = shutdown(&cleanup);
            }
            _ => {}
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quick_tunnel_start_preserves_host_permission_mode() {
        let workspace = tempfile::tempdir().unwrap();
        let mut profile =
            WorkspaceProfile::new(workspace.path().to_string_lossy().to_string(), 28766).unwrap();
        profile.runtime.permission_mode = "host".into();

        let profile = quick_tunnel_profile(profile);

        assert_eq!(profile.runtime.permission_mode, "host");
        assert_eq!(profile.auth.r#type, "oauth");
        assert_eq!(profile.tunnel.cloudflare_mode, "quick");
    }

    #[test]
    fn permission_mode_labels_cover_every_supported_mode() {
        assert_eq!(permission_mode_label("safe", "en"), "Safe");
        assert_eq!(permission_mode_label("trusted", "en"), "Trusted");
        assert_eq!(permission_mode_label("dangerous", "en"), "Dangerous");
        assert_eq!(permission_mode_label("host", "en"), "Host · full access");
        assert_eq!(permission_mode_label("host", "zh-CN"), "主机 · 完全访问");
    }

    #[test]
    fn desktop_access_collapses_new_choices_without_hiding_legacy_modes() {
        assert_eq!(desktop_access_label("trusted", "en"), "Standard");
        assert_eq!(desktop_access_label("host", "en"), "Full Access");
        assert_eq!(desktop_access_label("safe", "en"), "Legacy · Safe");
        assert_eq!(desktop_access_label("dangerous", "zh-CN"), "旧模式 · 危险");
    }

    #[test]
    fn allowed_paths_are_canonicalized_deduplicated_and_exclude_workspace() {
        let container = tempfile::tempdir().unwrap();
        let workspace = container.path().join("workspace");
        let child = workspace.join("child");
        let allowed = container.path().join("allowed");
        std::fs::create_dir_all(&child).unwrap();
        std::fs::create_dir(&allowed).unwrap();
        let mut profile =
            WorkspaceProfile::new(workspace.to_string_lossy().to_string(), 28766).unwrap();
        profile.runtime.allowed_paths = vec![
            allowed.to_string_lossy().to_string(),
            allowed.to_string_lossy().to_string(),
            workspace.to_string_lossy().to_string(),
            child.to_string_lossy().to_string(),
            container.path().to_string_lossy().to_string(),
        ];

        normalize_allowed_paths(&mut profile).unwrap();

        assert_eq!(
            profile.runtime.allowed_paths,
            vec![std::fs::canonicalize(container.path())
                .unwrap()
                .to_string_lossy()
                .to_string()]
        );
    }
}
