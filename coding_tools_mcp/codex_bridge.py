"""Optional Codex app-server capability broker.

This module deliberately treats Codex as a local capability provider, not as a
second agent.  The public invariant is stronger than a convention: model-turn
RPCs are rejected before they can be written to the app-server process.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .errors import ToolFailure


CODEX_APP_SERVER_ALLOWED_METHODS = frozenset({
    "initialize",
    "thread/start",
    "thread/read",
    "mcpServer/tool/call",
})
CODEX_MODEL_METHODS = frozenset({
    "turn/start",
    "turn/steer",
    "thread/compact/start",
})
CODEX_COMPUTER_ALLOWED_MCP_SERVERS = frozenset({"node_repl"})
CODEX_REQUIRED_SCHEMA_METHODS = frozenset({
    "initialize",
    "thread/start",
    "thread/read",
    "mcpServer/tool/call",
})
SENSITIVE_ENV_MARKERS = (
    "CODING_TOOLS_MCP_AUTH",
    "CODING_TOOLS_MCP_OAUTH",
    "CODING_TOOLS_MCP_SERVER_URL",
)


@dataclass(frozen=True)
class CodexInstallation:
    executable: Path
    version: str
    schema_hash: str | None = None
    schema_compatible: bool | None = None


@dataclass(frozen=True)
class CodexBridgeProbe:
    available: bool
    compatible: bool
    executable: str | None
    version: str | None
    schema_hash: str | None
    started_servers: tuple[str, ...]
    app_count: int | None
    model_turns_created: int | None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "compatible": self.compatible,
            "executable": self.executable,
            "version": self.version,
            "schema_hash": self.schema_hash,
            "started_servers": list(self.started_servers),
            "app_count": self.app_count,
            "model_invocation": "forbidden",
            "model_turns_created": self.model_turns_created,
            "billing_guarantee": "none",
            "reason": self.reason,
        }


ServerRequestHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


def discover_codex_executable() -> Path | None:
    """Find the installed Codex executable without relying on plugin cache paths."""

    candidates: list[Path] = []
    configured = os.environ.get("CODEX_CLI_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())
    # Desktop-backed capabilities (Computer Use / Browser) are coupled to the
    # ChatGPT app runtime.  Prefer that installation over an unrelated CLI on
    # PATH, which may be a different version and lack the host services needed
    # by those capabilities.
    if sys_platform_is_macos():
        candidates.extend([
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex",
        ])
    on_path = shutil.which("codex")
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    return None


def sys_platform_is_macos() -> bool:
    return os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


def codex_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolFailure(
            "CODEX_APP_SERVER_UNAVAILABLE",
            "Could not query the installed Codex version.",
            category="runtime",
            details={"executable": str(executable)},
        ) from exc
    value = (result.stdout or result.stderr).strip()
    if not value:
        raise ToolFailure("CODEX_APP_SERVER_UNAVAILABLE", "Codex returned an empty version.", category="runtime")
    return value


def generate_schema_fingerprint(executable: Path) -> tuple[str, bool]:
    """Generate the current app-server schema and verify the minimum broker RPCs."""

    with tempfile.TemporaryDirectory(prefix="ctm-codex-schema-") as temp:
        root = Path(temp)
        try:
            subprocess.run(
                [str(executable), "app-server", "generate-json-schema", "--experimental", "--out", str(root)],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolFailure(
                "CODEX_SCHEMA_INCOMPATIBLE",
                "Could not generate the Codex app-server protocol schema.",
                category="runtime",
            ) from exc
        digest = hashlib.sha256()
        corpus = bytearray()
        files = sorted(root.rglob("*.json"))
        if not files:
            raise ToolFailure("CODEX_SCHEMA_INCOMPATIBLE", "Codex generated no JSON protocol schema.", category="runtime")
        for path in files:
            data = path.read_bytes()
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(data)
            corpus.extend(data)
        text = corpus.decode("utf-8", errors="replace")
        compatible = all(f'"{method}"' in text for method in CODEX_REQUIRED_SCHEMA_METHODS)
        return digest.hexdigest(), compatible


def inspect_codex_installation(executable: Path | None = None, *, include_schema: bool = True) -> CodexInstallation:
    binary = executable or discover_codex_executable()
    if binary is None:
        raise ToolFailure("CODEX_NOT_INSTALLED", "No installed Codex executable was found.", category="not_found")
    version = codex_version(binary)
    if not include_schema:
        return CodexInstallation(binary, version)
    schema_hash, schema_compatible = generate_schema_fingerprint(binary)
    return CodexInstallation(binary, version, schema_hash, schema_compatible)


def _toml_string(value: str) -> str:
    # JSON string escaping is compatible with the TOML basic-string subset we
    # need here and avoids depending on a TOML writer at runtime.
    return json.dumps(value, ensure_ascii=False)


def _require_path_within(path_value: str, root: Path, *, kind: str) -> Path:
    path = Path(path_value).expanduser()
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ToolFailure(
            "CODEX_BRIDGE_ISOLATION_FAILED",
            f"Codex {kind} path is outside the selected Desktop runtime.",
            category="permission",
            details={"kind": kind},
        ) from exc
    return resolved


def prepare_isolated_codex_home(source_home: Path, target_home: Path, executable: Path) -> Path:
    """Create a minimal Computer-Use-only Codex home.

    The isolated home intentionally contains no auth file, plugins, marketplaces,
    hooks, skills, or unrelated MCP servers.  It copies only the installed
    node_repl transport paths needed to reach the OpenAI-signed local Computer
    Use service and tightens the node_repl trusted service map to `sky`.
    """

    config_path = source_home / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ToolFailure(
            "CODEX_BRIDGE_ISOLATION_FAILED",
            "Could not parse Codex configuration for the isolated broker.",
            category="runtime",
        ) from exc
    node_repl = config.get("mcp_servers", {}).get("node_repl")
    if not isinstance(node_repl, dict):
        raise ToolFailure(
            "CODEX_CAPABILITY_UNAVAILABLE",
            "Codex node_repl is not configured; Computer Use cannot be brokered.",
            category="runtime",
        )
    command = node_repl.get("command")
    source_env = node_repl.get("env")
    if not isinstance(command, str) or not command or not isinstance(source_env, dict):
        raise ToolFailure(
            "CODEX_CAPABILITY_UNAVAILABLE",
            "Codex node_repl configuration is incomplete.",
            category="runtime",
        )
    required_env = ("NODE_REPL_NODE_MODULE_DIRS", "NODE_REPL_NODE_PATH", "SKY_CUA_SERVICE_PATH")
    missing = [key for key in required_env if not isinstance(source_env.get(key), str) or not source_env.get(key)]
    if missing:
        raise ToolFailure(
            "CODEX_CAPABILITY_UNAVAILABLE",
            "Codex node_repl is missing Computer Use runtime paths.",
            category="runtime",
            details={"missing": missing},
        )
    desktop_resources = executable.resolve(strict=True).parent
    command_path = _require_path_within(command, desktop_resources, kind="node_repl executable")
    node_path = _require_path_within(str(source_env["NODE_REPL_NODE_PATH"]), desktop_resources, kind="node runtime")
    raw_module_dirs = str(source_env["NODE_REPL_NODE_MODULE_DIRS"])
    module_paths = [
        _require_path_within(value, desktop_resources, kind="node module directory")
        for value in raw_module_dirs.split(os.pathsep)
        if value
    ]
    if not module_paths or not all(path.is_dir() for path in module_paths):
        raise ToolFailure(
            "CODEX_CAPABILITY_UNAVAILABLE",
            "Codex node module directories are unavailable.",
            category="runtime",
        )
    computer_root = (source_home / "computer-use").resolve(strict=True)
    service_path = _require_path_within(str(source_env["SKY_CUA_SERVICE_PATH"]), computer_root, kind="Computer Use service")
    if not command_path.is_file() or not node_path.is_file() or not service_path.is_dir():
        raise ToolFailure(
            "CODEX_CAPABILITY_UNAVAILABLE",
            "Codex local Computer Use runtime is incomplete.",
            category="runtime",
        )

    target_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    module_dirs = os.pathsep.join(str(path) for path in module_paths)
    broker_env = {
        "NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS": str(source_env.get("NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS", "1000")),
        "NODE_REPL_NODE_MODULE_DIRS": module_dirs,
        "NODE_REPL_NODE_PATH": str(node_path),
        "NODE_REPL_TRUSTED_CODE_PATHS": os.pathsep.join((str(target_home), module_dirs)),
        "CODEX_HOME": str(target_home),
        "NODE_REPL_TRUSTED_SERVICES": json.dumps({"sky": "@oai/sky/service"}, separators=(",", ":")),
        "SKY_CUA_SERVICE_PATH": str(service_path),
        "CODEX_CLI_PATH": str(executable),
    }
    args = node_repl.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex node_repl args are invalid.", category="runtime")
    startup_timeout = node_repl.get("startup_timeout_sec", 120)
    if not isinstance(startup_timeout, (int, float)):
        raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex node_repl startup timeout is invalid.", category="runtime")
    lines = [
        "[mcp_servers.node_repl]",
        "args = [" + ", ".join(_toml_string(item) for item in args) + "]",
        f"command = {_toml_string(str(command_path))}",
        f"startup_timeout_sec = {startup_timeout}",
        "",
        "[mcp_servers.node_repl.env]",
    ]
    lines.extend(f"{key} = {_toml_string(value)}" for key, value in broker_env.items())
    isolated_config = target_home / "config.toml"
    isolated_config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    isolated_config.chmod(0o600)
    return isolated_config


def sanitized_broker_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Inherit the user environment while removing Coding Tools transport secrets."""

    env = dict(os.environ if source is None else source)
    for key in tuple(env):
        if any(key.startswith(marker) for marker in SENSITIVE_ENV_MARKERS):
            env.pop(key, None)
    return env


class CodexAppServerClient:
    """Small bidirectional JSON-RPC client with a hard no-model-turn allowlist."""

    def __init__(
        self,
        executable: Path,
        *,
        cwd: Path,
        codex_home: Path | None = None,
        config_overrides: Sequence[str] = (),
        request_handler: ServerRequestHandler | None = None,
        app_server_args: Sequence[str] = ("app-server", "--stdio"),
        env: dict[str, str] | None = None,
    ) -> None:
        self.executable = executable
        self.cwd = cwd
        configured_home = os.environ.get("CODEX_HOME")
        self.codex_home = codex_home or (Path(configured_home) if configured_home else Path.home() / ".codex")
        self.config_overrides = tuple(config_overrides)
        self.request_handler = request_handler
        self.app_server_args = tuple(app_server_args)
        self.env = sanitized_broker_env(env)
        self.env["CODEX_HOME"] = str(self.codex_home)
        self.process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._closed = threading.Event()

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        args = list(self.app_server_args)
        # `codex app-server` accepts `-c` as an app-server option.  Keep the
        # overrides before `--stdio`; current desktop builds do not reliably
        # accept config overrides appended after that transport flag.
        insert_at = args.index("--stdio") if "--stdio" in args else len(args)
        config_args = [item for override in self.config_overrides for item in ("-c", override)]
        args[insert_at:insert_at] = config_args
        command = [str(self.executable), *args]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(self.cwd),
                env=self.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ToolFailure(
                "CODEX_APP_SERVER_UNAVAILABLE",
                "Could not start Codex app-server.",
                category="runtime",
                details={"executable": str(self.executable)},
            ) from exc
        self._closed.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="codex-app-server-output")
        self._reader.start()

    def close(self) -> None:
        self._closed.set()
        process, self.process = self.process, None
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stderr is not None:
                process.stderr.close()
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)
        self._reader = None
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for waiter in pending:
            try:
                waiter.put_nowait({"error": {"code": -1, "message": "Codex app-server closed."}})
            except queue.Full:
                pass

    def __enter__(self) -> CodexAppServerClient:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _read_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if self._closed.is_set():
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if "method" in message and request_id is not None:
                    self._handle_server_request(message)
                    continue
                if request_id is not None:
                    with self._state_lock:
                        waiter = self._pending.get(request_id)
                    if waiter is not None:
                        try:
                            waiter.put_nowait(message)
                        except queue.Full:
                            pass
                    continue
                if "method" in message:
                    self._notifications.put(message)
        finally:
            self._closed.set()

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if self.request_handler is None:
            response: dict[str, Any] = {
                "id": request_id,
                "error": {"code": -32601, "message": f"Unhandled server request: {method}"},
            }
        else:
            try:
                response = {"id": request_id, "result": self.request_handler(method, params)}
            except ToolFailure as exc:
                response = {"id": request_id, "error": {"code": -32000, "message": exc.message, "data": {"code": exc.code}}}
            except Exception as exc:  # defensive boundary around a host callback
                response = {"id": request_id, "error": {"code": -32603, "message": str(exc)}}
        self._write_message(response)

    def _write_message(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise ToolFailure("CODEX_BROKER_CRASHED", "Codex app-server is not running.", category="runtime")
        payload = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                process.stdin.write(payload)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ToolFailure("CODEX_BROKER_CRASHED", "Could not write to Codex app-server.", category="runtime") from exc

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 20) -> dict[str, Any]:
        if method in CODEX_MODEL_METHODS or method.startswith("turn/"):
            raise ToolFailure(
                "CODEX_MODEL_TURN_FORBIDDEN",
                f"Codex capability broker forbids model RPC: {method}",
                category="permission",
            )
        if method not in CODEX_APP_SERVER_ALLOWED_METHODS:
            raise ToolFailure(
                "CODEX_RPC_FORBIDDEN",
                f"Codex capability broker does not allow RPC: {method}",
                category="permission",
            )
        self.start()
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        try:
            self._write_message({"id": request_id, "method": method, "params": params or {}})
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise ToolFailure(
                    "CODEX_APP_SERVER_TIMEOUT",
                    f"Codex app-server timed out handling {method}.",
                    category="runtime",
                    retryable=False,
                ) from exc
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)
        error = response.get("error")
        if isinstance(error, dict):
            raise ToolFailure(
                "CODEX_APP_SERVER_ERROR",
                str(error.get("message", "Codex app-server returned an error.")),
                category="runtime",
                details={"rpc_code": error.get("code"), "method": method},
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def wait_notification(self, method: str, *, timeout: float = 20) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolFailure(
                        "CODEX_APP_SERVER_TIMEOUT",
                        f"Timed out waiting for Codex notification {method}.",
                        category="runtime",
                    )
                try:
                    message = self._notifications.get(timeout=remaining)
                except queue.Empty as exc:
                    raise ToolFailure(
                        "CODEX_APP_SERVER_TIMEOUT",
                        f"Timed out waiting for Codex notification {method}.",
                        category="runtime",
                    ) from exc
                if message.get("method") == method:
                    return message
                deferred.append(message)
        finally:
            for message in deferred:
                self._notifications.put(message)


def _extract_tool_text(result: dict[str, Any]) -> str:
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            return item["text"]
    raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex tool call returned no text content.", category="runtime")


def probe_codex_computer(
    workspace: Path,
    *,
    executable: Path | None = None,
    codex_home: Path | None = None,
    include_schema: bool = True,
    timeout: float = 30,
) -> CodexBridgeProbe:
    """Perform the read-only Computer Use compatibility probe used after upgrades."""

    configured_home = os.environ.get("CODEX_HOME")
    home = codex_home or (Path(configured_home) if configured_home else Path.home() / ".codex")
    try:
        installation = inspect_codex_installation(executable, include_schema=include_schema)
    except ToolFailure as exc:
        return CodexBridgeProbe(False, False, str(executable) if executable else None, None, None, (), None, None, exc.code)
    if installation.schema_compatible is False:
        return CodexBridgeProbe(True, False, str(installation.executable), installation.version,
                                installation.schema_hash, (), None, None, "CODEX_SCHEMA_INCOMPATIBLE")
    started_servers: set[str] = set()
    try:
        with tempfile.TemporaryDirectory(prefix="ctm-codex-broker-") as temp:
            isolated_home = Path(temp)
            prepare_isolated_codex_home(home, isolated_home, installation.executable)
            client = CodexAppServerClient(
                installation.executable,
                cwd=workspace,
                codex_home=isolated_home,
            )
            with client:
                client.call("initialize", {
                    "clientInfo": {"name": "coding-tools-mcp-capability-probe", "version": "1"},
                    "capabilities": {},
                }, timeout=timeout)
                started = client.call("thread/start", {"ephemeral": True, "cwd": str(workspace), "model": None}, timeout=timeout)
                thread = started.get("thread")
                if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                    raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex did not return an ephemeral thread id.", category="runtime")
                thread_id = thread["id"]

                deadline = time.monotonic() + timeout
                node_ready = False
                while time.monotonic() < deadline and not node_ready:
                    note = client.wait_notification("mcpServer/startupStatus/updated", timeout=max(0.1, deadline - time.monotonic()))
                    params = note.get("params")
                    if not isinstance(params, dict) or params.get("threadId") != thread_id:
                        continue
                    name = params.get("name")
                    if isinstance(name, str):
                        started_servers.add(name)
                        if name not in CODEX_COMPUTER_ALLOWED_MCP_SERVERS:
                            raise ToolFailure(
                                "CODEX_BRIDGE_ISOLATION_FAILED",
                                f"Unexpected Codex MCP server started in isolated broker: {name}",
                                category="permission",
                                details={"started_servers": sorted(started_servers)},
                            )
                        if name == "node_repl" and params.get("status") == "ready":
                            node_ready = True
                if not node_ready:
                    raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex node_repl did not become ready.", category="runtime")

                code = (
                    'globalThis.sky=(await import("@oai/sky")).sky; '
                    'const apps=await sky.list_apps(); '
                    'nodeRepl.write(JSON.stringify(apps.map(a=>({displayName:a.displayName,id:a.id,isRunning:a.isRunning}))))'
                )
                tool_result = client.call("mcpServer/tool/call", {
                    "server": "node_repl",
                    "threadId": thread_id,
                    "tool": "js",
                    "arguments": {"code": code, "timeout_ms": int(timeout * 1000), "title": "CTM Codex bridge probe"},
                }, timeout=timeout)
                try:
                    apps = json.loads(_extract_tool_text(tool_result))
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex list_apps probe returned invalid JSON.", category="runtime") from exc
                if not isinstance(apps, list):
                    raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex list_apps probe did not return a list.", category="runtime")
                readback = client.call("thread/read", {"threadId": thread_id}, timeout=timeout)
                read_thread = readback.get("thread")
                turns = read_thread.get("turns") if isinstance(read_thread, dict) else None
                if turns != []:
                    raise ToolFailure(
                        "CODEX_MODEL_TURN_GUARD_FAILED",
                        "Codex probe thread unexpectedly contains model turns.",
                        category="permission",
                        details={"turn_count": len(turns) if isinstance(turns, list) else None},
                    )
                return CodexBridgeProbe(
                    True, True, str(installation.executable), installation.version, installation.schema_hash,
                    tuple(sorted(started_servers)), len(apps), 0, None,
                )
    except ToolFailure as exc:
        return CodexBridgeProbe(
            True, False, str(installation.executable), installation.version, installation.schema_hash,
            tuple(sorted(started_servers)), None, None, exc.code,
        )

