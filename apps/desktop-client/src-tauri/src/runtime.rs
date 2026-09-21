mod environment;

use crate::models::{LogBundle, RuntimeStatus, WorkspaceProfile, MCP_ENDPOINT_PATH};
use crate::resource_installer;
use chrono::Local;
use rand::distr::{Alphanumeric, SampleString};
use regex::Regex;
use serde::Serialize;
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const START_TIMEOUT: Duration = Duration::from_secs(20);
const TUNNEL_TIMEOUT: Duration = Duration::from_secs(30);
const LOG_LIMIT: usize = 16_000;

fn computer_helper_path(resources: &Path) -> PathBuf {
    resources.join(
        "computer/Coding Tools MCP App Helper.app/Contents/MacOS/coding-tools-computer-helper",
    )
}

struct ManagedChild {
    child: Child,
    group_id: u32,
}

impl ManagedChild {
    fn is_running(&mut self) -> bool {
        self.child.try_wait().ok().flatten().is_none()
    }

    fn terminate(&mut self) {
        if self.group_id == 0 {
            return;
        }
        // A launcher may exit before its server. Always terminate the owned
        // process group, even when the direct child has already been reaped.
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.group_id as i32), libc::SIGTERM);
        }
        #[cfg(windows)]
        {
            let _ = Command::new("taskkill")
                .args(["/PID", &self.group_id.to_string(), "/T", "/F"])
                .status();
        }
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            let running = self.is_running();
            #[cfg(unix)]
            let running = running || unsafe { libc::kill(-(self.group_id as i32), 0) == 0 };
            if !running {
                self.group_id = 0;
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.group_id as i32), libc::SIGKILL);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
        #[cfg(unix)]
        {
            let deadline = Instant::now() + Duration::from_secs(1);
            while unsafe { libc::kill(-(self.group_id as i32), 0) == 0 }
                && Instant::now() < deadline
            {
                thread::sleep(Duration::from_millis(10));
            }
        }
        self.group_id = 0;
    }
}

impl Drop for ManagedChild {
    fn drop(&mut self) {
        self.terminate();
    }
}

#[derive(Default)]
struct PendingOperation {
    cancelled: AtomicBool,
    finished: Mutex<bool>,
    completion: Condvar,
}

impl PendingOperation {
    fn finish(&self) {
        *self.finished.lock().unwrap_or_else(|e| e.into_inner()) = true;
        self.completion.notify_all();
    }

    fn wait(&self) {
        let mut finished = self.finished.lock().unwrap_or_else(|e| e.into_inner());
        while !*finished {
            finished = self
                .completion
                .wait(finished)
                .unwrap_or_else(|e| e.into_inner());
        }
    }
}

struct ManagedSession {
    runtime: ManagedChild,
    tunnel: Option<ManagedChild>,
    public_url: Arc<Mutex<String>>,
    server_name: String,
}

fn new_server_name(prefix: &str) -> String {
    let random_code = Alphanumeric
        .sample_string(&mut rand::rng(), 2)
        .to_ascii_uppercase();
    format!(
        "{}{}{}",
        prefix.trim(),
        random_code,
        Local::now().format("%Y%m%d%H%M%S")
    )
}

pub struct RuntimeManager {
    sessions: HashMap<String, ManagedSession>,
    preparing: HashMap<String, Arc<PendingOperation>>,
    stopping: HashMap<String, Arc<PendingOperation>>,
    shutting_down: bool,
    resource_dir: PathBuf,
    data_dir: PathBuf,
    runtime_ready_hint: bool,
}

#[derive(Clone, Serialize)]
pub struct DependencyStatus {
    pub uv: bool,
    pub cloudflared: bool,
    pub runtime_ready: bool,
    pub runtime_version: Option<String>,
}

impl RuntimeManager {
    pub fn new() -> Self {
        Self {
            sessions: HashMap::new(),
            preparing: HashMap::new(),
            stopping: HashMap::new(),
            shutting_down: false,
            resource_dir: PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources"),
            data_dir: PathBuf::new(),
            runtime_ready_hint: false,
        }
    }

    pub fn configure_environment(&mut self, resources: PathBuf, data: PathBuf) {
        self.resource_dir = resources;
        self.data_dir = data;
    }

    pub fn dependency_status(&self) -> DependencyStatus {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let (managed_runtime_ready, runtime_version) =
            environment::readiness(&self.resource_dir, &self.data_dir, &effective_path());
        let runtime_ready =
            managed_runtime_ready || self.runtime_ready_hint || !self.sessions.is_empty();
        DependencyStatus {
            uv: resource_installer::is_managed_installed("uv", &self.data_dir)
                || which::which_in("uv", Some(effective_path()), &cwd).is_ok(),
            cloudflared: resolve_cloudflared(&self.data_dir).is_ok(),
            runtime_ready,
            runtime_version,
        }
    }

    pub fn computer_helper_path(&self) -> PathBuf {
        computer_helper_path(&self.resource_dir)
    }

    pub fn status(&mut self, profile: &WorkspaceProfile) -> RuntimeStatus {
        if self.stopping.contains_key(&profile.id) {
            let mut status = RuntimeStatus::stopped(profile.runtime.local_port);
            status.state = "stopping".into();
            status.local_message = "Stopping runtime processes".into();
            return status;
        }
        if self.preparing.contains_key(&profile.id) {
            let mut status = RuntimeStatus::stopped(profile.runtime.local_port);
            status.state = "starting".into();
            status.local_message = "Preparing runtime dependencies".into();
            return status;
        }
        let Some(session) = self.sessions.get_mut(&profile.id) else {
            return RuntimeStatus::stopped(profile.runtime.local_port);
        };
        if !session.runtime.is_running() {
            if let Some(tunnel) = session.tunnel.as_mut() {
                tunnel.terminate();
            }
            self.sessions.remove(&profile.id);
            return RuntimeStatus::stopped(profile.runtime.local_port);
        }
        let runtime_pid = session.runtime.child.id();
        let public_url = session
            .public_url
            .lock()
            .map(|value| value.clone())
            .unwrap_or_default();
        let tunnel_ok = profile.tunnel.r#type == "frp"
            || session
                .tunnel
                .as_mut()
                .is_some_and(ManagedChild::is_running);
        let (state, public_message) = if tunnel_ok {
            if profile.tunnel.r#type == "cloudflare" && public_url.is_empty() {
                (
                    "error",
                    "Waiting for Cloudflare to assign a public URL".into(),
                )
            } else if profile.tunnel.r#type == "frp" {
                ("running", "External FRP client required".into())
            } else {
                ("running", public_url.clone())
            }
        } else {
            ("error", "The Cloudflare tunnel is not connected".into())
        };
        RuntimeStatus {
            state: state.into(),
            pid: Some(runtime_pid),
            server_name: session.server_name.clone(),
            local_message: format!("Listening on 127.0.0.1:{}", profile.runtime.local_port),
            public_message,
            public_url,
            local_url: format!(
                "http://127.0.0.1:{}{MCP_ENDPOINT_PATH}",
                profile.runtime.local_port
            ),
        }
    }

    pub fn workflow_state_root(&self) -> PathBuf {
        self.data_dir.join("workflow")
    }
}

pub fn start_workspace(
    manager: &Arc<Mutex<RuntimeManager>>,
    profile: &WorkspaceProfile,
    log_dir: &Path,
) -> Result<RuntimeStatus, String> {
    profile.validate()?;
    let operation = Arc::new(PendingOperation::default());
    let cancelled = &operation.cancelled;
    let (resources, data) = {
        let mut state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?;
        if state.shutting_down {
            return Err("The desktop app is shutting down.".into());
        }
        if state.stopping.contains_key(&profile.id) {
            return Err("This workspace is still stopping. Please retry shortly.".into());
        }
        let status = state.status(profile);
        if status.pid.is_some() {
            return Ok(status);
        }
        if state.preparing.contains_key(&profile.id) {
            return Err("This workspace is already preparing its runtime.".into());
        }
        state
            .preparing
            .insert(profile.id.clone(), Arc::clone(&operation));
        (state.resource_dir.clone(), state.data_dir.clone())
    };
    // Resolve dependencies and launch processes without holding the UI/status mutex.
    let resolved = environment::resolve(&resources, &data, log_dir, &effective_path(), cancelled);
    let runtime_ready = resolved.is_ok();
    let startup = resolved.and_then(|resolved| {
        if profile.tunnel.r#type == "cloudflare" && resolve_cloudflared(&data).is_err() {
            resource_installer::install("cloudflared", &data, cancelled)?;
        }
        start_session(profile, log_dir, resolved, &data, &resources, cancelled)
    });
    let mut state = manager
        .lock()
        .map_err(|_| "Runtime manager is unavailable")?;
    let owns_start = state
        .preparing
        .get(&profile.id)
        .is_some_and(|signal| Arc::ptr_eq(signal, &operation));
    if runtime_ready {
        state.runtime_ready_hint = true;
    }
    if cancelled.load(Ordering::Relaxed) || !owns_start {
        drop(state);
        if let Ok(session) = startup {
            terminate_session(session);
        }
        let mut state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?;
        if owns_start {
            state.preparing.remove(&profile.id);
        }
        operation.finish();
        return Err("Workspace startup was cancelled.".into());
    }
    state.preparing.remove(&profile.id);
    operation.finish();
    let session = startup?;
    state.sessions.insert(profile.id.clone(), session);
    Ok(state.status(profile))
}

pub fn stop_workspace(
    manager: &Arc<Mutex<RuntimeManager>>,
    profile: &WorkspaceProfile,
) -> Result<RuntimeStatus, String> {
    let request = {
        let mut state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?;
        begin_stop(&mut state, &profile.id)
    };
    finish_stop(manager, request)?;
    Ok(RuntimeStatus::stopped(profile.runtime.local_port))
}

pub fn stop_all_workspaces(manager: &Arc<Mutex<RuntimeManager>>) -> Result<(), String> {
    let requests = {
        let mut state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?;
        let ids = state
            .sessions
            .keys()
            .chain(state.preparing.keys())
            .chain(state.stopping.keys())
            .cloned()
            .collect::<std::collections::HashSet<_>>();
        ids.iter()
            .map(|id| begin_stop(&mut state, id))
            .collect::<Vec<_>>()
    };
    for request in requests {
        finish_stop(manager, request)?;
    }
    Ok(())
}

pub fn shutdown(manager: &Arc<Mutex<RuntimeManager>>) -> Result<(), String> {
    manager
        .lock()
        .map_err(|_| "Runtime manager is unavailable")?
        .shutting_down = true;
    stop_all_workspaces(manager)
}

enum StopRequest {
    Wait(Arc<PendingOperation>),
    Cleanup {
        id: String,
        completion: Arc<PendingOperation>,
        startup: Option<Arc<PendingOperation>>,
        session: Option<ManagedSession>,
    },
}

fn begin_stop(state: &mut RuntimeManager, id: &str) -> StopRequest {
    if let Some(completion) = state.stopping.get(id) {
        return StopRequest::Wait(Arc::clone(completion));
    }
    let startup = state.preparing.get(id).cloned();
    if let Some(startup) = &startup {
        startup.cancelled.store(true, Ordering::Relaxed);
    }
    let completion = Arc::new(PendingOperation::default());
    state
        .stopping
        .insert(id.to_string(), Arc::clone(&completion));
    StopRequest::Cleanup {
        id: id.to_string(),
        completion,
        startup,
        session: state.sessions.remove(id),
    }
}

fn finish_stop(manager: &Arc<Mutex<RuntimeManager>>, request: StopRequest) -> Result<(), String> {
    match request {
        StopRequest::Wait(completion) => completion.wait(),
        StopRequest::Cleanup {
            id,
            completion,
            startup,
            session,
        } => {
            if let Some(startup) = startup {
                startup.wait();
            }
            if let Some(session) = session {
                terminate_session(session);
            }
            manager
                .lock()
                .map_err(|_| "Runtime manager is unavailable")?
                .stopping
                .remove(&id);
            completion.finish();
        }
    }
    Ok(())
}

fn start_session(
    profile: &WorkspaceProfile,
    log_dir: &Path,
    resolved: (PathBuf, Vec<String>),
    data_dir: &Path,
    resource_dir: &Path,
    cancelled: &AtomicBool,
) -> Result<ManagedSession, String> {
    if cancelled.load(Ordering::Relaxed) {
        return Err("Workspace startup was cancelled.".into());
    }
    if port_is_listening(profile.runtime.local_port) {
        return Err(format!(
            "Local port {} is already in use. Stop the existing process or choose another port.",
            profile.runtime.local_port
        ));
    }
    fs::create_dir_all(log_dir).map_err(|error| error.to_string())?;
    let server_name = new_server_name(&profile.runtime.server_name_prefix);
    let mut runtime = spawn_runtime(
        profile,
        log_dir,
        resolved,
        &data_dir.join("workflow"),
        &server_name,
        Some(&computer_helper_path(resource_dir)),
    )?;
    if let Err(error) = wait_for_port(
        profile.runtime.local_port,
        &mut runtime,
        START_TIMEOUT,
        cancelled,
    ) {
        runtime.terminate();
        return Err(error);
    }

    let public_url = Arc::new(Mutex::new(profile.public_url()));
    let tunnel = match profile.tunnel.r#type.as_str() {
        "frp" => None,
        "cloudflare" => match spawn_cloudflare(
            profile,
            log_dir,
            Arc::clone(&public_url),
            data_dir,
            cancelled,
        ) {
            Ok(child) => Some(child),
            Err(error) => {
                runtime.terminate();
                return Err(error);
            }
        },
        _ => {
            runtime.terminate();
            return Err("Only Cloudflare and externally managed FRP tunnels are supported.".into());
        }
    };
    Ok(ManagedSession {
        runtime,
        tunnel,
        public_url,
        server_name,
    })
}

fn terminate_session(mut session: ManagedSession) {
    if let Some(tunnel) = session.tunnel.as_mut() {
        tunnel.terminate();
    }
    session.runtime.terminate();
}

pub fn prepare_runtime(
    manager: &Arc<Mutex<RuntimeManager>>,
    repair: bool,
) -> Result<String, String> {
    let cancelled = AtomicBool::new(false);
    let (resources, data, active) = {
        let state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?;
        (
            state.resource_dir.clone(),
            state.data_dir.clone(),
            !state.sessions.is_empty() || !state.preparing.is_empty() || !state.stopping.is_empty(),
        )
    };
    if repair && active {
        return Err("Stop all workspaces before repairing the managed runtime.".into());
    }
    if repair {
        environment::reset_managed(&resources, &data)?;
        manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?
            .runtime_ready_hint = false;
    }
    let log_dir = data.join("logs").join("runtime");
    fs::create_dir_all(&log_dir).map_err(|e| e.to_string())?;
    environment::resolve(&resources, &data, &log_dir, &effective_path(), &cancelled)?;
    manager
        .lock()
        .map_err(|_| "Runtime manager is unavailable")?
        .runtime_ready_hint = true;
    let (_, version) = environment::readiness(&resources, &data, &effective_path());
    Ok(format!(
        "Runtime {} ready.",
        version.as_deref().unwrap_or("managed")
    ))
}

pub fn read_logs(log_dir: &Path) -> Result<LogBundle, String> {
    Ok(LogBundle {
        cloudflared: tail(&log_dir.join("cloudflared.log"))?,
        stderr: tail(&log_dir.join("stderr.log"))?,
        stdout: tail(&log_dir.join("stdout.log"))?,
    })
}

fn spawn_runtime(
    profile: &WorkspaceProfile,
    log_dir: &Path,
    resolved: (PathBuf, Vec<String>),
    workflow_state_root: &Path,
    server_name: &str,
    computer_helper: Option<&Path>,
) -> Result<ManagedChild, String> {
    let (program, prefix) = resolved;
    let mut command = Command::new(program);
    command
        .args(prefix)
        .args([
            "--workspace",
            &profile.path,
            "--host",
            "127.0.0.1",
            "--port",
            &profile.runtime.local_port.to_string(),
            "--permission-mode",
            &profile.runtime.permission_mode,
            "--shell-env-inherit",
            "all",
            "--enable-workflow-tools",
            "--defer-workflow-tools",
        ])
        .arg("--state-root")
        .arg(workflow_state_root);
    for root in file_access_roots(profile) {
        command.arg("--file-access-root").arg(root);
    }
    if profile.runtime.computer_enabled {
        if !cfg!(target_os = "macos") {
            return Err("Application control currently requires macOS.".into());
        }
        let helper = computer_helper.filter(|path| path.is_file()).ok_or(
            "The bundled computer helper is missing. Reinstall or rebuild the desktop app.",
        )?;
        command
            .arg("--enable-computer-tools")
            .arg("--computer-helper")
            .arg(helper);
    }
    if profile.runtime.permission_mode == "host" {
        command.arg("--dangerously-fake-readonly-annotations");
    }
    command
        .current_dir(&profile.path)
        .env("PATH", effective_path());
    if let Some(ssh_auth_sock) = effective_ssh_auth_sock() {
        command.env("SSH_AUTH_SOCK", ssh_auth_sock);
    }
    for name in [
        "CODING_TOOLS_MCP_AUTH_MODE",
        "CODING_TOOLS_MCP_AUTH_TOKEN",
        "CODING_TOOLS_MCP_OAUTH_MODE",
        "CODING_TOOLS_MCP_OAUTH_PASSWORD",
        "CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET",
        "CODING_TOOLS_MCP_SERVER_NAME",
        "CODING_TOOLS_MCP_ENABLE_COMPUTER_TOOLS",
        "CODING_TOOLS_MCP_COMPUTER_HELPER",
    ] {
        command.env_remove(name);
    }
    for variable in runtime_environment_variables(profile) {
        command.env(variable.name.trim(), &variable.value);
    }
    command.env("CODING_TOOLS_MCP_SERVER_NAME", server_name);
    match profile.auth.r#type.as_str() {
        "oauth" => {
            command
                .arg("--oauth-mode")
                .env(
                    "CODING_TOOLS_MCP_OAUTH_PASSWORD",
                    &profile.auth.oauth_password,
                )
                .env(
                    "CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET",
                    &profile.auth.oauth_token_secret,
                );
            if profile.tunnel.r#type == "cloudflare" && profile.tunnel.cloudflare_mode == "named" {
                command.env("CODING_TOOLS_MCP_SERVER_URL", profile.public_url());
            }
        }
        "bearer" => {
            command.arg("--auth-token").arg(&profile.auth.bearer_token);
        }
        _ => return Err("Unknown authentication type.".into()),
    }
    let stdout = File::create(log_dir.join("stdout.log")).map_err(|error| error.to_string())?;
    let stderr = File::create(log_dir.join("stderr.log")).map_err(|error| error.to_string())?;
    command.stdout(stdout).stderr(stderr).stdin(Stdio::null());
    configure_process_group(&mut command);
    let child = command
        .spawn()
        .map_err(|error| format!("Could not start Coding Tools MCP: {error}"))?;
    let group_id = child.id();
    Ok(ManagedChild { child, group_id })
}

fn file_access_roots(profile: &WorkspaceProfile) -> Vec<String> {
    if profile.runtime.permission_mode == "host" {
        Vec::new()
    } else {
        profile.runtime.allowed_paths.clone()
    }
}

fn runtime_environment_variables(
    profile: &WorkspaceProfile,
) -> &[crate::models::EnvironmentVariable] {
    if profile.runtime.permission_mode == "host" {
        &profile.runtime.environment_variables
    } else {
        &[]
    }
}

fn spawn_cloudflare(
    profile: &WorkspaceProfile,
    log_dir: &Path,
    public_url: Arc<Mutex<String>>,
    data_dir: &Path,
    cancelled: &AtomicBool,
) -> Result<ManagedChild, String> {
    let executable = resolve_cloudflared(data_dir)?;
    let mut command = Command::new(executable);
    if profile.tunnel.cloudflare_mode == "named" {
        command.args([
            "tunnel",
            "run",
            "--token",
            profile.tunnel.cloudflare_token.trim(),
        ]);
    } else {
        command.args([
            "tunnel",
            "--url",
            &format!("http://127.0.0.1:{}", profile.runtime.local_port),
        ]);
    }
    command
        .current_dir(&profile.path)
        .env("PATH", effective_path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let log = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(log_dir.join("cloudflared.log"))
        .map_err(|error| error.to_string())?;
    configure_process_group(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start cloudflared: {error}"))?;
    let group_id = child.id();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let log = Arc::new(Mutex::new(log));
    let (sender, receiver) = mpsc::channel::<TunnelEvent>();
    if let Some(stream) = stdout {
        stream_tunnel_output(
            stream,
            Arc::clone(&log),
            sender.clone(),
            Arc::clone(&public_url),
        );
    }
    if let Some(stream) = stderr {
        stream_tunnel_output(stream, log, sender, Arc::clone(&public_url));
    }
    let mut managed = ManagedChild { child, group_id };
    let deadline = Instant::now() + TUNNEL_TIMEOUT;
    while Instant::now() < deadline {
        if cancelled.load(Ordering::Relaxed) {
            managed.terminate();
            return Err("Workspace startup was cancelled.".into());
        }
        if !managed.is_running() {
            return Err(
                "cloudflared exited before establishing a tunnel. Check cloudflared.log.".into(),
            );
        }
        if let Ok(event) = receiver.recv_timeout(Duration::from_millis(250)) {
            match event {
                TunnelEvent::Url(url) if profile.tunnel.cloudflare_mode == "quick" => {
                    if let Ok(mut value) = public_url.lock() {
                        *value = url;
                    }
                    return Ok(managed);
                }
                TunnelEvent::Ready if profile.tunnel.cloudflare_mode == "named" => {
                    return Ok(managed)
                }
                _ => {}
            }
        }
    }
    managed.terminate();
    Err(
        "cloudflared did not establish the tunnel before the timeout. Check cloudflared.log."
            .into(),
    )
}

enum TunnelEvent {
    Url(String),
    Ready,
}

fn stream_tunnel_output<R: Read + Send + 'static>(
    stream: R,
    log: Arc<Mutex<File>>,
    sender: mpsc::Sender<TunnelEvent>,
    public_url: Arc<Mutex<String>>,
) {
    thread::spawn(move || {
        let expression = Regex::new(r"https://[a-z0-9-]+\.trycloudflare\.com").unwrap();
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            if let Ok(mut file) = log.lock() {
                let _ = writeln!(file, "{line}");
                let _ = file.flush();
            }
            if let Some(matched) = expression.find(&line) {
                let url = matched.as_str().to_string();
                if let Ok(mut value) = public_url.lock() {
                    *value = url.clone();
                }
                let _ = sender.send(TunnelEvent::Url(url));
            }
            if line
                .to_ascii_lowercase()
                .contains("registered tunnel connection")
            {
                let _ = sender.send(TunnelEvent::Ready);
            }
        }
    });
}

fn wait_for_port(
    port: u16,
    child: &mut ManagedChild,
    timeout: Duration,
    cancelled: &AtomicBool,
) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if cancelled.load(Ordering::Relaxed) {
            return Err("Workspace startup was cancelled.".into());
        }
        if port_is_listening(port) {
            return Ok(());
        }
        if !child.is_running() {
            return Err(
                "Coding Tools MCP exited before opening its local port. Check stderr.log.".into(),
            );
        }
        thread::sleep(Duration::from_millis(150));
    }
    Err(format!(
        "Coding Tools MCP did not listen on port {port} before the timeout."
    ))
}

fn resolve_cloudflared(data_dir: &Path) -> Result<PathBuf, String> {
    if let Ok(explicit) = std::env::var("CODING_TOOLS_MCP_DESKTOP_CLOUDFLARED") {
        return Ok(PathBuf::from(explicit));
    }
    let managed = resource_installer::tools_bin(data_dir).join("cloudflared");
    if managed.is_file() {
        return Ok(managed);
    }
    let native_candidates = if cfg!(target_os = "macos") {
        vec![
            PathBuf::from("/opt/homebrew/bin/cloudflared"),
            PathBuf::from("/usr/local/bin/cloudflared"),
        ]
    } else {
        vec![]
    };
    for candidate in native_candidates {
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    which::which_in("cloudflared", Some(effective_path()), cwd)
        .map_err(|_| "cloudflared was not found.".into())
}

fn effective_path() -> String {
    let current = std::env::var("PATH").unwrap_or_default();
    #[cfg(target_os = "macos")]
    {
        if let Ok(output) = Command::new("/bin/zsh")
            .args(["-lic", "printf %s \"$PATH\""])
            .output()
        {
            let login = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !login.is_empty() {
                return format!("{login}:{current}");
            }
        }
    }
    current
}

fn effective_ssh_auth_sock() -> Option<String> {
    if let Ok(current) = std::env::var("SSH_AUTH_SOCK") {
        let current = current.trim().to_string();
        if !current.is_empty() {
            return Some(current);
        }
    }
    #[cfg(target_os = "macos")]
    {
        if let Ok(output) = Command::new("/bin/zsh")
            .args(["-lic", "printf %s \"$SSH_AUTH_SOCK\""])
            .output()
        {
            let login = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !login.is_empty() {
                return Some(login);
            }
        }
        if let Ok(output) = Command::new("/bin/launchctl")
            .args(["getenv", "SSH_AUTH_SOCK"])
            .output()
        {
            let launchd = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !launchd.is_empty() {
                return Some(launchd);
            }
        }
    }
    None
}

fn port_is_listening(port: u16) -> bool {
    TcpStream::connect_timeout(
        &SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(180),
    )
    .is_ok()
}

fn tail(path: &Path) -> Result<String, String> {
    if !path.exists() {
        return Ok(String::new());
    }
    let bytes = fs::read(path).map_err(|error| error.to_string())?;
    let start = bytes.len().saturating_sub(LOG_LIMIT);
    Ok(String::from_utf8_lossy(&bytes[start..]).to_string())
}

#[cfg(unix)]
fn configure_process_group(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    command.process_group(0);
}

#[cfg(windows)]
fn configure_process_group(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x00000200);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    fn orphaned_listener() -> (ManagedChild, u16) {
        let mut command = Command::new("python3");
        command
            .args([
                "-c",
                r#"
import os, signal, socket, sys, time
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(listener.getsockname()[1], flush=True)
if os.fork():
    os._exit(0)
while True:
    time.sleep(1)
"#,
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        configure_process_group(&mut command);
        let mut child = command.spawn().unwrap();
        let mut port = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut port)
            .unwrap();
        let group_id = child.id();
        child.wait().unwrap();
        (
            ManagedChild { child, group_id },
            port.trim().parse().unwrap(),
        )
    }

    #[cfg(unix)]
    fn direct_listener() -> (ManagedChild, u16) {
        let mut command = Command::new("python3");
        command
            .args([
                "-c",
                r#"
import socket, time
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
print(listener.getsockname()[1], flush=True)
while True:
    time.sleep(1)
"#,
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        configure_process_group(&mut command);
        let mut child = command.spawn().unwrap();
        let mut port = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut port)
            .unwrap();
        let group_id = child.id();
        (
            ManagedChild { child, group_id },
            port.trim().parse().unwrap(),
        )
    }

    #[test]
    #[cfg(unix)]
    fn termination_releases_port_after_launcher_exits() {
        let (mut process, port) = orphaned_listener();
        let group_id = process.group_id;
        assert!(port_is_listening(port));
        process.terminate();
        let deadline = Instant::now() + Duration::from_secs(2);
        while port_is_listening(port) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        let released = !port_is_listening(port);
        // Clean the fixture up even when exercising the old broken implementation.
        if !released {
            unsafe {
                libc::kill(-(group_id as i32), libc::SIGKILL);
            }
        }
        assert!(
            released,
            "The launcher exited, but its child still owns the local port"
        );
    }

    #[test]
    #[cfg(unix)]
    fn stop_and_shutdown_terminate_runtime_and_tunnel_processes() {
        for use_shutdown in [false, true] {
            let temporary = tempfile::tempdir().unwrap();
            let profile =
                WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766)
                    .unwrap();
            let (runtime, runtime_port) = direct_listener();
            let (tunnel, tunnel_port) = direct_listener();
            let manager = Arc::new(Mutex::new(RuntimeManager::new()));
            manager.lock().unwrap().sessions.insert(
                profile.id.clone(),
                ManagedSession {
                    runtime,
                    tunnel: Some(tunnel),
                    public_url: Arc::new(Mutex::new(String::new())),
                    server_name: "cleanup-test".into(),
                },
            );
            assert!(port_is_listening(runtime_port));
            assert!(port_is_listening(tunnel_port));

            if use_shutdown {
                shutdown(&manager).unwrap();
            } else {
                stop_workspace(&manager, &profile).unwrap();
            }

            assert!(!port_is_listening(runtime_port));
            assert!(!port_is_listening(tunnel_port));
            assert!(manager.lock().unwrap().sessions.is_empty());
        }
    }

    #[test]
    fn server_names_include_prefix_random_code_and_second_precision() {
        let name = new_server_name("www");
        assert!(Regex::new(r"^www[A-Z0-9]{2}\d{14}$")
            .unwrap()
            .is_match(&name));
    }

    #[test]
    fn full_access_does_not_pass_redundant_file_roots() {
        let temporary = tempfile::tempdir().unwrap();
        let extra = temporary.path().join("Applications");
        fs::create_dir(&extra).unwrap();
        let mut profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        profile.runtime.allowed_paths = vec![extra.to_string_lossy().into_owned()];

        assert_eq!(file_access_roots(&profile), profile.runtime.allowed_paths);

        profile.runtime.permission_mode = "host".into();
        let roots = file_access_roots(&profile);
        assert!(roots.is_empty());
    }

    #[test]
    #[cfg(unix)]
    fn environment_variables_are_injected_only_for_full_access() {
        let temporary = tempfile::tempdir().unwrap();
        let mut profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        for (permission_mode, expected) in [("trusted", "missing"), ("host", "secret")] {
            let capture = temporary.path().join(format!("{permission_mode}.txt"));
            let script = temporary
                .path()
                .join(format!("capture-{permission_mode}.py"));
            fs::write(
                &script,
                format!(
                    "import os\nopen({:?}, 'w').write(os.environ.get('TEST_API_KEY', 'missing'))\n",
                    capture.to_string_lossy()
                ),
            )
            .unwrap();
            profile.runtime.permission_mode = permission_mode.into();
            profile.runtime.environment_variables = vec![crate::models::EnvironmentVariable {
                name: "TEST_API_KEY".into(),
                value: "secret".into(),
            }];
            let log_dir = temporary.path().join(format!("logs-{permission_mode}"));
            fs::create_dir(&log_dir).unwrap();
            let mut process = spawn_runtime(
                &profile,
                &log_dir,
                (
                    which::which("python3").unwrap(),
                    vec![script.to_string_lossy().into_owned()],
                ),
                temporary.path(),
                "test-server",
                None,
            )
            .unwrap();
            assert!(process.child.wait().unwrap().success());
            assert_eq!(fs::read_to_string(capture).unwrap(), expected);
        }
    }

    #[test]
    fn stopping_preparation_waits_for_cleanup_before_allowing_restart() {
        let temporary = tempfile::tempdir().unwrap();
        let profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        let manager = Arc::new(Mutex::new(RuntimeManager::new()));
        let operation = Arc::new(PendingOperation::default());
        manager
            .lock()
            .unwrap()
            .preparing
            .insert(profile.id.clone(), Arc::clone(&operation));
        assert_eq!(manager.lock().unwrap().status(&profile).state, "starting");
        assert!(manager.lock().unwrap().status(&profile).is_active());
        let stopping_manager = Arc::clone(&manager);
        let stopping_profile = profile.clone();
        let stopper = thread::spawn(move || stop_workspace(&stopping_manager, &stopping_profile));
        let deadline = Instant::now() + Duration::from_secs(2);
        while !operation.cancelled.load(Ordering::Relaxed) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        let status = manager.lock().unwrap().status(&profile);
        let restart = start_workspace(&manager, &profile, temporary.path());
        let finished_early = stopper.is_finished();
        // Complete the simulated startup cleanup before assertions so failures
        // cannot leave a waiting test thread behind.
        manager.lock().unwrap().preparing.remove(&profile.id);
        operation.finish();
        stopper.join().unwrap().unwrap();
        assert!(operation.cancelled.load(Ordering::Relaxed));
        assert!(!finished_early);
        assert_eq!(status.state, "stopping");
        assert!(status.is_active());
        assert!(restart.unwrap_err().contains("still stopping"));
        assert_eq!(manager.lock().unwrap().status(&profile).state, "stopped");
    }

    #[test]
    fn shutdown_waits_for_startup_cleanup_and_rejects_new_starts() {
        let temporary = tempfile::tempdir().unwrap();
        let profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        let manager = Arc::new(Mutex::new(RuntimeManager::new()));
        let operation = Arc::new(PendingOperation::default());
        manager
            .lock()
            .unwrap()
            .preparing
            .insert(profile.id.clone(), Arc::clone(&operation));
        let shutdown_manager = Arc::clone(&manager);
        let exiting = thread::spawn(move || shutdown(&shutdown_manager));
        let deadline = Instant::now() + Duration::from_secs(2);
        while !operation.cancelled.load(Ordering::Relaxed) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        let finished_early = exiting.is_finished();
        let restart = start_workspace(&manager, &profile, temporary.path());
        manager.lock().unwrap().preparing.remove(&profile.id);
        operation.finish();
        exiting.join().unwrap().unwrap();
        assert!(!finished_early);
        assert!(operation.cancelled.load(Ordering::Relaxed));
        assert!(restart.unwrap_err().contains("shutting down"));
        assert!(manager.lock().unwrap().stopping.is_empty());
    }

    #[test]
    fn stopped_and_reopened_workspaces_can_reuse_the_port() {
        let temporary = tempfile::tempdir().unwrap();
        let parent = temporary.path().join("project");
        let nested = parent.join("child");
        let same_name = temporary.path().join("other/project");
        fs::create_dir_all(&nested).unwrap();
        fs::create_dir_all(&same_name).unwrap();
        let script = temporary.path().join("listener.py");
        fs::write(
            &script,
            r#"
import argparse, socket, time
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int)
args, _ = parser.parse_known_args()
listener = socket.socket()
listener.bind(('127.0.0.1', args.port))
listener.listen(64)
while True:
    connection, _ = listener.accept()
    connection.close()
"#,
        )
        .unwrap();
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let mut manager = Arc::new(Mutex::new(RuntimeManager::new()));
        for (index, path) in [&parent, &parent, &same_name, &nested]
            .into_iter()
            .enumerate()
        {
            let mut profile =
                WorkspaceProfile::new(path.to_string_lossy().into_owned(), port).unwrap();
            profile.tunnel.r#type = "frp".into();
            profile.tunnel.frp_server = "example.test".into();
            profile.tunnel.frp_subdomain = "test".into();
            profile.validate().unwrap();
            let session = start_session(
                &profile,
                &temporary.path().join(format!("logs-{index}")),
                (
                    which::which("python3").unwrap(),
                    vec![script.to_string_lossy().into_owned()],
                ),
                temporary.path(),
                temporary.path(),
                &AtomicBool::new(false),
            )
            .unwrap();
            manager
                .lock()
                .unwrap()
                .sessions
                .insert(profile.id.clone(), session);
            assert_eq!(manager.lock().unwrap().status(&profile).state, "running");
            if index % 2 == 0 {
                stop_workspace(&manager, &profile).unwrap();
            } else {
                shutdown(&manager).unwrap();
                manager = Arc::new(Mutex::new(RuntimeManager::new()));
            }
            assert!(!port_is_listening(port));
        }
    }

    #[test]
    fn missing_logs_are_empty() {
        let temporary = tempfile::tempdir().unwrap();
        let logs = read_logs(temporary.path()).unwrap();
        assert!(logs.stderr.is_empty());
    }

    #[test]
    fn loopback_probe_rejects_an_unused_port() {
        assert!(!port_is_listening(1));
    }

    #[test]
    fn loopback_probe_detects_a_running_listener() {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(port_is_listening(port));
    }
}
