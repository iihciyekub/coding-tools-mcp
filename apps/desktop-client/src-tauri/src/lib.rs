mod models;
mod resource_installer;
mod runtime;
mod storage;

use models::{LogBundle, RuntimeStatus, WorkspaceProfile};
use resource_installer::managed_version;
use runtime::{
    open_app_permission as open_app_permission_runtime,
    prepare_chrome_bridge as prepare_chrome_bridge_runtime,
    prepare_runtime as prepare_runtime_environment, read_logs, start_workspace, DependencyStatus,
    RuntimeManager,
};
use serde::Serialize;
use std::collections::HashMap;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use storage::ProfileStore;
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

const TRAY_ID: &str = "coding-tools-mcp";
const WORKSPACE_NAME_MAX_CHARS: usize = 22;

#[derive(Clone)]
struct DesktopState {
    store: Arc<Mutex<ProfileStore>>,
    runtime: Arc<Mutex<RuntimeManager>>,
}

#[derive(Serialize)]
struct DesktopSnapshot {
    profiles: Vec<WorkspaceProfile>,
    statuses: HashMap<String, RuntimeStatus>,
    dependencies: DependencyStatus,
}

#[tauri::command]
fn desktop_snapshot(state: tauri::State<'_, DesktopState>) -> Result<DesktopSnapshot, String> {
    let profiles = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.")?
        .profiles();
    let mut runtime = state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.")?;
    let statuses = profiles
        .iter()
        .map(|profile| (profile.id.clone(), runtime.status(profile)))
        .collect();
    let dependencies = runtime.dependency_status();
    Ok(DesktopSnapshot {
        profiles,
        statuses,
        dependencies,
    })
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
    let profile = WorkspaceProfile::new(path, store.next_port())?;
    store.insert(profile)
}

#[tauri::command]
fn save_profile(
    profile: WorkspaceProfile,
    state: tauri::State<'_, DesktopState>,
) -> Result<WorkspaceProfile, String> {
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
        Ok(runtime
            .lock()
            .map_err(|_| "Runtime manager is unavailable.")?
            .stop(&profile))
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

#[tauri::command]
async fn open_permission_settings(
    permission: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<String, String> {
    let runtime = Arc::clone(&state.runtime);
    tauri::async_runtime::spawn_blocking(move || open_app_permission_runtime(&runtime, &permission))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn prepare_chrome_bridge(state: tauri::State<'_, DesktopState>) -> Result<String, String> {
    let store = Arc::clone(&state.store);
    let runtime = Arc::clone(&state.runtime);
    tauri::async_runtime::spawn_blocking(move || {
        let (profile, log_dir) = {
            let store = store
                .lock()
                .map_err(|_| "Profile store is unavailable.".to_string())?;
            let profile =
                store.profiles().into_iter().next().ok_or_else(|| {
                    "Add a workspace before preparing the Chrome bridge.".to_string()
                })?;
            let log_dir = store.log_dir(&profile.id)?;
            (profile, log_dir)
        };
        prepare_chrome_bridge_runtime(&runtime, &profile, &log_dir)
    })
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
    draw_polyline(&mut rgba, SIZE, &cloud, 2.8);
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
        2.8,
    );
    draw_stroke(&mut rgba, SIZE, (12.0, 19.0), (20.0, 19.0), 2.8);
    draw_stroke(&mut rgba, SIZE, (14.0, 15.5), (14.0, 19.0), 2.8);
    draw_stroke(&mut rgba, SIZE, (18.0, 15.5), (18.0, 19.0), 2.8);
    draw_stroke(&mut rgba, SIZE, (16.0, 24.0), (16.0, 28.0), 2.8);
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

fn status_text(status: &RuntimeStatus) -> &'static str {
    if status.state == "starting" {
        "Preparing runtime"
    } else if status.pid.is_none() {
        "Stopped"
    } else if status.state == "running" {
        "Running"
    } else {
        "Starting / connection issue"
    }
}

fn permission_mode_label(mode: &str) -> &'static str {
    match mode {
        "safe" => "Safe",
        "trusted" => "Trusted",
        "dangerous" => "Dangerous",
        "host" => "Host · full access",
        _ => "Unknown",
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
    let profiles = state
        .store
        .lock()
        .map_err(|_| "Profile store is unavailable.".to_string())?
        .profiles();
    let (statuses, dependencies) = {
        let mut runtime = state
            .runtime
            .lock()
            .map_err(|_| "Runtime manager is unavailable.".to_string())?;
        let statuses = profiles
            .iter()
            .map(|profile| (profile.id.clone(), runtime.status(profile)))
            .collect::<HashMap<_, _>>();
        let dependencies = runtime.dependency_status();
        (statuses, dependencies)
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
        let empty = MenuItem::with_id(app, "menu:empty", "No workspaces yet", false, None::<&str>)
            .map_err(menu_error)?;
        menu.append(&empty).map_err(menu_error)?;
    }

    if !profiles.is_empty() {
        let workspaces_title =
            MenuItem::with_id(app, "menu:workspaces", "Workspaces", false, None::<&str>)
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
                status_text(&status)
            ),
            true,
            running.then(running_indicator_icon),
        )
        .map_err(menu_error)?;

        let status_item = MenuItem::with_id(
            app,
            format!("status:{}", profile.id),
            format!("Status · {}", status_text(&status)),
            false,
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&status_item).map_err(menu_error)?;

        let path_item = MenuItem::with_id(
            app,
            format!("path:{}", profile.id),
            format!("Folder · {}", profile.path),
            false,
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&path_item).map_err(menu_error)?;
        let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
        workspace.append(&separator).map_err(menu_error)?;

        let permission_menu = Submenu::with_id(
            app,
            format!("permission-menu:{}", profile.id),
            format!(
                "Permission mode · {}",
                permission_mode_label(&profile.runtime.permission_mode)
            ),
            true,
        )
        .map_err(menu_error)?;
        for mode in ["safe", "trusted", "dangerous", "host"] {
            let item = CheckMenuItem::with_id(
                app,
                format!("permission:{mode}:{}", profile.id),
                permission_mode_label(mode),
                !running,
                profile.runtime.permission_mode == mode,
                None::<&str>,
            )
            .map_err(menu_error)?;
            permission_menu.append(&item).map_err(menu_error)?;
        }
        workspace.append(&permission_menu).map_err(menu_error)?;

        let power_item = MenuItem::with_id(
            app,
            format!("{}:{}", if running { "stop" } else { "start" }, profile.id),
            if running {
                "Stop workspace"
            } else {
                "Start workspace"
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
            "Server URL · Copy",
            !endpoint.is_empty(),
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&copy_url).map_err(menu_error)?;
        let copy_passcode = MenuItem::with_id(
            app,
            format!("copy-passcode:{}", profile.id),
            "Authorization passcode · Copy",
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
            "Remove workspace…",
            !running,
            None::<&str>,
        )
        .map_err(menu_error)?;
        workspace.append(&remove).map_err(menu_error)?;
        menu.append(&workspace).map_err(menu_error)?;
    }

    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    menu.append(&separator).map_err(menu_error)?;
    let add = MenuItem::with_id(app, "add-workspace", "Add workspace…", true, None::<&str>)
        .map_err(menu_error)?;
    menu.append(&add).map_err(menu_error)?;
    let refresh =
        MenuItem::with_id(app, "refresh", "Refresh", true, None::<&str>).map_err(menu_error)?;
    menu.append(&refresh).map_err(menu_error)?;

    let resources = Submenu::with_id(app, "resources", "Resources", true).map_err(menu_error)?;
    for (id, label) in [
        (
            "resource-status:runtime",
            format!(
                "MCP Runtime · {}{}",
                if dependencies.runtime_ready {
                    "Ready"
                } else {
                    "Not prepared"
                },
                dependencies
                    .runtime_version
                    .as_deref()
                    .map(|version| format!(" · {version}"))
                    .unwrap_or_default()
            ),
        ),
        (
            "resource-status:playwright",
            format!(
                "Playwright · {}{}",
                if dependencies.playwright_ready {
                    "Ready"
                } else {
                    "Not prepared"
                },
                dependencies
                    .playwright_version
                    .as_deref()
                    .map(|version| format!(" · {version}"))
                    .unwrap_or_default()
            ),
        ),
        (
            "resource-status:uv",
            format!(
                "uv · {}",
                if dependencies.uv {
                    "Ready"
                } else {
                    "Not found"
                }
            ),
        ),
        (
            "resource-status:cloudflared",
            format!(
                "cloudflared · {}",
                if dependencies.cloudflared {
                    "Ready"
                } else {
                    "Not found"
                }
            ),
        ),
        (
            "resource-status:app-helper",
            format!(
                "App Helper · {}",
                if dependencies.app_helper {
                    "Ready"
                } else {
                    "Not found"
                }
            ),
        ),
        (
            "resource-status:chrome",
            format!(
                "Chrome · {}",
                if dependencies.chrome_installed {
                    "Installed"
                } else {
                    "Not found"
                }
            ),
        ),
        (
            "resource-status:cdp",
            format!(
                "Chrome CDP · {}",
                if dependencies.chrome_cdp_ready {
                    "Connected"
                } else {
                    "Not connected"
                }
            ),
        ),
        (
            "resource-status:bridge",
            format!(
                "Chrome Bridge · {}",
                if dependencies.chrome_bridge_connected {
                    "Connected"
                } else if dependencies.chrome_manifest {
                    "Installed"
                } else {
                    "Not installed"
                }
            ),
        ),
        (
            "resource-status:accessibility",
            format!(
                "Accessibility · {}",
                match dependencies.accessibility_trusted {
                    Some(true) => "Allowed",
                    Some(false) => "Permission required",
                    None => "Unavailable",
                }
            ),
        ),
        (
            "resource-status:screen-recording",
            format!(
                "Screen Recording · {}",
                match dependencies.screen_recording_trusted {
                    Some(true) => "Allowed",
                    Some(false) => "Permission required",
                    None => "Unavailable",
                }
            ),
        ),
    ] {
        let item = MenuItem::with_id(app, id, label, false, None::<&str>).map_err(menu_error)?;
        resources.append(&item).map_err(menu_error)?;
    }
    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    resources.append(&separator).map_err(menu_error)?;
    let prepare_runtime = MenuItem::with_id(
        app,
        "resource:prepare-runtime",
        "Prepare runtime…",
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&prepare_runtime).map_err(menu_error)?;
    let repair_runtime = MenuItem::with_id(
        app,
        "resource:repair-runtime",
        "Repair runtime…",
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&repair_runtime).map_err(menu_error)?;
    let chrome_bridge = MenuItem::with_id(
        app,
        "resource:chrome-bridge",
        "Prepare Chrome bridge…",
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&chrome_bridge).map_err(menu_error)?;
    let accessibility = MenuItem::with_id(
        app,
        "resource:accessibility",
        "Accessibility settings…",
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&accessibility).map_err(menu_error)?;
    let screen_recording = MenuItem::with_id(
        app,
        "resource:screen-recording",
        "Screen Recording settings…",
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&screen_recording).map_err(menu_error)?;
    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    resources.append(&separator).map_err(menu_error)?;
    let uv = MenuItem::with_id(
        app,
        "resource:uv",
        "Install / Repair uv…",
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&uv).map_err(menu_error)?;
    let cloudflared = MenuItem::with_id(
        app,
        "resource:cloudflared",
        "Install / Repair cloudflared…",
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&cloudflared).map_err(menu_error)?;
    let github = MenuItem::with_id(
        app,
        "resource:github",
        "Source on GitHub…",
        true,
        None::<&str>,
    )
    .map_err(menu_error)?;
    resources.append(&github).map_err(menu_error)?;
    menu.append(&resources).map_err(menu_error)?;

    let separator = PredefinedMenuItem::separator(app).map_err(menu_error)?;
    menu.append(&separator).map_err(menu_error)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Coding Tools MCP", true, None::<&str>)
        .map_err(menu_error)?;
    menu.append(&quit).map_err(menu_error)?;
    Ok(menu)
}

fn refresh_tray_menu(app: &AppHandle) -> Result<(), String> {
    let menu = build_tray_menu(app)?;
    let tray = app
        .tray_by_id(TRAY_ID)
        .ok_or_else(|| "Tray icon is unavailable.".to_string())?;
    tray.set_menu(Some(menu)).map_err(menu_error)
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
    let app = app.clone();
    let callback_app = app.clone();
    app.dialog()
        .file()
        .set_title("Choose workspace folder")
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
                let profile = WorkspaceProfile::new(path, store.next_port())?;
                store.insert(profile)?;
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
            state
                .runtime
                .lock()
                .map_err(|_| "Runtime manager is unavailable.".to_string())?
                .stop(&profile);
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

fn remove_workspace_from_menu(app: &AppHandle, profile_id: String) {
    let profile = {
        let state = app.state::<DesktopState>();
        state
            .store
            .lock()
            .ok()
            .and_then(|store| store.get(&profile_id))
    };
    let Some(profile) = profile else {
        show_error(app, "Workspace profile was not found.");
        return;
    };
    let dialog_app = app.clone();
    let callback_app = app.clone();
    dialog_app
        .dialog()
        .message(format!("Remove workspace “{}”?", profile.name))
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Remove".into(),
            "Cancel".into(),
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
    } else if id == "resource:chrome-bridge" {
        let state = app.state::<DesktopState>();
        let runtime = Arc::clone(&state.runtime);
        let profile_and_log = state
            .store
            .lock()
            .map_err(|_| "Profile store is unavailable.".to_string())
            .and_then(|store| {
                let profile = store.profiles().into_iter().next().ok_or_else(|| {
                    "Add a workspace before preparing the Chrome bridge.".to_string()
                })?;
                let log_dir = store.log_dir(&profile.id)?;
                Ok((profile, log_dir))
            });
        match profile_and_log {
            Ok((profile, log_dir)) => {
                let callback_app = app.clone();
                tauri::async_runtime::spawn(async move {
                    let result = tauri::async_runtime::spawn_blocking(move || {
                        prepare_chrome_bridge_runtime(&runtime, &profile, &log_dir)
                    })
                    .await
                    .map_err(|error| error.to_string())
                    .and_then(|result| result);
                    if let Err(error) = result {
                        show_error(&callback_app, error);
                    }
                    refresh_tray_menu_on_main(callback_app);
                });
            }
            Err(error) => show_error(app, error),
        }
    } else if id == "resource:accessibility" || id == "resource:screen-recording" {
        let permission = if id == "resource:accessibility" {
            "accessibility"
        } else {
            "screen_recording"
        }
        .to_string();
        let runtime = Arc::clone(&app.state::<DesktopState>().runtime);
        let callback_app = app.clone();
        tauri::async_runtime::spawn(async move {
            let result = tauri::async_runtime::spawn_blocking(move || {
                open_app_permission_runtime(&runtime, &permission)
            })
            .await
            .map_err(|error| error.to_string())
            .and_then(|result| result);
            if let Err(error) = result {
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
            let menu = build_tray_menu(app.handle()).map_err(std::io::Error::other)?;
            TrayIconBuilder::with_id(TRAY_ID)
                .icon(tray_template_icon())
                .icon_as_template(cfg!(target_os = "macos"))
                .tooltip(format!("Coding Tools MCP {}", env!("CARGO_PKG_VERSION")))
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(|app, event| {
                    handle_tray_menu_event(app, event.id().as_ref());
                })
                .build(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            desktop_snapshot,
            create_profile,
            save_profile,
            delete_profile,
            start_profile,
            stop_profile,
            profile_status,
            profile_logs,
            open_resource,
            install_resource,
            repair_dependencies,
            prepare_runtime,
            prepare_chrome_bridge,
            open_permission_settings,
            quit_app
        ])
        .build(tauri::generate_context!())
        .expect("error while building Coding Tools MCP Desktop")
        .run(move |_app, event| match event {
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. } => {
                if let Ok(mut runtime) = cleanup.lock() {
                    runtime.stop_all();
                }
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
        assert_eq!(permission_mode_label("safe"), "Safe");
        assert_eq!(permission_mode_label("trusted"), "Trusted");
        assert_eq!(permission_mode_label("dangerous"), "Dangerous");
        assert_eq!(permission_mode_label("host"), "Host · full access");
    }
}
