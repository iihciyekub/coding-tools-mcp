use crate::models::{LogBundle, RuntimeStatus, WorkspaceProfile, MCP_ENDPOINT_PATH};
use regex::Regex;
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
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
}

impl RuntimeManager {
    pub fn new() -> Self {
        Self {
            sessions: HashMap::new(),
        }
    }

    pub fn start(
        &mut self,
        profile: &WorkspaceProfile,
        log_dir: &Path,
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
        let mut runtime = spawn_runtime(profile, log_dir)?;
        if let Err(error) = wait_for_port(profile.runtime.local_port, &mut runtime, START_TIMEOUT) {
            runtime.terminate();
            return Err(error);
        }

        let public_url = Arc::new(Mutex::new(profile.public_url()));
        let tunnel = match profile.tunnel.r#type.as_str() {
            "frp" => None,
            "cloudflare" => match spawn_cloudflare(profile, log_dir, Arc::clone(&public_url)) {
                Ok(child) => Some(child),
                Err(error) => {
                    runtime.terminate();
                    return Err(error);
                }
            },
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
        if let Some(mut session) = self.sessions.remove(&profile.id) {
            if let Some(tunnel) = session.tunnel.as_mut() {
                tunnel.terminate();
            }
            session.runtime.terminate();
        }
        RuntimeStatus::stopped(profile.runtime.local_port)
    }

    pub fn status(&mut self, profile: &WorkspaceProfile) -> RuntimeStatus {
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
        for (_, mut session) in self.sessions.drain() {
            if let Some(tunnel) = session.tunnel.as_mut() {
                tunnel.terminate();
            }
            session.runtime.terminate();
        }
    }
}

pub fn read_logs(log_dir: &Path) -> Result<LogBundle, String> {
    Ok(LogBundle {
        cloudflared: tail(&log_dir.join("cloudflared.log"))?,
        stderr: tail(&log_dir.join("stderr.log"))?,
        stdout: tail(&log_dir.join("stdout.log"))?,
    })
}

fn spawn_runtime(profile: &WorkspaceProfile, log_dir: &Path) -> Result<ManagedChild, String> {
    let (program, prefix) = resolve_runtime()?;
    let mut command = Command::new(program);
    command.args(prefix).args([
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
    ]);
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
) -> Result<ManagedChild, String> {
    let executable = resolve_cloudflared()?;
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

fn resolve_runtime() -> Result<(PathBuf, Vec<String>), String> {
    let path = effective_path();
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    if let Ok(explicit) = std::env::var("CODING_TOOLS_MCP_DESKTOP_RUNTIME") {
        return Ok((PathBuf::from(explicit), vec![]));
    }
    if let Ok(program) = which::which_in("coding-tools-mcp", Some(&path), &cwd) {
        return Ok((program, vec![]));
    }
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let python = if cfg!(windows) {
        repo.join(".venv/Scripts/python.exe")
    } else {
        repo.join(".venv/bin/python")
    };
    if python.is_file() && repo.join("coding_tools_mcp").is_dir() {
        return Ok((python, vec!["-m".into(), "coding_tools_mcp".into()]));
    }
    if let Ok(program) = which::which_in("uvx", Some(&path), &cwd) {
        return Ok((program, vec!["coding-tools-mcp".into()]));
    }
    Err("Could not find coding-tools-mcp or uvx. Install uv and ensure it is available in your login shell PATH.".into())
}

fn resolve_cloudflared() -> Result<PathBuf, String> {
    if let Ok(explicit) = std::env::var("CODING_TOOLS_MCP_DESKTOP_CLOUDFLARED") {
        return Ok(PathBuf::from(explicit));
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
    which::which_in("cloudflared", Some(effective_path()), cwd).map_err(|_| {
        "cloudflared was not found. Install Cloudflare Tunnel and restart the app.".into()
    })
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
    fn missing_logs_are_empty() {
        let temporary = tempfile::tempdir().unwrap();
        let logs = read_logs(temporary.path()).unwrap();
        assert!(logs.stderr.is_empty());
    }

    #[test]
    fn loopback_probe_rejects_an_unused_port() {
        assert!(!port_is_listening(1));
    }
}
