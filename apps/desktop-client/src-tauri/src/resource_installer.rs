use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;

const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(240);
const UV_VERSION: &str = "0.11.28";
const CLOUDFLARED_VERSION: &str = "2026.8.3";

#[derive(Clone, Copy)]
struct Asset {
    url: &'static str,
    sha256: &'static str,
}

fn asset(tool: &str) -> Result<Asset, String> {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        match tool {
            "uv" => Ok(Asset {
                url: "https://github.com/astral-sh/uv/releases/download/0.11.28/uv-aarch64-apple-darwin.tar.gz",
                sha256: "33540eb7c883ab857eff79bd5ac2aa31fe27b595abecb4a9c003a2c998447232",
            }),
            "cloudflared" => Ok(Asset {
                url: "https://github.com/cloudflare/cloudflared/releases/download/2026.8.3/cloudflared-darwin-arm64.tgz",
                sha256: "40c9144d86df8937c5b43293a1f7d2d2107029aa74725023dd46b1b27154352f",
            }),
            _ => Err(format!("Unknown managed resource {tool:?}.")),
        }
    }
    #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
    {
        match tool {
            "uv" => Ok(Asset {
                url: "https://github.com/astral-sh/uv/releases/download/0.11.28/uv-x86_64-apple-darwin.tar.gz",
                sha256: "2ad79983127ffca7d77b77ce6a24278d7e4f7b817a1acf72fea5f8124b4aac5e",
            }),
            "cloudflared" => Ok(Asset {
                url: "https://github.com/cloudflare/cloudflared/releases/download/2026.8.3/cloudflared-darwin-amd64.tgz",
                sha256: "61e1316266a00fd70ce40da011d612badc805367fb65293dd1925f938f704c99",
            }),
            _ => Err(format!("Unknown managed resource {tool:?}.")),
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = tool;
        Err("Automatic uv/cloudflared installation is currently provided by the macOS desktop app. Install the tool with your platform package manager and retry.".into())
    }
}

pub(crate) fn tools_bin(data_dir: &Path) -> PathBuf {
    data_dir.join("tools").join("bin")
}

pub(crate) fn managed_version(tool: &str) -> Option<&'static str> {
    match tool {
        "uv" => Some(UV_VERSION),
        "cloudflared" => Some(CLOUDFLARED_VERSION),
        _ => None,
    }
}

pub(crate) fn managed_path(tool: &str, data_dir: &Path) -> PathBuf {
    tools_bin(data_dir).join(tool)
}

pub(crate) fn is_managed_installed(tool: &str, data_dir: &Path) -> bool {
    let path = managed_path(tool, data_dir);
    if !path.is_file() {
        return false;
    }
    let Some(expected) = managed_version(tool) else {
        return false;
    };
    Command::new(&path)
        .arg("--version")
        .output()
        .ok()
        .filter(|result| result.status.success())
        .map(|result| {
            let text = format!(
                "{}{}",
                String::from_utf8_lossy(&result.stdout),
                String::from_utf8_lossy(&result.stderr)
            );
            text.contains(expected)
        })
        .unwrap_or(false)
}

fn run_cancellable(mut command: Command, cancelled: &AtomicBool) -> Result<(), String> {
    command.stdin(Stdio::null());
    let mut child = command.spawn().map_err(|e| e.to_string())?;
    let deadline = Instant::now() + DOWNLOAD_TIMEOUT;
    loop {
        if cancelled.load(Ordering::Relaxed) {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Workspace startup was cancelled.".into());
        }
        match child.try_wait() {
            Ok(Some(status)) if status.success() => return Ok(()),
            Ok(Some(status)) => {
                return Err(format!(
                    "Managed resource installer exited with status {status}."
                ));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(e.to_string());
            }
            Ok(None) => {}
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Managed resource download exceeded four minutes.".into());
        }
        thread::sleep(Duration::from_millis(100));
    }
}

fn sha256(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 128];
    loop {
        let count = file.read(&mut buffer).map_err(|e| e.to_string())?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn find_named(root: &Path, name: &str) -> Option<PathBuf> {
    let entries = fs::read_dir(root).ok()?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.file_name().and_then(|x| x.to_str()) == Some(name) && path.is_file() {
            return Some(path);
        }
        if path.is_dir() {
            if let Some(found) = find_named(&path, name) {
                return Some(found);
            }
        }
    }
    None
}

#[cfg(unix)]
fn make_executable(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let mut permissions = fs::metadata(path).map_err(|e| e.to_string())?.permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions).map_err(|e| e.to_string())
}

#[cfg(not(unix))]
fn make_executable(_path: &Path) -> Result<(), String> {
    Ok(())
}

fn install_binary(source: &Path, destination: &Path) -> Result<(), String> {
    let parent = destination
        .parent()
        .ok_or_else(|| "Managed tool destination has no parent directory.".to_string())?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        destination
            .file_name()
            .and_then(|x| x.to_str())
            .unwrap_or("tool"),
        Uuid::new_v4()
    ));
    fs::copy(source, &temporary).map_err(|e| e.to_string())?;
    make_executable(&temporary)?;
    if destination.exists() {
        fs::remove_file(destination).map_err(|e| e.to_string())?;
    }
    fs::rename(temporary, destination).map_err(|e| e.to_string())
}

pub(crate) fn install(
    tool: &str,
    data_dir: &Path,
    cancelled: &AtomicBool,
) -> Result<PathBuf, String> {
    let asset = asset(tool)?;
    let staging =
        data_dir
            .join("tools")
            .join("staging")
            .join(format!("{}-{}", tool, Uuid::new_v4()));
    fs::create_dir_all(&staging).map_err(|e| e.to_string())?;
    let archive = staging.join("download.tar.gz");
    let result = (|| {
        let mut curl = Command::new("/usr/bin/curl");
        curl.args([
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "15",
            "--max-time",
            "220",
            "--output",
        ])
        .arg(&archive)
        .arg(asset.url);
        run_cancellable(curl, cancelled).map_err(|e| format!("Could not download {tool}: {e}"))?;

        let actual = sha256(&archive)?;
        if actual != asset.sha256 {
            return Err(format!(
                "Downloaded {tool} failed SHA-256 verification (expected {}, got {}).",
                asset.sha256, actual
            ));
        }

        let extracted = staging.join("extracted");
        fs::create_dir_all(&extracted).map_err(|e| e.to_string())?;
        let mut tar = Command::new("/usr/bin/tar");
        tar.arg("-xzf").arg(&archive).arg("-C").arg(&extracted);
        run_cancellable(tar, cancelled).map_err(|e| format!("Could not unpack {tool}: {e}"))?;

        let source = find_named(&extracted, tool).ok_or_else(|| {
            format!("Downloaded {tool} archive did not contain the expected executable.")
        })?;
        let destination = tools_bin(data_dir).join(tool);
        install_binary(&source, &destination)?;
        if tool == "uv" {
            if let Some(uvx) = find_named(&extracted, "uvx") {
                install_binary(&uvx, &tools_bin(data_dir).join("uvx"))?;
            }
        }
        fs::create_dir_all(data_dir.join("tools").join("versions")).map_err(|e| e.to_string())?;
        if let Some(version) = managed_version(tool) {
            fs::write(
                data_dir.join("tools").join("versions").join(tool),
                format!("{version}\n"),
            )
            .map_err(|e| e.to_string())?;
        }
        Ok(destination)
    })();
    let _ = fs::remove_dir_all(&staging);
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn managed_versions_are_pinned() {
        assert_eq!(managed_version("uv"), Some("0.11.28"));
        assert_eq!(managed_version("cloudflared"), Some("2026.8.3"));
    }

    #[test]
    fn managed_tools_live_inside_app_data() {
        assert_eq!(tools_bin(Path::new("data")), Path::new("data/tools/bin"));
        assert_eq!(
            managed_path("uv", Path::new("data")),
            Path::new("data/tools/bin/uv")
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "downloads pinned uv and cloudflared release assets"]
    fn downloads_and_verifies_pinned_macos_tools() {
        let temporary = tempfile::tempdir().unwrap();
        let cancelled = AtomicBool::new(false);
        let uv = install("uv", temporary.path(), &cancelled).unwrap();
        let cloudflared = install("cloudflared", temporary.path(), &cancelled).unwrap();
        assert!(uv.is_file());
        assert!(cloudflared.is_file());

        let uv_version = Command::new(&uv).arg("--version").output().unwrap();
        assert!(uv_version.status.success());
        assert!(String::from_utf8_lossy(&uv_version.stdout).contains(UV_VERSION));

        let cloudflared_version = Command::new(&cloudflared)
            .arg("--version")
            .output()
            .unwrap();
        assert!(cloudflared_version.status.success());
        assert!(String::from_utf8_lossy(&cloudflared_version.stdout).contains(CLOUDFLARED_VERSION));
    }
}
