mod environment;

use crate::models::{LogBundle, RuntimeStatus, WorkspaceProfile, MCP_ENDPOINT_PATH};
use crate::resource_installer;
use regex::Regex;
use serde::Serialize;
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const START_TIMEOUT: Duration = Duration::from_secs(20);
const TUNNEL_TIMEOUT: Duration = Duration::from_secs(30);
const LOG_LIMIT: usize = 16_000;

struct ManagedChild {
    child: Child,
    group_id: u32,
}

impl ManagedChild {
    fn is_running(&mut self) -> bool {
        self.child.try_wait().ok().flatten().is_none()
    }

    fn terminate(&mut self) {
        if !self.is_running() {
            return;
        }
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
            if !self.is_running() {
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
    }
}

struct ManagedSession {
    runtime: ManagedChild,
    tunnel: Option<ManagedChild>,
    public_url: Arc<Mutex<String>>,
}

pub struct RuntimeManager {
    sessions: HashMap<String, ManagedSession>,
    preparing: HashMap<String, Arc<AtomicBool>>,
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

    fn start(
        &mut self,
        profile: &WorkspaceProfile,
        log_dir: &Path,
        resolved: (PathBuf, Vec<String>),
    ) -> Result<RuntimeStatus, String> {
        profile.validate()?;
        if self.sessions.contains_key(&profile.id) {
            let status = self.status(profile);
            if status.state == "running" {
                return Ok(status);
            }
            self.stop(profile);
        }
        if port_is_listening(profile.runtime.local_port) {
            return Err(format!("Local port {} is already in use. Stop the existing process or choose another port.", profile.runtime.local_port));
        }
        fs::create_dir_all(log_dir).map_err(|error| error.to_string())?;
        let mut runtime =
            spawn_runtime(profile, log_dir, resolved, &self.data_dir.join("workflow"))?;
        if let Err(error) = wait_for_port(profile.runtime.local_port, &mut runtime, START_TIMEOUT) {
            runtime.terminate();
            return Err(error);
        }

        let public_url = Arc::new(Mutex::new(profile.public_url()));
        let tunnel = match profile.tunnel.r#type.as_str() {
            "frp" => None,
            "cloudflare" => {
                match spawn_cloudflare(profile, log_dir, Arc::clone(&public_url), &self.data_dir) {
                    Ok(child) => Some(child),
                    Err(error) => {
                        runtime.terminate();
                        return Err(error);
                    }
                }
            }
            _ => {
                runtime.terminate();
                return Err(
                    "Only Cloudflare and externally managed FRP tunnels are supported.".into(),
                );
            }
        };
        self.sessions.insert(
            profile.id.clone(),
            ManagedSession {
                runtime,
                tunnel,
                public_url,
            },
        );
        Ok(self.status(profile))
    }

    pub fn stop(&mut self, profile: &WorkspaceProfile) -> RuntimeStatus {
        if let Some(cancelled) = self.preparing.remove(&profile.id) {
            cancelled.store(true, Ordering::Relaxed);
        }
        if let Some(mut session) = self.sessions.remove(&profile.id) {
            if let Some(tunnel) = session.tunnel.as_mut() {
                tunnel.terminate();
            }
            session.runtime.terminate();
        }
        RuntimeStatus::stopped(profile.runtime.local_port)
    }

    pub fn status(&mut self, profile: &WorkspaceProfile) -> RuntimeStatus {
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
            local_message: format!("Listening on 127.0.0.1:{}", profile.runtime.local_port),
            public_message,
            public_url,
            local_url: format!(
                "http://127.0.0.1:{}{MCP_ENDPOINT_PATH}",
                profile.runtime.local_port
            ),
        }
    }

    pub fn stop_all(&mut self) {
        for (_, cancelled) in self.preparing.drain() {
            cancelled.store(true, Ordering::Relaxed);
        }
        for (_, mut session) in self.sessions.drain() {
            if let Some(tunnel) = session.tunnel.as_mut() {
                tunnel.terminate();
            }
            session.runtime.terminate();
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
    let cancelled = Arc::new(AtomicBool::new(false));
    let (resources, data) = {
        let mut state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?;
        let status = state.status(profile);
        if status.state == "running" {
            return Ok(status);
        }
        if state.preparing.contains_key(&profile.id) {
            return Err("This workspace is already preparing its runtime.".into());
        }
        state
            .preparing
            .insert(profile.id.clone(), Arc::clone(&cancelled));
        (state.resource_dir.clone(), state.data_dir.clone())
    };
    // Download/install without holding the UI/status mutex.
    let resolved = environment::resolve(&resources, &data, log_dir, &effective_path(), &cancelled);
    let tunnel_setup = if resolved.is_ok()
        && profile.tunnel.r#type == "cloudflare"
        && resolve_cloudflared(&data).is_err()
    {
        resource_installer::install("cloudflared", &data, &cancelled).map(|_| ())
    } else {
        Ok(())
    };
    let mut state = manager
        .lock()
        .map_err(|_| "Runtime manager is unavailable")?;
    if cancelled.load(Ordering::Relaxed) {
        return Err("Workspace startup was cancelled.".into());
    }
    state.preparing.remove(&profile.id);
    let resolved = resolved?;
    state.runtime_ready_hint = true;
    tunnel_setup?;
    state.start(profile, log_dir, resolved)
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
            !state.sessions.is_empty() || !state.preparing.is_empty(),
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
    if profile.runtime.file_access_scope == "home" {
        if let Some(home) = std::env::var_os("HOME") {
            command.arg("--file-access-root").arg(home);
        }
    }
    command
        .current_dir(&profile.path)
        .env("PATH", effective_path());
    for name in [
        "CODING_TOOLS_MCP_AUTH_MODE",
        "CODING_TOOLS_MCP_AUTH_TOKEN",
        "CODING_TOOLS_MCP_OAUTH_MODE",
        "CODING_TOOLS_MCP_OAUTH_PASSWORD",
        "CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET",
    ] {
        command.env_remove(name);
    }
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

fn spawn_cloudflare(
    profile: &WorkspaceProfile,
    log_dir: &Path,
    public_url: Arc<Mutex<String>>,
    data_dir: &Path,
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
    configure_process_group(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start cloudflared: {error}"))?;
    let group_id = child.id();
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    let log = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(log_dir.join("cloudflared.log"))
        .map_err(|error| error.to_string())?;
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

fn wait_for_port(port: u16, child: &mut ManagedChild, timeout: Duration) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
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

    #[test]
    fn stopping_preparation_cancels_its_start_intent() {
        let temporary = tempfile::tempdir().unwrap();
        let profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        let mut manager = RuntimeManager::new();
        let cancelled = Arc::new(AtomicBool::new(false));
        manager
            .preparing
            .insert(profile.id.clone(), Arc::clone(&cancelled));
        assert_eq!(manager.status(&profile).state, "starting");
        assert!(manager.status(&profile).is_active());
        manager.stop(&profile);
        assert!(cancelled.load(Ordering::Relaxed));
        assert_eq!(manager.status(&profile).state, "stopped");
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
