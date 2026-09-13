//! Resolve an existing installation or prepare a private, persistent environment.
//! Only our wheel and hashed production requirements are shipped in the app.
use super::{configure_process_group, ManagedChild};
use crate::resource_installer;
use fs2::FileExt;
use serde::Deserialize;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const SETUP_TIMEOUT: Duration = Duration::from_secs(300);
const PROBE_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Deserialize)]
struct Package {
    version: String,
    wheel: String,
    environment_key: String,
}

impl Package {
    fn read(directory: &Path) -> Result<Self, String> {
        let data = fs::read(directory.join("manifest.json")).map_err(|e| {
            format!("Runtime source package is missing. Reinstall the desktop app: {e}")
        })?;
        let package: Self = serde_json::from_slice(&data).map_err(|e| e.to_string())?;
        if !safe_component(&package.environment_key)
            || !safe_component(&package.wheel)
            || !package.wheel.starts_with("coding_tools_mcp-")
            || !package.wheel.ends_with("-py3-none-any.whl")
            || !directory.join(&package.wheel).is_file()
            || !directory.join("requirements.txt").is_file()
        {
            return Err("Invalid desktop runtime source package. Reinstall the app.".into());
        }
        Ok(package)
    }
}

fn safe_component(value: &str) -> bool {
    !value.is_empty()
        && value != "."
        && value != ".."
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
}

fn python_in(environment: &Path) -> PathBuf {
    environment.join(if cfg!(windows) {
        "Scripts/python.exe"
    } else {
        "bin/python"
    })
}

fn python_command(python: PathBuf) -> (PathBuf, Vec<String>) {
    (
        python,
        vec!["-I".into(), "-m".into(), "coding_tools_mcp".into()],
    )
}

fn probe(mut command: Command) -> Option<String> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    configure_process_group(&mut command);
    let child = command.spawn().ok()?;
    let group_id = child.id();
    let mut process = ManagedChild { child, group_id };
    let deadline = Instant::now() + PROBE_TIMEOUT;
    loop {
        if let Some(status) = process.child.try_wait().ok()? {
            if !status.success() {
                return None;
            }
            let mut output = String::new();
            process
                .child
                .stdout
                .take()?
                .take(4096)
                .read_to_string(&mut output)
                .ok()?;
            return Some(output.trim().into());
        }
        if Instant::now() >= deadline {
            process.terminate();
            return None;
        }
        thread::sleep(Duration::from_millis(25));
    }
}

fn usable_python(python: &Path, version: &str) -> bool {
    let mut command = Command::new(python);
    command.args(["-I", "-c", "import sys; import coding_tools_mcp, jwt; assert sys.version_info >= (3,11); print(coding_tools_mcp.__version__)"]);
    probe(command).as_deref() == Some(version)
}

pub(super) fn readiness(resources: &Path, data: &Path, _path: &str) -> (bool, Option<String>) {
    let package_dir = resources.join("runtime");
    let package = match Package::read(&package_dir) {
        Ok(package) => package,
        Err(_) => return (false, None),
    };
    let target = data.join("runtimes").join(&package.environment_key);
    let python = python_in(&target);
    let ready = target.join(".ready");
    let marker_matches = fs::read_to_string(&ready)
        .ok()
        .is_some_and(|value| value.trim() == package.version);
    if marker_matches && python.is_file() {
        return (true, Some(package.version));
    }
    (false, Some(package.version))
}

pub(super) fn reset_managed(resources: &Path, data: &Path) -> Result<bool, String> {
    let package = Package::read(&resources.join("runtime"))?;
    let target = data.join("runtimes").join(package.environment_key);
    if !target.exists() {
        return Ok(false);
    }
    fs::remove_dir_all(&target).map_err(|e| {
        format!(
            "Could not remove the managed runtime at {}: {e}",
            target.display()
        )
    })?;
    Ok(true)
}

fn run_step(
    mut command: Command,
    log: &Path,
    deadline: Instant,
    cancelled: &AtomicBool,
) -> Result<(), String> {
    let output = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log)
        .map_err(|e| e.to_string())?;
    command
        .stdout(output.try_clone().map_err(|e| e.to_string())?)
        .stderr(output)
        .stdin(Stdio::null());
    configure_process_group(&mut command);
    let child = command
        .spawn()
        .map_err(|e| format!("Could not prepare the runtime: {e}"))?;
    let group_id = child.id();
    let mut process = ManagedChild { child, group_id };
    loop {
        if cancelled.load(Ordering::Relaxed) {
            process.terminate();
            return Err("Workspace startup was cancelled.".into());
        }
        match process.child.try_wait() {
            Ok(Some(status)) if status.success() => return Ok(()),
            Ok(Some(_)) => return Err(format!("Runtime preparation failed. Check your network connection and {}. Install uv from Resources if Python has no venv/pip support, then retry.", log.display())),
            Err(error) => {
                process.terminate();
                return Err(error.to_string());
            }
            _ => {}
        }
        if Instant::now() >= deadline {
            process.terminate();
            return Err(format!("Runtime preparation exceeded five minutes. Check your connection and {}, then retry.", log.display()));
        }
        thread::sleep(Duration::from_millis(100));
    }
}

pub(super) fn resolve(
    resources: &Path,
    data: &Path,
    log_dir: &Path,
    path: &str,
    cancelled: &AtomicBool,
) -> Result<(PathBuf, Vec<String>), String> {
    if let Ok(explicit) = std::env::var("CODING_TOOLS_MCP_DESKTOP_RUNTIME") {
        return Ok((PathBuf::from(explicit), vec![]));
    }
    let package_dir = resources.join("runtime");
    let package = Package::read(&package_dir)?;
    let managed_bin = resource_installer::tools_bin(data);
    let find = |name: &str| {
        let managed = managed_bin.join(name);
        if managed.is_file() {
            Some(managed)
        } else {
            which::which_in(name, Some(path), resources).ok()
        }
    };
    if let Some(program) = find("coding-tools-mcp") {
        let mut command = Command::new(&program);
        command.arg("--version").current_dir(resources);
        if probe(command)
            .is_some_and(|s| s.split_whitespace().last() == Some(package.version.as_str()))
        {
            return Ok((program, vec![]));
        }
    }
    for name in ["python3", "python"] {
        if let Some(python) = find(name) {
            if usable_python(&python, &package.version) {
                return Ok(python_command(python));
            }
        }
    }
    let environments = data.join("runtimes");
    let target = environments.join(&package.environment_key);
    let python = python_in(&target);
    let ready = target.join(".ready");
    if ready.is_file() && usable_python(&python, &package.version) {
        return Ok(python_command(python));
    }
    fs::create_dir_all(&environments).map_err(|e| e.to_string())?;
    let deadline = Instant::now() + SETUP_TIMEOUT;
    // An OS lock is released even after a crash; two app instances cannot
    // install into the same environment concurrently.
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(environments.join(format!("{}.lock", package.environment_key)))
        .map_err(|e| e.to_string())?;
    loop {
        if cancelled.load(Ordering::Relaxed) {
            return Err("Workspace startup was cancelled.".into());
        }
        match lock.try_lock_exclusive() {
            Ok(()) => break,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    return Err(
                        "Another app is preparing the runtime. Please retry shortly.".into(),
                    );
                }
                thread::sleep(Duration::from_millis(100));
            }
            Err(e) => return Err(e.to_string()),
        }
    }
    if ready.is_file() && usable_python(&python, &package.version) {
        return Ok(python_command(python));
    }
    let log = log_dir.join("setup.log");
    writeln!(
        File::create(&log).map_err(|e| e.to_string())?,
        "Preparing a private runtime. Existing system packages are not modified."
    )
    .map_err(|e| e.to_string())?;
    let mut uv = find("uv");
    if uv.is_none() {
        // A compatible system Python is still preferred because it avoids a
        // download. When neither Python nor uv can create the private runtime,
        // bootstrap the pinned uv release into the app-owned tools directory.
        let has_venv_python = ["python3", "python"]
            .into_iter()
            .filter_map(find)
            .any(|python| {
                let mut command = Command::new(python);
                command.args([
                    "-I",
                    "-c",
                    "import sys, venv; assert sys.version_info >= (3,11)",
                ]);
                probe(command).is_some()
            });
        if !has_venv_python {
            uv = Some(resource_installer::install("uv", data, cancelled)?);
        }
    }
    let created = target.join(".created");
    if !python.is_file() || !created.is_file() {
        // Only incomplete app-owned environments are rebuilt. Installation
        // retries must not inherit a half-created venv without pip or scripts.
        if target.exists() {
            fs::remove_dir_all(&target).map_err(|e| e.to_string())?;
        }
        let mut create = if let Some(uv) = &uv {
            let mut command = Command::new(uv);
            command.args([
                "venv",
                "--no-config",
                "--python",
                ">=3.11",
                "--python-preference",
                "system",
            ]);
            command
        } else {
            let system_python = ["python3", "python"].into_iter().filter_map(find).find(|python| {
                let mut command = Command::new(python);
                command.args(["-I", "-c", "import sys, venv; assert sys.version_info >= (3,11)"]);
                probe(command).is_some()
            }).ok_or("No compatible Python was found and automatic uv installation was unavailable. Check your network connection and retry.")?;
            let mut command = Command::new(system_python);
            command.args(["-I", "-m", "venv"]);
            command
        };
        create
            .arg(&target)
            .env("PATH", path)
            .current_dir(&package_dir);
        run_step(create, &log, deadline, cancelled)?;
        fs::write(&created, b"created").map_err(|e| e.to_string())?;
    }
    let installer = || {
        let mut command = if let Some(uv) = &uv {
            let mut c = Command::new(uv);
            c.args(["pip", "install", "--no-config", "--python"])
                .arg(&python);
            c
        } else {
            let mut c = Command::new(&python);
            c.args([
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
            ]);
            c
        };
        command.env("PATH", path).current_dir(&package_dir);
        command
    };
    let mut dependencies = installer();
    dependencies
        .args(["--require-hashes", "--only-binary=:all:", "-r"])
        .arg(package_dir.join("requirements.txt"));
    run_step(dependencies, &log, deadline, cancelled)?;
    let mut install = installer();
    install
        .arg("--no-deps")
        .arg(package_dir.join(&package.wheel));
    run_step(install, &log, deadline, cancelled)?;
    if !usable_python(&python, &package.version) {
        return Err(format!(
            "The runtime was installed but its health check failed. See {}.",
            log.display()
        ));
    }
    fs::write(ready, package.version).map_err(|e| e.to_string())?;
    Ok(python_command(python))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_rejects_paths_outside_the_runtime_package() {
        let directory = tempfile::tempdir().unwrap();
        fs::write(
            directory.path().join("manifest.json"),
            r#"{"version":"0.3.4","wheel":"../escape.whl","environment_key":"../outside"}"#,
        )
        .unwrap();
        assert!(Package::read(directory.path()).is_err());
    }

    #[test]
    fn environment_paths_keep_directory_and_executable_distinct() {
        let python = python_in(Path::new("runtime-123"));
        assert_eq!(
            python,
            Path::new("runtime-123").join(if cfg!(windows) {
                "Scripts/python.exe"
            } else {
                "bin/python"
            })
        );
    }

    #[test]
    fn missing_source_package_has_an_actionable_error() {
        let directory = tempfile::tempdir().unwrap();
        let error = Package::read(directory.path()).err().unwrap();
        assert!(error.contains("Reinstall"));
    }

    #[test]
    fn failed_setup_is_not_treated_as_success() {
        let directory = tempfile::tempdir().unwrap();
        let mut command = if cfg!(windows) {
            Command::new("cmd")
        } else {
            Command::new("sh")
        };
        command.args(if cfg!(windows) {
            vec!["/C", "exit 7"]
        } else {
            vec!["-c", "exit 7"]
        });
        let result = run_step(
            command,
            &directory.path().join("setup.log"),
            Instant::now() + Duration::from_secs(5),
            &AtomicBool::new(false),
        );
        assert!(result.unwrap_err().contains("preparation failed"));
    }

    #[cfg(unix)]
    #[test]
    fn cancelling_preparation_terminates_its_process() {
        let directory = tempfile::tempdir().unwrap();
        let cancelled = std::sync::Arc::new(AtomicBool::new(false));
        let signal = std::sync::Arc::clone(&cancelled);
        let setter = thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            signal.store(true, Ordering::Relaxed);
        });
        let mut command = Command::new("sh");
        command.args(["-c", "sleep 10"]);
        let start = Instant::now();
        let error = run_step(
            command,
            &directory.path().join("setup.log"),
            start + Duration::from_secs(15),
            &cancelled,
        )
        .unwrap_err();
        setter.join().unwrap();
        assert!(error.contains("cancelled"));
        assert!(start.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn missing_installers_offer_setup_instead_of_starting_an_old_runtime() {
        let temporary = tempfile::tempdir().unwrap();
        let resources = temporary.path().join("resources");
        let package = resources.join("runtime");
        fs::create_dir_all(&package).unwrap();
        fs::write(package.join("manifest.json"), r#"{"version":"0.0.0","wheel":"coding_tools_mcp-0.0.0-py3-none-any.whl","environment_key":"test-empty"}"#).unwrap();
        fs::write(package.join("coding_tools_mcp-0.0.0-py3-none-any.whl"), b"").unwrap();
        fs::write(package.join("requirements.txt"), b"").unwrap();
        let error = resolve(
            &resources,
            temporary.path(),
            temporary.path(),
            "",
            &AtomicBool::new(false),
        )
        .unwrap_err();
        assert!(error.contains("Install uv"));
    }

    #[cfg(unix)]
    #[test]
    #[ignore = "installs dependencies using system Python and pip without uv"]
    fn prepare_external_runtime_without_uv() {
        let resources = std::env::var_os("CODING_TOOLS_MCP_TEST_RUNTIME_RESOURCES")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources"));
        let temporary = tempfile::tempdir().unwrap();
        let tools = temporary.path().join("tools");
        let logs = temporary.path().join("logs");
        fs::create_dir_all(&tools).unwrap();
        fs::create_dir_all(&logs).unwrap();
        let installed = std::env::var_os("CODING_TOOLS_MCP_TEST_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| which::which("python3").unwrap());
        let python = fs::canonicalize(installed).unwrap();
        std::os::unix::fs::symlink(python, tools.join("python3")).unwrap();
        let result = resolve(
            &resources,
            temporary.path(),
            &logs,
            tools.to_str().unwrap(),
            &AtomicBool::new(false),
        )
        .unwrap();
        assert!(result.0.starts_with(temporary.path().join("runtimes")));
        let installed = fs::read_to_string(logs.join("setup.log")).unwrap();
        assert!(installed.contains("Successfully installed"));
    }

    #[test]
    #[ignore = "downloads locked production dependencies into a temporary private environment"]
    fn prepare_and_reuse_external_runtime() {
        let resources = std::env::var_os("CODING_TOOLS_MCP_TEST_RUNTIME_RESOURCES")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources"));
        let temporary = tempfile::tempdir().unwrap();
        let tools = temporary.path().join("tools");
        let logs = temporary.path().join("logs");
        fs::create_dir_all(&tools).unwrap();
        fs::create_dir_all(&logs).unwrap();
        let uv = which::which("uv").expect("Install uv for this integration test");
        let copied = tools.join(if cfg!(windows) { "uv.exe" } else { "uv" });
        fs::copy(uv, &copied).unwrap();
        let path = tools.to_str().unwrap();
        let first = resolve(
            &resources,
            temporary.path(),
            &logs,
            path,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert!(first.0.starts_with(temporary.path().join("runtimes")));
        assert!(logs.join("setup.log").is_file());
        // Removing the only installer proves that reuse does not need uv,
        // a network request, or an installed system package.
        fs::remove_file(copied).unwrap();
        let second = resolve(
            &resources,
            temporary.path(),
            &logs,
            path,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(first, second);
        let reused = resolve(
            &resources,
            temporary.path(),
            &logs,
            second.0.parent().unwrap().to_str().unwrap(),
            &AtomicBool::new(false),
        )
        .unwrap();
        assert!(reused
            .0
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with("coding-tools-mcp"));
        let mut handshake = Command::new(&second.0);
        handshake.args(["-I", "-c", r#"
import json, os, subprocess, sys
requests = [
    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"desktop-smoke","version":"1"}}},
    {"jsonrpc":"2.0","method":"notifications/initialized"},
    {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
]
result = subprocess.run([sys.executable, '-I', '-m', 'coding_tools_mcp', '--stdio', '--workspace', sys.argv[1]], input=''.join(json.dumps(x)+'\n' for x in requests), text=True, capture_output=True, timeout=4, env={k:v for k,v in os.environ.items() if not k.startswith('CODING_TOOLS_MCP_')})
assert result.returncode == 0, result.stderr
responses = [json.loads(line) for line in result.stdout.splitlines()]
catalog = next(x['result']['tools'] for x in responses if x.get('id') == 2)
assert any(x['name'] == 'read_files' for x in catalog)
assert any(x['name'] == 'tool_search' for x in catalog)
assert any(x['name'] == 'runtime_doctor' for x in catalog)
assert any(x['name'] == 'hooks_status' for x in catalog)
assert any(x['name'] == 'shell_snapshot' for x in catalog)
print(len(catalog))
"#]).arg(temporary.path());
        assert_eq!(probe(handshake).as_deref(), Some("28"));
        let mut command = Command::new(&second.0);
        command.args([
            "-I",
            "-c",
            "import importlib.util; assert importlib.util.find_spec('cryptography') is None",
        ]);
        assert!(probe(command).is_some());
    }
}
