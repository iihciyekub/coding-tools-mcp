//! Bounded cache refreshes; privileged repair is only invoked by a user action.
use super::*;

pub(super) fn refresh_user_cache() {
    #[cfg(target_os = "macos")]
    {
        let mut command = Command::new("/usr/bin/dscacheutil");
        command.arg("-flushcache");
        let _ = run_cache_command(command, Duration::from_secs(2), &AtomicBool::new(false));
    }
}

pub fn repair_local_dns(
    manager: &Arc<Mutex<RuntimeManager>>,
    language: &str,
) -> Result<bool, String> {
    repair_with(manager, |cancelled| {
        refresh_system_cache(language, cancelled)
    })
}

struct RepairGuard {
    manager: Arc<Mutex<RuntimeManager>>,
    operation: Arc<PendingOperation>,
}

impl Drop for RepairGuard {
    fn drop(&mut self) {
        let mut state = self.manager.lock().unwrap_or_else(|e| e.into_inner());
        if state
            .dns_repair
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, &self.operation))
        {
            state.dns_repair = None;
        }
        drop(state);
        self.operation.finish();
    }
}

fn repair_with(
    manager: &Arc<Mutex<RuntimeManager>>,
    refresh: impl FnOnce(&AtomicBool) -> Result<(), String>,
) -> Result<bool, String> {
    let operation = Arc::new(PendingOperation::default());
    {
        let mut state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable.")?;
        if state.dns_repair.is_some() {
            return Err("DNS repair is already in progress.".into());
        }
        if state.shutting_down
            || !state.connection_wanted
            || !state.local_dns_error
            || !state.gateway_is_active()
        {
            return Err("Local DNS repair is not needed.".into());
        }
        state.dns_repair = Some(Arc::clone(&operation));
        state.dns_refresh_attempted = true;
        state.public_checked_at = None;
        state.public_probe_generation = state.public_probe_generation.wrapping_add(1);
    }
    let _guard = RepairGuard {
        manager: Arc::clone(manager),
        operation: Arc::clone(&operation),
    };
    // Keep ownership through verification. Stop cancels and awaits the entire
    // operation, and another window cannot open a second administrator prompt.
    refresh(&operation.cancelled)?;
    if operation.cancelled.load(Ordering::Relaxed) {
        return Err("DNS repair was cancelled.".into());
    }
    supervisor::check_public_endpoint_for_repair(manager, &operation);
    if operation.cancelled.load(Ordering::Relaxed) {
        return Err("DNS repair was cancelled.".into());
    }
    let state = manager
        .lock()
        .map_err(|_| "Runtime manager is unavailable.")?;
    Ok(state.connection_wanted && state.public_healthy == Some(true) && !state.local_dns_error)
}

#[cfg(target_os = "macos")]
fn refresh_system_cache(language: &str, cancelled: &AtomicBool) -> Result<(), String> {
    // Fixed commands and fixed prompts only. Do not add a caller-supplied shell
    // string, password, hostname, or path to this privileged operation.
    let prompt = if language == "zh-CN" {
        "Coding Tools MCP 需要刷新本机 DNS 缓存，以恢复授权页面访问。"
    } else {
        "Coding Tools MCP needs to refresh the local DNS cache to restore access to the authorization page."
    };
    let script = format!(
        "do shell script \"/usr/bin/dscacheutil -flushcache && /usr/bin/killall -HUP mDNSResponder\" with administrator privileges with prompt \"{prompt}\""
    );
    let mut command = Command::new("/usr/bin/osascript");
    command.args(["-e", &script]);
    run_cache_command(command, Duration::from_secs(120), cancelled)
}

#[cfg(not(target_os = "macos"))]
fn refresh_system_cache(_language: &str, _cancelled: &AtomicBool) -> Result<(), String> {
    Err("Built-in DNS repair is available on macOS only.".into())
}

#[cfg(target_os = "macos")]
fn run_cache_command(
    mut command: Command,
    timeout: Duration,
    cancelled: &AtomicBool,
) -> Result<(), String> {
    use std::os::fd::AsRawFd;
    if cancelled.load(Ordering::Relaxed) {
        return Err("DNS repair was cancelled.".into());
    }
    configure_process_group(&mut command);
    let child = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| "Could not start DNS cache refresh.")?;
    let group_id = child.id();
    let mut process = ManagedChild { child, group_id };
    let mut stderr = process
        .child
        .stderr
        .take()
        .ok_or("DNS cache refresh failed.")?;
    let flags = unsafe { libc::fcntl(stderr.as_raw_fd(), libc::F_GETFL) };
    if flags == -1
        || unsafe { libc::fcntl(stderr.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } == -1
    {
        return Err("DNS cache refresh failed.".into());
    }
    let deadline = Instant::now() + timeout;
    let mut errors = Vec::new();
    loop {
        let status = process.child.try_wait();
        // Drain a bounded amount without waiting for EOF: a descendant may
        // inherit stderr after its launcher exits. Retain only a short tail.
        let mut buffer = [0_u8; 4096];
        for _ in 0..16 {
            match stderr.read(&mut buffer) {
                Ok(0) => break,
                Ok(count) => {
                    errors.extend_from_slice(&buffer[..count]);
                    if errors.len() > 8192 {
                        errors.drain(..errors.len() - 8192);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => return Err("DNS cache refresh failed.".into()),
            }
        }
        if cancelled.load(Ordering::Relaxed) {
            return Err("DNS repair was cancelled.".into());
        }
        match status {
            Ok(Some(status)) => {
                return if status.success() {
                    Ok(())
                } else if String::from_utf8_lossy(&errors).contains("(-128)") {
                    Err("DNS repair was cancelled.".into())
                } else {
                    Err("DNS cache refresh failed.".into())
                };
            }
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            _ => return Err("DNS cache refresh timed out. Try again when ready.".into()),
        }
        // ManagedChild's Drop reaps the owned process group on every return.
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_stopped_connection_cannot_request_privileges() {
        let manager = Arc::new(Mutex::new(RuntimeManager::new()));
        let result = repair_with(&manager, |_| panic!("must not prompt"));
        assert_eq!(result.unwrap_err(), "Local DNS repair is not needed.");
        assert!(manager.lock().unwrap().dns_repair.is_none());
    }

    #[test]
    fn concurrent_repairs_do_not_open_another_prompt() {
        let manager = Arc::new(Mutex::new(RuntimeManager::new()));
        manager.lock().unwrap().dns_repair = Some(Arc::new(PendingOperation::default()));
        let result = repair_with(&manager, |_| panic!("must not prompt twice"));
        assert_eq!(result.unwrap_err(), "DNS repair is already in progress.");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn cache_commands_are_bounded_and_cancellable() {
        let cancelled = AtomicBool::new(false);
        assert!(run_cache_command(
            Command::new("/usr/bin/true"),
            Duration::from_secs(1),
            &cancelled
        )
        .is_ok());
        assert_eq!(
            run_cache_command(
                Command::new("/usr/bin/false"),
                Duration::from_secs(1),
                &cancelled
            )
            .unwrap_err(),
            "DNS cache refresh failed."
        );
        let mut slow = Command::new("/bin/sleep");
        slow.arg("60");
        assert_eq!(
            run_cache_command(slow, Duration::from_millis(20), &cancelled).unwrap_err(),
            "DNS cache refresh timed out. Try again when ready."
        );
        cancelled.store(true, Ordering::Relaxed);
        let mut slow = Command::new("/bin/sleep");
        slow.arg("60");
        assert_eq!(
            run_cache_command(slow, Duration::from_secs(60), &cancelled).unwrap_err(),
            "DNS repair was cancelled."
        );
    }

    #[cfg(unix)]
    fn gateway_fixture() -> (Arc<Mutex<RuntimeManager>>, u32) {
        let mut command = Command::new("/bin/sleep");
        command.arg("60");
        configure_process_group(&mut command);
        let child = command.spawn().unwrap();
        let pid = child.id();
        let manager = Arc::new(Mutex::new(RuntimeManager::new()));
        {
            let mut state = manager.lock().unwrap();
            state.connection_wanted = true;
            state.local_dns_error = true;
            state.public_healthy = Some(true);
            state.gateway = Some(ManagedGatewaySession {
                gateway: ManagedSession {
                    runtime: ManagedChild {
                        child,
                        group_id: pid,
                    },
                    tunnel: None,
                    public_url: Arc::new(Mutex::new("https://example.test".into())),
                    server_name: "test".into(),
                },
                projects: HashMap::new(),
                registry_path: PathBuf::new(),
                resolved_runtime: ResolvedRuntime {
                    command: (PathBuf::new(), vec![]),
                    build_id: "test".into(),
                },
            });
        }
        (manager, pid)
    }

    #[cfg(unix)]
    #[test]
    fn stopping_connection_cancels_and_waits_for_dns_repair() {
        let (manager, _) = gateway_fixture();
        let (started_tx, started_rx) = mpsc::channel();
        let repair_manager = Arc::clone(&manager);
        let repair = thread::spawn(move || {
            repair_with(&repair_manager, |cancelled| {
                started_tx.send(()).unwrap();
                let deadline = Instant::now() + Duration::from_secs(2);
                while !cancelled.load(Ordering::Relaxed) {
                    assert!(Instant::now() < deadline, "Stop did not cancel DNS repair");
                    thread::sleep(Duration::from_millis(5));
                }
                Err("DNS repair was cancelled.".into())
            })
        });
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        stop_gateway_processes(&manager).unwrap();
        assert_eq!(
            repair.join().unwrap().unwrap_err(),
            "DNS repair was cancelled."
        );
        let state = manager.lock().unwrap();
        assert!(!state.connection_wanted);
        assert!(state.gateway.is_none());
        assert!(state.dns_repair.is_none());
        assert!(state.gateway_stopping.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn cancelling_repair_releases_busy_state_without_restarting_the_gateway() {
        let (manager, pid) = gateway_fixture();
        let result = repair_with(&manager, |_| {
            // The prompt runs outside the manager lock, keeping Stop responsive.
            assert!(manager.try_lock().unwrap().dns_repair.is_some());
            Err("DNS repair was cancelled.".into())
        });
        assert_eq!(result.unwrap_err(), "DNS repair was cancelled.");
        let state = manager.lock().unwrap();
        assert!(state.dns_repair.is_none());
        assert!(state.local_dns_error);
        assert!(state.connection_wanted);
        assert_eq!(
            state.gateway.as_ref().unwrap().gateway.runtime.child.id(),
            pid
        );
        assert_eq!(state.public_probe_generation, 1);
        assert!(state.public_checked_at.is_none());
    }
    #[cfg(target_os = "macos")]
    #[test]
    fn descendant_stderr_does_not_escape_timeout() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "/bin/sleep 1.2 &"]);
        let started = Instant::now();
        let result = run_cache_command(command, Duration::from_millis(50), &AtomicBool::new(false));
        assert!(result.is_ok());
        assert!(
            started.elapsed() < Duration::from_millis(400),
            "wait_with_output bypassed the deadline"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn timeout_reaps_descendant_processes() {
        let temporary = tempfile::tempdir().unwrap();
        let pidfile = temporary.path().join("child.pid");
        let mut command = Command::new("/bin/sh");
        command
            .args([
                "-c",
                "/bin/sleep 30 & printf '%s' \"$!\" > \"$1\"; wait",
                "audit",
            ])
            .arg(&pidfile);
        let result =
            run_cache_command(command, Duration::from_millis(150), &AtomicBool::new(false));
        let pid: i32 = fs::read_to_string(&pidfile).unwrap().parse().unwrap();
        let alive = unsafe { libc::kill(pid, 0) == 0 };
        if alive {
            unsafe {
                libc::kill(pid, libc::SIGTERM);
            }
        }
        assert!(result.is_err());
        assert!(!alive, "only the launcher was terminated");
    }

    #[cfg(unix)]
    #[test]
    fn repair_keeps_exclusion_during_verification() {
        let (manager, _) = gateway_fixture();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!(
            "https://127.0.0.1:{}",
            listener.local_addr().unwrap().port()
        );
        *manager
            .lock()
            .unwrap()
            .gateway
            .as_ref()
            .unwrap()
            .gateway
            .public_url
            .lock()
            .unwrap() = origin;
        let worker_manager = Arc::clone(&manager);
        let worker = thread::spawn(move || repair_with(&worker_manager, |_| Ok(())));
        let deadline = Instant::now() + Duration::from_secs(2);
        let stream = loop {
            if let Ok((stream, _)) = listener.accept() {
                break stream;
            }
            assert!(Instant::now() < deadline, "verification did not connect");
            thread::sleep(Duration::from_millis(5));
        };
        let still_running = !worker.is_finished();
        let busy = manager.lock().unwrap().dns_repair.is_some();
        let mut second_prompt = false;
        let second = repair_with(&manager, |_| {
            second_prompt = true;
            Err("synthetic second prompt".into())
        });
        drop(stream);
        let first = worker.join().unwrap();
        assert!(still_running);
        assert_eq!(second.unwrap_err(), "DNS repair is already in progress.");
        assert!(first.is_ok());
        assert!(
            busy && !second_prompt,
            "verification is outside the repair operation"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn cancellation_reaps_descendants_and_recognizes_native_cancel() {
        let mut native_cancel = Command::new("/bin/sh");
        native_cancel.args(["-c", "printf 'User canceled. (-128)' >&2; exit 1"]);
        assert_eq!(
            run_cache_command(
                native_cancel,
                Duration::from_secs(1),
                &AtomicBool::new(false)
            )
            .unwrap_err(),
            "DNS repair was cancelled."
        );
        let temporary = tempfile::tempdir().unwrap();
        let pidfile = temporary.path().join("child.pid");
        let mut command = Command::new("/bin/sh");
        command
            .args([
                "-c",
                "/bin/sleep 30 & printf '%s' \"$!\" > \"$1\"; wait",
                "test",
            ])
            .arg(&pidfile);
        let cancelled = Arc::new(AtomicBool::new(false));
        let signal = Arc::clone(&cancelled);
        let worker =
            thread::spawn(move || run_cache_command(command, Duration::from_secs(30), &signal));
        let deadline = Instant::now() + Duration::from_secs(2);
        let pid: i32 = loop {
            if let Some(pid) = fs::read_to_string(&pidfile)
                .ok()
                .and_then(|value| value.parse().ok())
            {
                break pid;
            }
            assert!(Instant::now() < deadline, "child did not start");
            thread::sleep(Duration::from_millis(5));
        };
        cancelled.store(true, Ordering::Relaxed);
        let result = worker.join().unwrap();
        let alive = unsafe { libc::kill(pid, 0) == 0 };
        if alive {
            unsafe {
                libc::kill(pid, libc::SIGTERM);
            }
        }
        assert_eq!(result.unwrap_err(), "DNS repair was cancelled.");
        assert!(!alive, "cancel left a descendant alive");
    }

    #[cfg(unix)]
    #[test]
    fn stopping_waits_for_dns_verification_and_returns_cancellation() {
        let (manager, _) = gateway_fixture();
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        *manager
            .lock()
            .unwrap()
            .gateway
            .as_ref()
            .unwrap()
            .gateway
            .public_url
            .lock()
            .unwrap() = format!(
            "https://127.0.0.1:{}",
            listener.local_addr().unwrap().port()
        );
        let worker_manager = Arc::clone(&manager);
        let repair = thread::spawn(move || repair_with(&worker_manager, |_| Ok(())));
        let deadline = Instant::now() + Duration::from_secs(2);
        let stream = loop {
            if let Ok((stream, _)) = listener.accept() {
                break stream;
            }
            assert!(Instant::now() < deadline, "verification did not connect");
            thread::sleep(Duration::from_millis(5));
        };
        let stop_manager = Arc::clone(&manager);
        let (done_tx, done_rx) = mpsc::channel();
        let stop = thread::spawn(move || {
            stop_gateway_processes(&stop_manager).unwrap();
            done_tx.send(()).unwrap();
        });
        while manager.lock().unwrap().connection_wanted {
            assert!(Instant::now() < deadline, "Stop did not begin");
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            done_rx.try_recv().is_err(),
            "Stop returned while verification was still blocked"
        );
        drop(stream);
        stop.join().unwrap();
        assert_eq!(
            repair.join().unwrap().unwrap_err(),
            "DNS repair was cancelled."
        );
        let state = manager.lock().unwrap();
        assert!(state.gateway.is_none());
        assert!(state.dns_repair.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn parallel_stops_wait_for_the_same_cleanup() {
        for iteration in 0..100 {
            let manager = Arc::new(Mutex::new(RuntimeManager::new()));
            let pending = Arc::new(PendingOperation::default());
            manager.lock().unwrap().dns_repair = Some(Arc::clone(&pending));
            let barrier = Arc::new(std::sync::Barrier::new(16));
            let (tx, rx) = mpsc::channel();
            let workers = (0..16)
                .map(|_| {
                    let manager = Arc::clone(&manager);
                    let barrier = Arc::clone(&barrier);
                    let tx = tx.clone();
                    thread::spawn(move || {
                        barrier.wait();
                        stop_gateway_processes(&manager).unwrap();
                        tx.send(()).unwrap();
                    })
                })
                .collect::<Vec<_>>();
            let returned_early = rx.recv_timeout(Duration::from_millis(10)).is_ok();
            pending.finish();
            for worker in workers {
                worker.join().unwrap();
            }
            if returned_early {
                panic!("a concurrent Stop returned before DNS cleanup completed, iteration={iteration}");
            }
        }
    }
}
