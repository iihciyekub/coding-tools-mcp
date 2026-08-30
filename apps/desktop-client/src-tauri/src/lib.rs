mod models;
mod runtime;
mod storage;

use models::{LogBundle, RuntimeStatus, WorkspaceProfile};
use runtime::{read_logs, RuntimeManager};
use serde::Serialize;
use std::collections::HashMap;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use storage::ProfileStore;
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
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
    Ok(DesktopSnapshot { profiles, statuses })
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
        .pid
        .is_some()
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
        .pid
        .is_some()
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
    profile_id: String,
    state: tauri::State<'_, DesktopState>,
) -> Result<RuntimeStatus, String> {
    let store = Arc::clone(&state.store);
    let runtime = Arc::clone(&state.runtime);
    tauri::async_runtime::spawn_blocking(move || {
        let (profile, log_dir) = {
            let mut store = store.lock().map_err(|_| "Profile store is unavailable.")?;
            let profile = store.prepare_for_start(&profile_id)?;
            let log_dir = store.log_dir(&profile_id)?;
            (profile, log_dir)
        };
        runtime
            .lock()
            .map_err(|_| "Runtime manager is unavailable.")?
            .start(&profile, &log_dir)
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
    profile.runtime.permission_mode = "trusted".into();
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
    if status.pid.is_none() {
        "Stopped"
    } else if status.state == "running" {
        "Running"
    } else {
        "Starting / connection issue"
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
    let statuses = {
        let mut runtime = state
            .runtime
            .lock()
            .map_err(|_| "Runtime manager is unavailable.".to_string())?;
        profiles
            .iter()
            .map(|profile| (profile.id.clone(), runtime.status(profile)))
            .collect::<HashMap<_, _>>()
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
        let running = status.pid.is_some();
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
    let uv = MenuItem::with_id(app, "resource:uv", "Install uv…", true, None::<&str>)
        .map_err(menu_error)?;
    resources.append(&uv).map_err(menu_error)?;
    let cloudflared = MenuItem::with_id(
        app,
        "resource:cloudflared",
        "Install cloudflared…",
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
            state
                .runtime
                .lock()
                .map_err(|_| "Runtime manager is unavailable.".to_string())?
                .start(&profile, &log_dir)?;
            Ok::<(), String>(())
        })();
        if let Err(error) = result {
            show_error(&app, error);
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
                    .pid
                    .is_some();
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
    } else if id == "resource:uv" {
        if let Err(error) = open_resource("uv".into()) {
            show_error(app, error);
        }
    } else if id == "resource:cloudflared" {
        if let Err(error) = open_resource("cloudflared".into()) {
            show_error(app, error);
        }
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
