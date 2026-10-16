//! Connection recovery and cached public health checks, independent of the panel.
#[cfg(test)]
use super::public_health::public_card_matches;
use super::public_health::PublicHealth;
use super::*;

/// Catch normal process termination before the OS bypasses Rust destructors.
/// The listener sleeps on a pipe; cleanup runs outside the signal handler so
/// pending startups can be cancelled and all owned process groups can be reaped.
#[cfg(unix)]
pub fn install_termination_handler(
    manager: Arc<Mutex<RuntimeManager>>,
    on_exit: impl FnOnce() + Send + 'static,
) -> std::io::Result<()> {
    use signal_hook::consts::{SIGINT, SIGTERM};
    use signal_hook::iterator::Signals;
    let mut signals = Signals::new([SIGTERM, SIGINT])?;
    thread::Builder::new()
        .name("desktop-termination".into())
        .spawn(move || {
            if signals.forever().next().is_some() {
                let _ = shutdown(&manager);
                on_exit();
            }
        })?;
    Ok(())
}

impl RuntimeManager {
    pub fn gateway_is_busy(&self) -> bool {
        self.gateway.is_some()
            || self.gateway_preparing.is_some()
            || self.gateway_stopping.is_some()
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down
    }
}

fn retry_delay(failures: u32) -> Duration {
    Duration::from_secs((5_u64 * 2_u64.pow(failures.saturating_sub(1).min(6))).min(300))
}

/// Recover only connections the user started (or explicitly opted to resume).
/// Manual stop cancels the shared PendingOperation before awaiting its cleanup.
pub fn maintain_connection(
    manager: &Arc<Mutex<RuntimeManager>>,
    profiles: &[WorkspaceProfile],
    config: &GatewayConfig,
) -> Result<(), String> {
    let Some(profile) = profiles.first() else {
        return Ok(());
    };
    let restart_gateway = {
        let mut state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?;
        if state.shutting_down
            || !state.connection_wanted
            || state.gateway_preparing.is_some()
            || state.gateway_stopping.is_some()
            || state
                .recovery_after
                .is_some_and(|deadline| Instant::now() < deadline)
        {
            return Ok(());
        }
        !state.gateway_is_active()
    };
    if restart_gateway {
        let result = start_gateway_inner(manager, profiles, config, true).map(|_| ());
        record_recovery(manager, &result);
        return result;
    }
    let repair = {
        let mut state = manager
            .lock()
            .map_err(|_| "Runtime manager is unavailable")?;
        let data = state.data_dir.clone();
        let Some(session) = state.gateway.as_mut() else {
            return Ok(());
        };
        let tunnel_dead = profile.tunnel.r#type == "cloudflare"
            && !session
                .gateway
                .tunnel
                .as_mut()
                .is_some_and(ManagedChild::is_running);
        if tunnel_dead {
            let url = Arc::clone(&session.gateway.public_url);
            let mut tunnel_profile = profile.clone();
            tunnel_profile.path = data.join("gateway-control").to_string_lossy().into_owned();
            let operation = Arc::new(PendingOperation::default());
            state.gateway_preparing = Some(Arc::clone(&operation));
            Some((tunnel_profile, data, url, operation))
        } else {
            None
        }
    };
    if let Some((profile, data, url, operation)) = repair {
        let result = spawn_cloudflare(
            &profile,
            &data.join("logs/gateway"),
            url,
            &data,
            &operation.cancelled,
        );
        let mut state = manager.lock().unwrap_or_else(|e| e.into_inner());
        let owns = state
            .gateway_preparing
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, &operation));
        let accepted =
            owns && state.connection_wanted && !operation.cancelled.load(Ordering::Relaxed);
        let outcome = match result {
            Ok(child) if accepted && state.gateway.is_some() => {
                state.gateway.as_mut().unwrap().gateway.tunnel = Some(child);
                state.public_healthy = None;
                state.public_error = None;
                state.public_checked_at = None;
                Ok(())
            }
            Ok(mut child) => {
                drop(state);
                child.terminate();
                state = manager.lock().unwrap_or_else(|e| e.into_inner());
                Ok(())
            }
            Err(error) => Err(error),
        };
        if owns {
            state.gateway_preparing = None;
        }
        drop(state);
        operation.finish();
        record_recovery(manager, &outcome);
        outcome?;
    }
    check_public_endpoint(manager);
    Ok(())
}

fn record_recovery(manager: &Arc<Mutex<RuntimeManager>>, result: &Result<(), String>) {
    let mut state = manager.lock().unwrap_or_else(|e| e.into_inner());
    if result.is_ok() {
        state.recovery_failures = 0;
        state.recovery_after = None;
    } else {
        state.recovery_failures = state.recovery_failures.saturating_add(1);
        state.recovery_after = Some(Instant::now() + retry_delay(state.recovery_failures));
    }
}

fn check_public_endpoint(manager: &Arc<Mutex<RuntimeManager>>) {
    let target = {
        let mut state = manager.lock().unwrap_or_else(|e| e.into_inner());
        if !state.connection_wanted
            || state
                .public_checked_at
                .is_some_and(|at| at.elapsed() < Duration::from_secs(30))
        {
            return;
        }
        let Some(session) = state.gateway.as_ref() else {
            return;
        };
        let origin = session
            .gateway
            .public_url
            .lock()
            .map(|url| url.clone())
            .unwrap_or_default();
        if !origin.starts_with("https://") {
            return;
        }
        let identity = (
            origin,
            session.gateway.runtime.child.id(),
            session.gateway.server_name.clone(),
        );
        state.public_checked_at = Some(Instant::now());
        identity
    };
    let health = public_health::probe(&target.0, &target.2);
    let mut state = manager.lock().unwrap_or_else(|e| e.into_inner());
    if state.gateway.as_ref().is_some_and(|session| {
        session.gateway.runtime.child.id() == target.1
            && session
                .gateway
                .public_url
                .lock()
                .is_ok_and(|url| *url == target.0)
    }) {
        state.public_healthy = Some(health == PublicHealth::Healthy);
        state.public_error = health.error();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn recovery_is_bounded_and_manual_stop_stays_stopped() {
        assert_eq!(retry_delay(1), Duration::from_secs(5));
        assert_eq!(retry_delay(30), Duration::from_secs(300));
        let manager = Arc::new(Mutex::new(RuntimeManager::new()));
        maintain_connection(&manager, &[], &GatewayConfig::default()).unwrap();
        assert!(!manager.lock().unwrap().connection_wanted);
    }
    #[test]
    fn public_probe_requires_the_expected_mcp_server() {
        assert!(public_card_matches(
            br#"{"server":{"name":"expected"},"transport":{"endpoint":"/mcp"}}"#,
            "expected"
        ));
        assert!(!public_card_matches(
            br#"{"server":{"name":"other"},"transport":{"endpoint":"/mcp"}}"#,
            "expected"
        ));
        assert!(!public_card_matches(
            b"<html>Cloudflare error</html>",
            "expected"
        ));
    }
    #[test]
    fn named_token_is_passed_only_through_the_child_environment() {
        let temporary = tempfile::tempdir().unwrap();
        let mut profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        profile.tunnel.cloudflare_mode = "named".into();
        profile.tunnel.cloudflare_token = "synthetic-token".into();
        let command = cloudflare_command(&profile, Path::new("/test/cloudflared"));
        assert_eq!(command.get_args().collect::<Vec<_>>(), ["tunnel", "run"]);
        assert!(command.get_envs().any(|(key, value)| key == "TUNNEL_TOKEN"
            && value == Some(std::ffi::OsStr::new("synthetic-token"))));
    }

    #[test]
    fn failed_gateway_preparation_releases_its_pending_operation() {
        let temporary = tempfile::tempdir().unwrap();
        let profile =
            WorkspaceProfile::new(temporary.path().to_string_lossy().into_owned(), 28766).unwrap();
        let manager = Arc::new(Mutex::new(RuntimeManager::new()));
        manager.lock().unwrap().configure_environment(
            temporary.path().join("missing"),
            temporary.path().join("data"),
        );
        assert!(start_gateway(
            &manager,
            std::slice::from_ref(&profile),
            &GatewayConfig::default()
        )
        .is_err());
        {
            let mut state = manager.lock().unwrap();
            assert!(state.gateway_preparing.is_none());
            assert!(!state.connection_wanted);
            assert_eq!(state.status(&profile).state, "error");
        }
        maintain_connection(
            &manager,
            std::slice::from_ref(&profile),
            &GatewayConfig::default(),
        )
        .unwrap();
        assert!(manager.lock().unwrap().gateway_preparing.is_none());
        stop_gateway(&manager, &profile).unwrap();
        {
            let mut state = manager.lock().unwrap();
            assert!(!state.connection_wanted);
            assert_eq!(state.status(&profile).state, "stopped");
        }
    }

    #[cfg(unix)]
    #[test]
    fn dead_tunnel_recovers_without_restarting_runtime_and_stop_prevents_recovery() {
        use std::os::unix::fs::PermissionsExt;
        let temporary = tempfile::tempdir().unwrap();
        let data = temporary.path();
        fs::create_dir_all(data.join("gateway-control")).unwrap();
        fs::create_dir_all(data.join("logs/gateway")).unwrap();
        let tools = resource_installer::tools_bin(data);
        fs::create_dir_all(&tools).unwrap();
        let executable = tools.join("cloudflared");
        fs::write(
            &executable,
            "#!/bin/sh\nprintf 'registered tunnel connection\\n' >&2\nexec /bin/sleep 60\n",
        )
        .unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let mut profile =
            WorkspaceProfile::new(data.to_string_lossy().into_owned(), 28766).unwrap();
        profile.tunnel.cloudflare_mode = "named".into();
        profile.tunnel.cloudflare_token = "synthetic-token".into();
        profile.tunnel.public_url = "https://example.test".into();
        let mut command = Command::new("/bin/sleep");
        command.arg("60");
        configure_process_group(&mut command);
        let child = command.spawn().unwrap();
        let runtime_pid = child.id();
        let manager = Arc::new(Mutex::new(RuntimeManager::new()));
        {
            let mut state = manager.lock().unwrap();
            state.data_dir = data.to_path_buf();
            state.connection_wanted = true;
            state.gateway = Some(ManagedGatewaySession {
                gateway: ManagedSession {
                    runtime: ManagedChild {
                        child,
                        group_id: runtime_pid,
                    },
                    tunnel: None,
                    public_url: Arc::new(Mutex::new(String::new())),
                    server_name: "test".into(),
                },
                projects: HashMap::new(),
                registry_path: data.join("registry.json"),
                resolved_runtime: ResolvedRuntime {
                    command: (PathBuf::new(), vec![]),
                    build_id: "test".into(),
                },
            });
        }
        let config = GatewayConfig::from_workspace_profile(&profile);
        maintain_connection(&manager, std::slice::from_ref(&profile), &config).unwrap();
        {
            let mut state = manager.lock().unwrap();
            let gateway = &mut state.gateway.as_mut().unwrap().gateway;
            assert_eq!(gateway.runtime.child.id(), runtime_pid);
            assert!(gateway.tunnel.as_mut().unwrap().is_running());
            gateway.tunnel.as_mut().unwrap().terminate();
        }
        maintain_connection(&manager, std::slice::from_ref(&profile), &config).unwrap();
        assert_eq!(
            manager
                .lock()
                .unwrap()
                .gateway
                .as_ref()
                .unwrap()
                .gateway
                .runtime
                .child
                .id(),
            runtime_pid
        );
        stop_gateway(&manager, &profile).unwrap();
        maintain_connection(&manager, std::slice::from_ref(&profile), &config).unwrap();
        assert!(manager.lock().unwrap().gateway.is_none());
    }
}
