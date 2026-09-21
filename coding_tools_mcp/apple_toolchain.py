from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: float = 5.0) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()[:20_000]


def _xcrun_find(xcrun: str | None, tool: str, *, cwd: Path, env: dict[str, str]) -> str | None:
    if not xcrun:
        return None
    output = _run([xcrun, "--find", tool], cwd=cwd, env=env, timeout=3.0)
    if not output:
        return None
    candidate = output.splitlines()[0].strip()
    return candidate if candidate and Path(candidate).is_file() else None


def probe(*, cwd: Path, env: dict[str, str], include_sdks: bool = False) -> dict[str, Any]:
    path_value = env.get("PATH") or env.get("Path") or ""
    is_macos = sys.platform == "darwin"
    xcrun = shutil.which("xcrun", path=path_value)
    xcodebuild = shutil.which("xcodebuild", path=path_value)
    xcode_select = shutil.which("xcode-select", path=path_value)
    swift = shutil.which("swift", path=path_value)
    sourcekit_lsp = shutil.which("sourcekit-lsp", path=path_value)
    codesign = shutil.which("codesign", path=path_value)
    brew = shutil.which("brew", path=path_value)
    git = shutil.which("git", path=path_value)

    developer_dir = _run([xcode_select, "-p"], cwd=cwd, env=env, timeout=3.0) if xcode_select else None
    xcode_version = _run([xcodebuild, "-version"], cwd=cwd, env=env, timeout=5.0) if xcodebuild else None
    swift_version = _run([swift, "--version"], cwd=cwd, env=env, timeout=5.0) if swift else None
    if sourcekit_lsp is None:
        sourcekit_lsp = _xcrun_find(xcrun, "sourcekit-lsp", cwd=cwd, env=env)
    notarytool = _xcrun_find(xcrun, "notarytool", cwd=cwd, env=env)
    xcresulttool = _xcrun_find(xcrun, "xcresulttool", cwd=cwd, env=env)

    sdk_summary: list[str] = []
    if include_sdks and xcodebuild:
        sdk_output = _run([xcodebuild, "-showsdks"], cwd=cwd, env=env, timeout=5.0)
        if sdk_output:
            sdk_summary = [line.strip() for line in sdk_output.splitlines() if "-sdk " in line][:50]

    return {
        "platform": "macos" if is_macos else sys.platform,
        "is_macos": is_macos,
        "macos_version": platform.mac_ver()[0] if is_macos else None,
        "architecture": platform.machine(),
        "developer_dir": developer_dir,
        "command_line_tools_available": bool(developer_dir),
        "xcodebuild": xcodebuild,
        "xcode_available": bool(xcodebuild and developer_dir),
        "xcode_version": xcode_version,
        "xcrun": xcrun,
        "swift": swift,
        "swift_available": bool(swift),
        "swift_version": swift_version,
        "sourcekit_lsp": sourcekit_lsp,
        "codesign": codesign,
        "notarytool": notarytool,
        "xcresulttool": xcresulttool,
        "homebrew": brew,
        "git": git,
        "sdk_summary": sdk_summary,
    }
