from __future__ import annotations

import json
import os
import shutil
import shlex
import socket
import subprocess
import sys
import time
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from .chrome_native_host import HOST_NAME, bridge_dir, socket_path
from .errors import ToolFailure


EXTENSION_ID = "dkjnlnmnanecacnjnpkhblkofmkaahai"
DEFAULT_TIMEOUT_MS = 5000


def _unsupported() -> ToolFailure:
    return ToolFailure(
        "UNSUPPORTED_PLATFORM",
        "Chrome Native Messaging integration is currently supported on macOS only.",
        category="runtime",
    )


def _manifest_path() -> Path:
    if sys.platform != "darwin":
        raise _unsupported()
    return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts" / f"{HOST_NAME}.json"


def _extension_install_dir() -> Path:
    return bridge_dir() / "chrome-extension"


def _bundled_host_candidate() -> Path | None:
    candidates: list[Path] = []
    # Resolving a venv's Python symlink would lose its adjacent entry points.
    executable = Path(sys.executable).absolute()
    candidates.append(executable.with_name("coding-tools-mcp-chrome-host"))
    candidates.append(executable.parent.parent / "coding-tools-mcp-chrome-host")
    candidates.append(
        executable.parent.parent
        / "coding-tools-mcp-chrome-host"
        / "coding-tools-mcp-chrome-host"
    )
    candidates.append(executable.parent.parent.parent / "coding-tools-mcp-chrome-host")
    candidates.append(
        executable.parent.parent.parent
        / "coding-tools-mcp-chrome-host"
        / "coding-tools-mcp-chrome-host"
    )
    found = shutil.which("coding-tools-mcp-chrome-host")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def install(args: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise _unsupported()
    candidate = args.get("host_path") or _bundled_host_candidate()
    if not candidate:
        # Module-only installations also work without a frozen native host.
        # Pin the interpreter and import root; Chrome does not inherit our PATH.
        host = bridge_dir() / "coding-tools-mcp-chrome-host"
        host.parent.mkdir(parents=True, exist_ok=True)
        code = (
            f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r}); "
            "from coding_tools_mcp.chrome_native_host import main; raise SystemExit(main())"
        )
        host.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", "-c", code]) + "\n", encoding="utf-8")
        host.chmod(0o700)
    else:
        host = Path(str(candidate)).expanduser()
    if not host.is_file():
        raise ToolFailure(
            "NOT_FOUND",
            "Could not find coding-tools-mcp-chrome-host. Build/install the bundled native host first.",
            category="not_found",
        )
    target = _extension_install_dir()
    target.mkdir(parents=True, exist_ok=True)
    source = resources.files("coding_tools_mcp").joinpath("chrome_extension")
    for name in ("manifest.json", "service_worker.js"):
        data = source.joinpath(name).read_bytes()
        (target / name).write_bytes(data)

    manifest = {
        "name": HOST_NAME,
        "description": "Coding Tools MCP Chrome bridge",
        "path": str(host.resolve()),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    manifest_path = _manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if bool(args.get("open_extensions_page", True)):
        subprocess.run(
            ["/usr/bin/open", "-a", "Google Chrome", "chrome://extensions/"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return {
        "ok": True,
        "extension_id": EXTENSION_ID,
        "extension_path": str(target),
        "native_host_manifest": str(manifest_path),
        "native_host_path": str(host.resolve()),
        "next_step": "Enable Developer mode, then Load unpacked and select extension_path. This one-time Chrome step is required by current stable Chrome.",
    }


def _request(action: str, params: dict[str, Any] | None = None, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise _unsupported()
    path = socket_path()
    if not path.exists():
        raise ToolFailure(
            "CHROME_EXTENSION_UNAVAILABLE",
            "Chrome extension bridge is not connected. Install/load the Coding Tools MCP extension and keep Chrome running.",
            category="runtime",
            retryable=True,
            details={"socket": str(path), "extension_id": EXTENSION_ID},
        )
    request_id = uuid.uuid4().hex
    payload = {"id": request_id, "action": action, "params": {**(params or {}), "timeoutMs": timeout_ms}}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_ms / 1000)
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        client.connect(str(path))
        client.sendall((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        chunks = bytearray()
        while b"\n" not in chunks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("Chrome request deadline exceeded")
            client.settimeout(remaining)
            part = client.recv(65536)
            if not part:
                break
            chunks.extend(part)
            if len(chunks) > 8 * 1024 * 1024:
                raise ToolFailure("OUTPUT_TOO_LARGE", "Chrome extension response exceeded 8 MiB.", category="runtime")
    except socket.timeout as exc:
        raise ToolFailure(
            "CHROME_EXTENSION_UNAVAILABLE",
            "Chrome extension bridge timed out.",
            category="runtime",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise ToolFailure(
            "CHROME_EXTENSION_UNAVAILABLE",
            f"Could not connect to Chrome extension bridge: {exc}",
            category="runtime",
            retryable=True,
        ) from exc
    finally:
        client.close()
    if not chunks:
        raise ToolFailure("CHROME_EXTENSION_UNAVAILABLE", "Chrome extension bridge returned no response.", category="runtime")
    response = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
    if not response.get("ok", False):
        raise ToolFailure(
            "CHROME_EXTENSION_ERROR",
            str(response.get("error") or "Chrome extension request failed."),
            category="runtime",
            details={"action": action, "response": response},
        )
    return response


def status(args: dict[str, Any]) -> dict[str, Any]:
    manifest = _manifest_path() if sys.platform == "darwin" else None
    base = {
        "ok": True,
        "platform_supported": sys.platform == "darwin",
        "extension_id": EXTENSION_ID,
        "extension_path": str(_extension_install_dir()) if sys.platform == "darwin" else None,
        "native_host_manifest": str(manifest) if manifest else None,
        "manifest_installed": bool(manifest and manifest.is_file()),
        "bridge_socket": str(socket_path()),
        "bridge_connected": socket_path().exists(),
    }
    if socket_path().exists():
        try:
            response = _request("status", timeout_ms=int(args.get("timeout_ms", DEFAULT_TIMEOUT_MS)))
            base["bridge"] = response.get("result")
        except ToolFailure as exc:
            base["bridge_connected"] = False
            base["bridge_error"] = exc.message
    return base


def extensions(args: dict[str, Any]) -> dict[str, Any]:
    response = _request("extensions", timeout_ms=int(args.get("timeout_ms", DEFAULT_TIMEOUT_MS)))
    items = response.get("result") or []
    query = str(args.get("query") or "").casefold()
    if query:
        items = [item for item in items if query in str(item.get("name", "")).casefold() or query in str(item.get("id", "")).casefold()]
    limit = int(args.get("max_results", 200))
    return {"ok": True, "extensions": items[:limit], "count": min(len(items), limit), "truncated": len(items) > limit}


def tabs(args: dict[str, Any]) -> dict[str, Any]:
    response = _request("tabs", timeout_ms=int(args.get("timeout_ms", DEFAULT_TIMEOUT_MS)))
    items = response.get("result") or []
    return {"ok": True, "tabs": items, "count": len(items)}


def execute(args: dict[str, Any]) -> dict[str, Any]:
    params = {"tabId": int(args["tab_id"]), "script": str(args["script"]), "timeoutMs": int(args.get("timeout_ms", DEFAULT_TIMEOUT_MS))}
    response = _request("execute", params, timeout_ms=int(args.get("timeout_ms", DEFAULT_TIMEOUT_MS)))
    return {"ok": True, "tab_id": params["tabId"], "result": response.get("result")}


def send(args: dict[str, Any]) -> dict[str, Any]:
    params = {"extensionId": str(args["extension_id"]), "message": args.get("message")}
    response = _request("send", params, timeout_ms=int(args.get("timeout_ms", DEFAULT_TIMEOUT_MS)))
    return {"ok": True, "extension_id": params["extensionId"], "result": response.get("result")}
