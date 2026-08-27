mod models;
mod runtime;
mod storage;

use models::{LogBundle, RuntimeStatus, WorkspaceProfile};
use runtime::{read_logs, RuntimeManager};
use serde::Serialize;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use storage::ProfileStore;

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
    state
        .runtime
        .lock()
        .map_err(|_| "Runtime manager is unavailable.")?
        .stop(&profile);
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
        .invoke_handler(tauri::generate_handler![
            desktop_snapshot,
            create_profile,
            save_profile,
            delete_profile,
            start_profile,
            stop_profile,
            profile_status,
            profile_logs
        ])
        .build(tauri::generate_context!())
        .expect("error while building Coding Tools MCP Desktop")
        .run(move |_app, event| {
            if matches!(
                event,
                tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
            ) {
                if let Ok(mut runtime) = cleanup.lock() {
                    runtime.stop_all();
                }
            }
        });
}
