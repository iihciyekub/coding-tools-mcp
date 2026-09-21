from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .errors import ToolFailure


LANGUAGE_SUFFIXES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".swift": "swift",
}
DEFAULT_COMMANDS = {
    "python": [
        ["basedpyright-langserver", "--stdio"],
        ["pyright-langserver", "--stdio"],
        ["pylsp"],
    ],
    "typescript": [["typescript-language-server", "--stdio"]],
    "rust": [["rust-analyzer"]],
    "swift": [["sourcekit-lsp"]],
}
COMMAND_ENV = {
    "python": "CODING_TOOLS_MCP_PYTHON_LSP_COMMAND",
    "typescript": "CODING_TOOLS_MCP_TYPESCRIPT_LSP_COMMAND",
    "rust": "CODING_TOOLS_MCP_RUST_LSP_COMMAND",
    "swift": "CODING_TOOLS_MCP_SWIFT_LSP_COMMAND",
}


class LanguageServer:
    def __init__(self, workspace: Path, language: str, command: list[str], env: dict[str, str]) -> None:
        self.workspace = workspace
        self.language = language
        self.command = command
        try:
            self.process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ToolFailure(
                "LSP_UNAVAILABLE", f"Could not start {language} language server: {exc}", category="runtime"
            ) from exc
        self._next_id = 1
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._document_lock = threading.RLock()
        self._diagnostics_condition = threading.Condition(self._document_lock)
        self._document_versions: dict[str, int] = {}
        self._diagnostic_versions: dict[str, int | None] = {}
        self._diagnostic_observed_versions: dict[str, int] = {}
        self._opened: dict[str, str] = {}
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"lsp-{language}-stdout")
        self._reader.start()
        if self.process.stderr is not None:
            threading.Thread(target=self._drain_stderr, daemon=True, name=f"lsp-{language}-stderr").start()
        self.capabilities = self._initialize()

    def _initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.workspace.as_uri(),
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]},
                    "textDocument": {
                        "definition": {"linkSupport": True},
                        "publishDiagnostics": {"relatedInformation": True, "versionSupport": True},
                    },
                    "workspace": {"workspaceFolders": True},
                },
                "workspaceFolders": [{"uri": self.workspace.as_uri(), "name": self.workspace.name}],
            },
            timeout=15,
        )
        self.notify("initialized", {})
        return result.get("capabilities", {}) if isinstance(result, dict) else {}

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        while self.process.stderr.read(8192):
            pass

    def _read_loop(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        try:
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        return
                    if line in {b"\r\n", b"\n"}:
                        break
                    key, separator, value = line.decode("ascii", errors="replace").partition(":")
                    if separator:
                        headers[key.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                if length <= 0 or length > 16 * 1024 * 1024:
                    return
                body = stream.read(length)
                if len(body) != length:
                    return
                message = json.loads(body.decode("utf-8"))
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if isinstance(message_id, int):
                    with self._pending_lock:
                        target = self._pending.get(message_id)
                    if target is not None:
                        target.put(message)
                    continue
                if message.get("method") == "textDocument/publishDiagnostics":
                    params = message.get("params")
                    if isinstance(params, dict) and isinstance(params.get("uri"), str):
                        self._publish_diagnostics(params)
        except (OSError, ValueError, json.JSONDecodeError):
            return

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise ToolFailure("LSP_EXITED", f"{self.language} language server exited.", category="runtime", retryable=True)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        message = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._write_lock:
            try:
                self.process.stdin.write(message)
                self.process.stdin.flush()
            except OSError as exc:
                raise ToolFailure("LSP_EXITED", "Language server connection closed.", category="runtime", retryable=True) from exc

    def request(self, method: str, params: dict[str, Any], *, timeout: float = 10) -> Any:
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = response_queue
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                response = response_queue.get(timeout=timeout)
            except queue.Empty as exc:
                raise ToolFailure(
                    "LSP_TIMEOUT", f"Language server timed out during {method}.", category="runtime", retryable=True
                ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if isinstance(response.get("error"), dict):
            error = response["error"]
            raise ToolFailure(
                "LSP_ERROR",
                str(error.get("message") or f"Language server rejected {method}."),
                category="runtime",
                details={"method": method, "server_code": error.get("code")},
            )
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def open_document(self, path: Path) -> tuple[str, str]:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolFailure("UNSUPPORTED_ENCODING", "LSP files must be UTF-8.", category="validation") from exc
        uri = path.as_uri()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        language_id = {
            ".py": "python",
            ".pyi": "python",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".rs": "rust",
            ".swift": "swift",
        }.get(path.suffix.lower(), self.language)
        with self._document_lock:
            previous = self._opened.get(uri)
            if previous != digest:
                version = self._document_versions.get(uri, 0) + 1
                self._opened[uri] = digest
                self._document_versions[uri] = version
                if previous is None:
                    self.notify("textDocument/didOpen", {"textDocument": {
                        "uri": uri, "languageId": language_id, "version": version, "text": content,
                    }})
                else:
                    self.notify("textDocument/didChange", {
                        "textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": content}],
                    })
                self._diagnostics_condition.notify_all()
        return uri, digest

    def _publish_diagnostics(self, params: dict[str, Any]) -> None:
        uri = str(params["uri"])
        diagnostics = params.get("diagnostics")
        if not isinstance(diagnostics, list):
            return
        version = params.get("version")
        version = version if isinstance(version, int) and not isinstance(version, bool) else None
        with self._diagnostics_condition:
            current = self._document_versions.get(uri, 0)
            if version is not None and version < current:
                return  # A late publication must never overwrite a current result.
            self._diagnostics[uri] = diagnostics
            self._diagnostic_versions[uri] = version
            self._diagnostic_observed_versions[uri] = current
            self._diagnostics_condition.notify_all()

    def diagnostics_snapshot(self, uri: str, wait_ms: int, *, expected_digest: str | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + wait_ms / 1000
        with self._diagnostics_condition:
            def freshness() -> str:
                if expected_digest is not None and self._opened.get(uri) != expected_digest:
                    return "stale"
                if uri not in self._diagnostics:
                    return "pending"
                current = self._document_versions.get(uri, 0)
                published = self._diagnostic_versions.get(uri)
                if published == current:
                    return "fresh"
                if published is None and self._diagnostic_observed_versions.get(uri) == current:
                    return "unversioned"
                return "stale"

            while not self._closed and freshness() in {"pending", "stale"}:
                if expected_digest is not None and self._opened.get(uri) != expected_digest:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._diagnostics_condition.wait(remaining)
            return {
                "diagnostics": list(self._diagnostics.get(uri, [])),
                "document_version": self._document_versions.get(uri),
                "diagnostics_version": self._diagnostic_versions.get(uri),
                "freshness": freshness(),
            }

    def diagnostics(self, uri: str, wait_ms: int) -> list[dict[str, Any]]:
        return self.diagnostics_snapshot(uri, wait_ms)["diagnostics"]

    def close(self) -> None:
        if self._closed:
            return
        with self._diagnostics_condition:
            self._closed = True
            self._diagnostics_condition.notify_all()
        try:
            if self.process.poll() is None:
                try:
                    self.request("shutdown", {}, timeout=2)
                    self.notify("exit", {})
                    self.process.wait(timeout=2)
                except (ToolFailure, subprocess.TimeoutExpired):
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
        finally:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
            self._reader.join(timeout=1)


class LSPManager:
    def __init__(self, workspace: Path, env: dict[str, str]) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.env = env
        self._servers: dict[tuple[str, Path], LanguageServer] = {}
        self._lock = threading.RLock()

    def _command(self, language: str) -> list[str] | None:
        configured = os.environ.get(COMMAND_ENV[language])
        if configured:
            parts = shlex.split(configured)
            if parts and (Path(parts[0]).is_file() or shutil.which(parts[0], path=self.env.get("PATH"))):
                return parts
            return None
        for candidate in DEFAULT_COMMANDS[language]:
            executable = shutil.which(candidate[0], path=self.env.get("PATH"))
            if executable:
                if language == "rust":
                    try:
                        rust_probe = subprocess.run(
                            [executable, "--version"],
                            cwd=self.workspace,
                            env=self.env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=3,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        continue
                    if rust_probe.returncode != 0:
                        continue
                return [executable, *candidate[1:]]
        if language == "swift":
            xcrun = shutil.which("xcrun", path=self.env.get("PATH"))
            if xcrun:
                try:
                    swift_probe = subprocess.run(
                        [xcrun, "--find", "sourcekit-lsp"],
                        cwd=self.workspace,
                        env=self.env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=3,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    swift_probe = None
                if swift_probe is not None and swift_probe.returncode == 0:
                    resolved = swift_probe.stdout.strip()
                    if resolved and Path(resolved).is_file():
                        return [resolved]
        return None

    def status(self) -> dict[str, Any]:
        backends = []
        for language in ("python", "typescript", "rust", "swift"):
            command = self._command(language)
            running = [server for (item_language, _), server in self._servers.items() if item_language == language]
            backends.append(
                {
                    "language": language,
                    "available": command is not None,
                    "running": any(server.process.poll() is None for server in running),
                    "command": command,
                    "extensions": sorted(suffix for suffix, value in LANGUAGE_SUFFIXES.items() if value == language),
                    "project_roots": sorted(
                        server.workspace.relative_to(self.workspace).as_posix() or "."
                        for server in running
                        if server.process.poll() is None
                    ),
                }
            )
        return {"ok": True, "backends": backends, "summary": "Inspected Python, TypeScript, Rust, and Swift LSP backends."}

    def project_root_for(self, path: Path, language: str) -> Path:
        candidate = path.parent.resolve(strict=True)
        if not candidate.is_relative_to(self.workspace):
            raise ToolFailure("LSP_PATH_OUTSIDE_WORKSPACE", "Document is outside the workspace.", category="security")
        markers = {
            "python": ("pyrightconfig.json", "pyproject.toml", "setup.cfg", "setup.py"),
            "typescript": ("tsconfig.json", "jsconfig.json", "package.json"),
            "rust": ("Cargo.toml",),
            "swift": ("Package.swift",),
        }.get(language, ())
        while True:
            swift_container = False
            if language == "swift":
                try:
                    swift_container = any(
                        child.suffix in {".xcodeproj", ".xcworkspace"}
                        for child in candidate.iterdir()
                    )
                except OSError:
                    swift_container = False
            if any((candidate / name).is_file() for name in markers) or swift_container:
                return candidate
            if (candidate / ".git").exists():
                return candidate
            if candidate == self.workspace:
                return self.workspace
            try:
                candidate.relative_to(self.workspace)
            except ValueError:
                return self.workspace
            parent = candidate.parent
            if parent == candidate:
                return self.workspace
            candidate = parent

    def server_for(self, path: Path) -> LanguageServer:
        language = LANGUAGE_SUFFIXES.get(path.suffix.lower())
        if language is None:
            raise ToolFailure("LSP_LANGUAGE_UNSUPPORTED", f"No LSP backend is configured for {path.suffix}.", category="validation")
        project_root = self.project_root_for(path, language)
        key = (language, project_root)
        with self._lock:
            current = self._servers.get(key)
            if current is not None and current.process.poll() is None:
                return current
            command = self._command(language)
            if command is None:
                raise ToolFailure(
                    "LSP_UNAVAILABLE",
                    f"No {language} language server is installed.",
                    category="runtime",
                    details={"language": language, "configured_by": COMMAND_ENV[language]},
                )
            server = LanguageServer(project_root, language, command, self.env)
            self._servers[key] = server
            return server

    def close(self) -> None:
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            server.close()


def lsp_position(path: Path, line: int, column: int) -> dict[str, int]:
    if line < 1 or column < 1:
        raise ToolFailure("INVALID_ARGUMENT", "line and column must be >= 1.", category="validation")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ToolFailure("UNSUPPORTED_ENCODING", "LSP files must be UTF-8.", category="validation") from exc
    if line > len(lines):
        raise ToolFailure("INVALID_ARGUMENT", "line is outside the file.", category="validation")
    if column > len(lines[line - 1]) + 1:
        raise ToolFailure("INVALID_ARGUMENT", "column is outside the line.", category="validation")
    prefix = lines[line - 1][: column - 1]
    utf16_column = len(prefix.encode("utf-16-le")) // 2
    return {"line": line - 1, "character": utf16_column}


def normalize_locations(workspace: Path, value: Any) -> list[dict[str, Any]]:
    raw = value if isinstance(value, list) else [] if value is None else [value]
    locations: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or item.get("targetUri")
        location_range = item.get("range") or item.get("targetSelectionRange") or item.get("targetRange")
        if not isinstance(uri, str) or not isinstance(location_range, dict):
            continue
        path = uri_to_workspace_path(workspace, uri)
        locations.append({"path": path.relative_to(workspace).as_posix(), "range": normalize_range(location_range)})
    return locations


def normalize_range(value: dict[str, Any]) -> dict[str, Any]:
    def position(raw: Any) -> dict[str, int]:
        item = raw if isinstance(raw, dict) else {}
        return {"line": int(item.get("line", 0)) + 1, "column_utf16": int(item.get("character", 0)) + 1}

    return {"start": position(value.get("start")), "end": position(value.get("end"))}


def uri_to_workspace_path(workspace: Path, uri: str) -> Path:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        raise ToolFailure("LSP_PATH_OUTSIDE_WORKSPACE", "Language server returned a non-file URI.", category="security")
    raw_path = urllib.parse.unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    path = Path(raw_path).resolve(strict=False)
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise ToolFailure(
            "LSP_PATH_OUTSIDE_WORKSPACE", "Language server returned a path outside the workspace.", category="security"
        ) from exc
    return path


def normalize_diagnostics(workspace: Path, uri: str, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = uri_to_workspace_path(workspace, uri).relative_to(workspace).as_posix()
    results = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("range"), dict):
            continue
        results.append(
            {
                "path": path,
                "range": normalize_range(item["range"]),
                "severity": item.get("severity"),
                "code": item.get("code"),
                "source": item.get("source"),
                "message": str(item.get("message", "")),
            }
        )
    return results


def normalize_workspace_edit(workspace: Path, value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise ToolFailure("LSP_EDIT_UNSUPPORTED", "Language server returned an invalid WorkspaceEdit.", category="runtime")
    grouped: dict[str, list[dict[str, Any]]] = {}
    changes = value.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            if isinstance(uri, str) and isinstance(edits, list):
                grouped.setdefault(uri, []).extend(item for item in edits if isinstance(item, dict))
    document_changes = value.get("documentChanges")
    if isinstance(document_changes, list):
        for change in document_changes:
            if not isinstance(change, dict) or "textDocument" not in change:
                raise ToolFailure(
                    "LSP_EDIT_UNSUPPORTED", "File create, rename, and delete edits are not supported.", category="validation"
                )
            document = change.get("textDocument")
            edits = change.get("edits")
            if isinstance(document, dict) and isinstance(document.get("uri"), str) and isinstance(edits, list):
                grouped.setdefault(document["uri"], []).extend(item for item in edits if isinstance(item, dict))
    result = []
    for uri, edits in grouped.items():
        path = uri_to_workspace_path(workspace, uri)
        content = path.read_bytes()
        normalized = []
        for edit in edits:
            if not isinstance(edit.get("range"), dict) or not isinstance(edit.get("newText"), str):
                raise ToolFailure("LSP_EDIT_UNSUPPORTED", "Unsupported text edit shape.", category="validation")
            normalized.append({"range": normalize_range(edit["range"]), "new_text": edit["newText"]})
        result.append(
            {
                "path": path.relative_to(workspace).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "edits": normalized,
            }
        )
    return result
