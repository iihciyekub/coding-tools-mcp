"""Codex Computer Use provider built on the isolated no-model-turn app-server broker.

Phase 1 established app discovery and observation. Computer v2 adds explicitly
approved control sessions with fresh-snapshot checks and bounded action methods;
Native Computer v1 remains the default provider.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .codex_bridge import (
    CODEX_COMPUTER_ALLOWED_MCP_SERVERS,
    CodexAppServerClient,
    discover_codex_executable,
    inspect_codex_installation,
    prepare_isolated_codex_home,
)
from .errors import ToolFailure
from .workflow_store import WorkflowStore


MAX_STATE_TEXT_CHARS = 256_000
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
MAX_SNAPSHOT_AGE_SECONDS = 10.0
CODEX_CONTROL_TOOLS = frozenset({
    "click",
    "perform_secondary_action",
    "set_value",
    "select_text",
    "scroll",
    "drag",
    "press_key",
    "type_text",
})


@dataclass(frozen=True)
class CodexComputerApp:
    app_id: str
    display_name: str | None
    is_running: bool | None
    last_used_date: str | None = None
    use_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "display_name": self.display_name,
            "is_running": self.is_running,
            "last_used_date": self.last_used_date,
            "use_count": self.use_count,
        }


@dataclass(frozen=True)
class CodexComputerState:
    app: str
    text: str
    text_truncated: bool
    screenshot_url: str | None


@dataclass(frozen=True)
class CodexComputerImage:
    data: bytes
    mime_type: str


ApprovalDecider = Callable[[dict[str, Any]], bool]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _SessionPolicy:
    app_id: str
    access: str
    approval_decider: ApprovalDecider


class CodexComputerObservationSession:
    def __init__(self, provider: CodexComputerProvider, thread_id: str, app_id: str, access: str = "observe") -> None:
        self._provider = provider
        self.thread_id = thread_id
        self.app_id = app_id
        self.access = access
        self._closed = False

    def get_state(self, *, disable_diff: bool = True, max_text_chars: int = MAX_STATE_TEXT_CHARS) -> CodexComputerState:
        if self._closed:
            raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex observation session is closed.", category="permission")
        return self._provider._get_app_state(self.thread_id, self.app_id, disable_diff=disable_diff,
                                             max_text_chars=max_text_chars)

    def read_screenshot(self, state: CodexComputerState, *, max_bytes: int = MAX_SCREENSHOT_BYTES) -> CodexComputerImage:
        if self._closed:
            raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex observation session is closed.", category="permission")
        return self._provider.read_screenshot(state.screenshot_url, max_bytes=max_bytes)

    def interact(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex Computer session is closed.", category="permission")
        if self.access != "control":
            raise ToolFailure("COMPUTER_CONTROL_REQUIRED", "This Codex session permits observation only.", category="permission")
        return self._provider._interact(self.thread_id, self.app_id, operation, arguments)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._provider._close_session(self.thread_id)

    def __enter__(self) -> CodexComputerObservationSession:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class CodexComputerProvider:
    """One isolated app-server broker with bounded read-only Computer sessions."""

    def __init__(
        self,
        workspace: Path,
        *,
        executable: Path | None = None,
        source_codex_home: Path | None = None,
        timeout: float = 30,
    ) -> None:
        self.workspace = workspace.resolve()
        self.executable = executable
        configured_home = os.environ.get("CODEX_HOME")
        self.source_codex_home = source_codex_home or (Path(configured_home) if configured_home else Path.home() / ".codex")
        self.timeout = timeout
        self._lock = threading.RLock()
        self._temporary_home: tempfile.TemporaryDirectory[str] | None = None
        self._client: CodexAppServerClient | None = None
        self._catalog_thread_id: str | None = None
        self._sessions: dict[str, _SessionPolicy] = {}
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise ToolFailure("CODEX_BROKER_CRASHED", "Codex Computer provider is closed.", category="runtime")
            if self._client is not None:
                return
            binary = self.executable or discover_codex_executable()
            if binary is None:
                raise ToolFailure("CODEX_NOT_INSTALLED", "No installed Desktop Codex runtime was found.", category="not_found")
            installation = inspect_codex_installation(binary, include_schema=False)
            temp = tempfile.TemporaryDirectory(prefix="ctm-codex-computer-")
            isolated_home = Path(temp.name)
            try:
                prepare_isolated_codex_home(self.source_codex_home, isolated_home, installation.executable)
                client = CodexAppServerClient(
                    installation.executable,
                    cwd=self.workspace,
                    codex_home=isolated_home,
                    request_handler=self._handle_server_request,
                )
                client.start()
                client.call("initialize", {
                    "clientInfo": {"name": "coding-tools-mcp-codex-computer", "version": "1"},
                    "capabilities": {},
                }, timeout=self.timeout)
            except Exception:
                temp.cleanup()
                raise
            self.executable = installation.executable
            self._temporary_home = temp
            self._client = client

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            client, self._client = self._client, None
            temp, self._temporary_home = self._temporary_home, None
            self._sessions.clear()
            self._catalog_thread_id = None
        if client is not None:
            client.close()
        if temp is not None:
            temp.cleanup()

    def __enter__(self) -> CodexComputerProvider:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _client_required(self) -> CodexAppServerClient:
        self.start()
        assert self._client is not None
        return self._client

    def _new_thread(self) -> str:
        client = self._client_required()
        started = client.call("thread/start", {
            "ephemeral": True,
            "cwd": str(self.workspace),
            "model": None,
        }, timeout=self.timeout)
        thread = started.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex did not return an ephemeral thread id.", category="runtime")
        thread_id = thread["id"]
        while True:
            note = client.wait_notification("mcpServer/startupStatus/updated", timeout=self.timeout)
            params = note.get("params")
            if not isinstance(params, dict) or params.get("threadId") != thread_id:
                continue
            name = params.get("name")
            if isinstance(name, str) and name not in CODEX_COMPUTER_ALLOWED_MCP_SERVERS:
                raise ToolFailure(
                    "CODEX_BRIDGE_ISOLATION_FAILED",
                    f"Unexpected Codex MCP server started in isolated Computer provider: {name}",
                    category="permission",
                )
            if name == "node_repl" and params.get("status") == "ready":
                return thread_id

    @staticmethod
    def _tool_text(result: dict[str, Any]) -> str:
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    return item["text"]
        raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex Computer tool returned no text result.", category="runtime")

    def _call_js(self, thread_id: str, code: str, *, title: str) -> Any:
        client = self._client_required()
        result = client.call("mcpServer/tool/call", {
            "server": "node_repl",
            "threadId": thread_id,
            "tool": "js",
            "arguments": {"code": code, "timeout_ms": int(self.timeout * 1000), "title": title},
        }, timeout=self.timeout + 5)
        text = self._tool_text(result)
        if result.get("isError") is True:
            raise ToolFailure(
                "CODEX_CAPABILITY_UNAVAILABLE",
                text[:2000] or "Codex Computer tool failed.",
                category="runtime",
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as first_error:
            payload = None
            # node_repl may prepend non-fatal warning lines to explicit
            # nodeRepl.write() output.  Our payload is always one compact JSON
            # line, so accept the final line that decodes cleanly rather than
            # treating a warning as a capability failure.
            for line in reversed(text.splitlines()):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            if payload is None:
                raise ToolFailure(
                    "CODEX_CAPABILITY_UNAVAILABLE",
                    "Codex Computer returned invalid JSON.",
                    category="runtime",
                ) from first_error
        self._assert_no_model_turns(thread_id)
        return payload

    def _assert_no_model_turns(self, thread_id: str) -> None:
        readback = self._client_required().call("thread/read", {"threadId": thread_id}, timeout=self.timeout)
        thread = readback.get("thread")
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if turns != []:
            self.close()
            raise ToolFailure(
                "CODEX_MODEL_TURN_GUARD_FAILED",
                "Codex Computer provider unexpectedly created a model turn.",
                category="permission",
                details={"turn_count": len(turns) if isinstance(turns, list) else None},
            )

    def list_apps(self) -> list[CodexComputerApp]:
        with self._lock:
            if self._catalog_thread_id is None:
                self._catalog_thread_id = self._new_thread()
            code = (
                'await (async()=>{const {sky}=await import("@oai/sky"); const a=await sky.list_apps(); '
                'nodeRepl.write(JSON.stringify(a.map(x=>({app_id:x.id,display_name:x.displayName??null,'
                'is_running:x.isRunning??null,last_used_date:x.lastUsedDate??null,use_count:x.useCount??null}))));})()'
            )
            payload = self._call_js(self._catalog_thread_id, code, title="List apps")
            if not isinstance(payload, list):
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex app catalog was not a list.", category="runtime")
            apps: list[CodexComputerApp] = []
            for row in payload:
                if not isinstance(row, dict) or not isinstance(row.get("app_id"), str):
                    continue
                apps.append(CodexComputerApp(
                    app_id=row["app_id"],
                    display_name=row.get("display_name") if isinstance(row.get("display_name"), str) else None,
                    is_running=row.get("is_running") if isinstance(row.get("is_running"), bool) else None,
                    last_used_date=row.get("last_used_date") if isinstance(row.get("last_used_date"), str) else None,
                    use_count=row.get("use_count") if isinstance(row.get("use_count"), int) else None,
                ))
            return apps

    def open_observation(self, app_id: str, approval_decider: ApprovalDecider) -> CodexComputerObservationSession:
        return self.open_session(app_id, "observe", approval_decider)

    def open_session(self, app_id: str, access: str, approval_decider: ApprovalDecider) -> CodexComputerObservationSession:
        if not app_id.strip():
            raise ToolFailure("INVALID_ARGUMENT", "app_id is required.", category="validation")
        if access not in {"observe", "control"}:
            raise ToolFailure("INVALID_ARGUMENT", "Codex Computer access must be observe or control.", category="validation")
        with self._lock:
            thread_id = self._new_thread()
            self._sessions[thread_id] = _SessionPolicy(app_id=app_id, access=access, approval_decider=approval_decider)
            return CodexComputerObservationSession(self, thread_id, app_id, access)

    def _close_session(self, thread_id: str) -> None:
        with self._lock:
            self._sessions.pop(thread_id, None)

    def _handle_server_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method != "mcpServer/elicitation/request":
            raise ToolFailure("CODEX_RPC_FORBIDDEN", f"Unexpected Codex server request: {method}", category="permission")
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return {"action": "decline", "content": {}}
        policy = self._sessions.get(thread_id)
        if policy is None:
            return {"action": "decline", "content": {}}
        meta = params.get("_meta")
        if not isinstance(meta, dict) or meta.get("connector_id") != "computer-use":
            return {"action": "decline", "content": {}}
        tool_name = meta.get("tool_name")
        if tool_name != "get_app_state" and not (policy.access == "control" and tool_name in CODEX_CONTROL_TOOLS):
            return {"action": "decline", "content": {}}
        tool_params = meta.get("tool_params")
        requested_app = tool_params.get("app") if isinstance(tool_params, dict) else None
        if requested_app != policy.app_id:
            return {"action": "decline", "content": {}}
        approved = bool(policy.approval_decider({
            "thread_id": thread_id,
            "app_id": policy.app_id,
            "tool_name": tool_name,
            "risk_level": meta.get("riskLevel"),
            "message": params.get("message"),
        }))
        if not approved:
            return {"action": "decline", "content": {}}
        return {"action": "accept", "content": {}, "_meta": {"persist": "session"}}

    def _get_app_state(self, thread_id: str, app_id: str, *, disable_diff: bool,
                       max_text_chars: int) -> CodexComputerState:
        if max_text_chars < 1 or max_text_chars > MAX_STATE_TEXT_CHARS:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"max_text_chars must be between 1 and {MAX_STATE_TEXT_CHARS}.",
                category="validation",
            )
        with self._lock:
            policy = self._sessions.get(thread_id)
            if policy is None or policy.app_id != app_id:
                raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex observation session is inactive.", category="permission")
            app_json = json.dumps(app_id, ensure_ascii=False)
            diff_json = "true" if disable_diff else "false"
            code = (
                'await (async()=>{const {sky}=await import("@oai/sky"); '
                f'const s=await sky.get_app_state({{app:{app_json},disableDiff:{diff_json}}}); '
                'const t=typeof s.text==="string"?s.text:""; '
                f'nodeRepl.write(JSON.stringify({{app:String(s.app??{app_json}),text:t.slice(0,{max_text_chars}),' 
                f'text_truncated:t.length>{max_text_chars},screenshot_url:s.screenshot?.url??null}}));}})()'
            )
            payload = self._call_js(thread_id, code, title="Observe app state")
            if not isinstance(payload, dict):
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex app state was not an object.", category="runtime")
            text = payload.get("text")
            screenshot_url = payload.get("screenshot_url")
            return CodexComputerState(
                app=str(payload.get("app", app_id)),
                text=text if isinstance(text, str) else "",
                text_truncated=bool(payload.get("text_truncated")),
                screenshot_url=screenshot_url if isinstance(screenshot_url, str) and screenshot_url else None,
            )

    def _interact(self, thread_id: str, app_id: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation not in CODEX_CONTROL_TOOLS:
            raise ToolFailure("INVALID_ARGUMENT", f"Unsupported Codex Computer action: {operation}", category="validation")
        with self._lock:
            policy = self._sessions.get(thread_id)
            if policy is None or policy.app_id != app_id:
                raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex Computer session is inactive.", category="permission")
            if policy.access != "control":
                raise ToolFailure("COMPUTER_CONTROL_REQUIRED", "This Codex session permits observation only.", category="permission")
            app_json = json.dumps(app_id, ensure_ascii=False)
            op_json = json.dumps(operation)
            args_json = json.dumps({"app": app_id, **arguments}, ensure_ascii=False, separators=(",", ":"))
            code = (
                'await (async()=>{const {sky}=await import("@oai/sky"); '
                f'const fn=sky[{op_json}]; if(typeof fn!=="function") throw new Error("Unsupported sky action"); '
                f'const r=await fn({args_json}); nodeRepl.write(JSON.stringify({{ok:true,app:{app_json},result:r??null}}));}})()'
            )
            payload = self._call_js(thread_id, code, title=f"Computer {operation}")
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex Computer action returned an invalid result.", category="runtime")
            return payload

    @staticmethod
    def read_screenshot(url: str | None, *, max_bytes: int = MAX_SCREENSHOT_BYTES) -> CodexComputerImage:
        if url is None:
            raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex app state contains no screenshot.", category="not_found")
        if max_bytes < 1 or max_bytes > MAX_SCREENSHOT_BYTES:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"max_bytes must be between 1 and {MAX_SCREENSHOT_BYTES}.",
                category="validation",
            )
        if url.startswith("data:"):
            header, separator, payload = url.partition(",")
            if not separator or ";base64" not in header:
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Unsupported Computer Use screenshot data URL.", category="runtime")
            mime_type = header[5:].split(";", 1)[0]
            if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Unsupported Computer Use screenshot type.", category="runtime")
            try:
                data = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Invalid Computer Use screenshot data.", category="runtime") from exc
        else:
            parsed = urlparse(url)
            if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Computer Use screenshot URL is not a local file.", category="permission")
            path = Path(unquote(parsed.path))
            try:
                resolved = path.resolve(strict=True)
                size = resolved.stat().st_size
                if size > max_bytes:
                    raise ToolFailure("OUTPUT_LIMIT_EXCEEDED", "Computer Use screenshot exceeds the configured limit.", category="runtime")
                data = resolved.read_bytes()
            except ToolFailure:
                raise
            except OSError as exc:
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Could not read the Computer Use screenshot.", category="runtime") from exc
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime_type = "image/png"
            elif data.startswith(b"\xff\xd8\xff"):
                mime_type = "image/jpeg"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime_type = "image/webp"
            else:
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Computer Use screenshot has an unsupported image format.", category="runtime")
        if len(data) > max_bytes:
            raise ToolFailure("OUTPUT_LIMIT_EXCEEDED", "Computer Use screenshot exceeds the configured limit.", category="runtime")
        return CodexComputerImage(data=data, mime_type=mime_type)


@dataclass
class _ObservationLease:
    session_id: str
    app: CodexComputerApp
    provider_session: CodexComputerObservationSession
    access: str
    expires_at: float
    created_at: float
    status: str = "active"
    snapshot_id: str | None = None
    observed_at: float | None = None


class CodexComputerObservationService:
    """CTM-owned approval/session layer for Codex observe and v2 control scopes."""

    MAX_SESSIONS = 8
    APPROVAL_TTL_SECONDS = 300

    def __init__(self, workflow: WorkflowStore, provider: CodexComputerProvider) -> None:
        self.workflow = workflow
        self.provider = provider
        self.runtime_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._sessions: dict[str, _ObservationLease] = {}
        self._closed = False

    @staticmethod
    def _app_scope(app: CodexComputerApp) -> dict[str, Any]:
        # High-level @oai/sky does not expose a stable PID/instance identity.
        # Phase 1 is observe-only and binds the approval to the canonical app id
        # plus its display identity.  Control remains on CTM Native Computer.
        return {
            "app_id": app.app_id,
            "display_name": app.display_name,
        }

    def _find_app(self, app_id: str) -> CodexComputerApp:
        for app in self.provider.list_apps():
            if app.app_id == app_id:
                return app
        raise ToolFailure("COMPUTER_APP_NOT_FOUND", f"Codex Computer app not found: {app_id}", category="not_found")

    def list_apps(self) -> list[dict[str, Any]]:
        """Return the read-only Codex app catalog through the CTM-owned service."""

        if self._closed:
            raise ToolFailure("CODEX_BROKER_CRASHED", "Codex Computer observation service is closed.", category="runtime")
        return [app.as_dict() for app in self.provider.list_apps()]

    def request_access(self, app_id: str, *, access: str = "observe", reason: str, ttl_seconds: int = 600) -> dict[str, Any]:
        if self._closed:
            raise ToolFailure("CODEX_BROKER_CRASHED", "Codex Computer observation service is closed.", category="runtime")
        if not reason.strip():
            raise ToolFailure("INVALID_ARGUMENT", "reason is required.", category="validation")
        if access not in {"observe", "control"}:
            raise ToolFailure("INVALID_ARGUMENT", "access must be observe or control.", category="validation")
        if ttl_seconds < 30 or ttl_seconds > 1800:
            raise ToolFailure("INVALID_ARGUMENT", "ttl_seconds must be between 30 and 1800.", category="validation")
        app = self._find_app(app_id)
        scope = {
            "provider": "codex",
            "app": self._app_scope(app),
            "access": access,
            "ttl_seconds": ttl_seconds,
        }
        approval = self.workflow.create_approval(
            tool_name="computer_session_start",
            permission=f"computer_{access}",
            reason=f"{app.display_name or app.app_id} [{app.app_id}] · {access} · {ttl_seconds}s — {reason}",
            arguments_hash=_digest(scope),
            displayed_arguments=scope,
            ttl_seconds=self.APPROVAL_TTL_SECONDS,
        )
        return approval

    def _reap(self) -> None:
        now = time.time()
        expired: list[_ObservationLease] = []
        for lease in self._sessions.values():
            if lease.status == "active" and lease.expires_at <= now:
                lease.status = "expired"
            if lease.status == "expired":
                expired.append(lease)
        for lease in expired:
            lease.provider_session.close()

    def start_session(self, approval_id: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ToolFailure("CODEX_BROKER_CRASHED", "Codex Computer observation service is closed.", category="runtime")
            self._reap()
            approval = self.workflow.get_approval(approval_id)
            if approval["tool_name"] != "computer_session_start":
                raise ToolFailure("APPROVAL_SCOPE_MISMATCH", "This approval does not authorize Codex observation.", category="permission")
            scope = approval["arguments"]
            if isinstance(scope, str):
                scope = json.loads(scope)
            if not isinstance(scope, dict) or scope.get("provider") != "codex" or scope.get("access") not in {"observe", "control"}:
                raise ToolFailure("APPROVAL_SCOPE_MISMATCH", "Codex Computer approval scope is invalid.", category="permission")
            app_scope = scope.get("app")
            if not isinstance(app_scope, dict) or not isinstance(app_scope.get("app_id"), str):
                raise ToolFailure("APPROVAL_SCOPE_MISMATCH", "Codex observation approval has no app identity.", category="permission")
            current = self._find_app(app_scope["app_id"])
            if self._app_scope(current) != app_scope:
                raise ToolFailure(
                    "COMPUTER_APP_CHANGED",
                    "The Codex app identity changed after requesting access. Request access again.",
                    category="permission",
                )
            active = [lease for lease in self._sessions.values() if lease.status == "active"]
            if len(active) >= self.MAX_SESSIONS:
                raise ToolFailure("COMPUTER_SESSION_LIMIT", "Stop an existing Codex observation session first.", category="runtime")
            session_id = "codex_computer_" + uuid.uuid4().hex

            def approve_internal(request: dict[str, Any]) -> bool:
                return self._approve_internal(session_id, request)

            access = str(scope["access"])
            provider_session = self.provider.open_session(current.app_id, access, approve_internal)
            try:
                self.workflow.consume_approvals(
                    [approval_id],
                    tool_name="computer_session_start",
                    arguments_hash=_digest(scope),
                    required_permissions={f"computer_{access}"},
                )
                now = time.time()
                lease = _ObservationLease(
                    session_id=session_id,
                    app=current,
                    provider_session=provider_session,
                    access=access,
                    expires_at=now + int(scope.get("ttl_seconds", 600)),
                    created_at=now,
                )
                self._sessions[session_id] = lease
                return self._lease_payload(lease)
            except BaseException:
                provider_session.close()
                raise

    def _approve_internal(self, session_id: str, request: dict[str, Any]) -> bool:
        with self._lock:
            lease = self._sessions.get(session_id)
            if lease is None or lease.status != "active" or self._closed:
                return False
            if lease.expires_at <= time.time():
                # Do not call provider_session.close() from the app-server
                # reader thread: the foreground call is waiting for this
                # elicitation response while holding the provider transport
                # lock.  Mark it expired and let the normal foreground/session
                # reaper close it after the request unwinds.
                lease.status = "expired"
                return False
            return request.get("app_id") == lease.app.app_id

    @staticmethod
    def _lease_payload(lease: _ObservationLease) -> dict[str, Any]:
        return {
            "ok": True,
            "session_id": lease.session_id,
            "provider": "codex",
            "access": lease.access,
            "status": lease.status,
            "app": lease.app.as_dict(),
            "expires_at": lease.expires_at,
            "created_at": lease.created_at,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            self._reap()
            lease = self._sessions.get(session_id)
            if lease is None:
                raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Codex observation session not found.", category="not_found")
            return self._lease_payload(lease)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._reap()
            return [self._lease_payload(lease) for lease in self._sessions.values()]

    def observe(self, session_id: str, *, disable_diff: bool = True,
                max_text_chars: int = MAX_STATE_TEXT_CHARS, include_image: bool = True) -> dict[str, Any]:
        with self._lock:
            self._reap()
            lease = self._sessions.get(session_id)
            if lease is None:
                raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Codex observation session not found.", category="not_found")
            if lease.status != "active" or self._closed:
                raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex observation session is inactive.", category="permission")
            provider_session = lease.provider_session
        state = provider_session.get_state(disable_diff=disable_diff, max_text_chars=max_text_chars)
        snapshot_id = "codex_snapshot_" + hashlib.sha256(
            (state.app + "\0" + state.text).encode("utf-8", errors="replace")
        ).hexdigest()[:32]
        observed_at = time.time()
        with self._lock:
            lease = self._sessions.get(session_id)
            if lease is None or lease.status != "active":
                raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex observation session is inactive.", category="permission")
            lease.snapshot_id = snapshot_id
            lease.observed_at = observed_at
        result: dict[str, Any] = {
            "ok": True,
            "session_id": session_id,
            "provider": "codex",
            "app": state.app,
            "text": state.text,
            "text_truncated": state.text_truncated,
            "has_screenshot": state.screenshot_url is not None,
            "snapshot_id": snapshot_id,
            "observed_at": observed_at,
        }
        if include_image and state.screenshot_url is not None:
            image = provider_session.read_screenshot(state)
            result["_mcp_image_data"] = base64.b64encode(image.data).decode()
            result["mime_type"] = image.mime_type
        return result

    @staticmethod
    def _validate_interaction(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed: dict[str, tuple[set[str], set[str]]] = {
            "click": ({"element_index"}, {"element_index", "x", "y", "click_count", "mouse_button"}),
            "perform_secondary_action": ({"element_index", "secondary_action"}, {"element_index", "secondary_action"}),
            "set_value": ({"element_index", "value"}, {"element_index", "value"}),
            "select_text": ({"element_index", "text"}, {"element_index", "text", "prefix", "suffix", "selection"}),
            "scroll": ({"element_index", "direction"}, {"element_index", "direction", "pages"}),
            "drag": ({"from_x", "from_y", "to_x", "to_y"}, {"from_x", "from_y", "to_x", "to_y"}),
            "press_key": ({"key"}, {"key"}),
            "type_text": ({"text"}, {"text"}),
        }
        if action not in allowed:
            raise ToolFailure("INVALID_ARGUMENT", f"Unsupported app_interact action: {action}", category="validation")
        required, accepted = allowed[action]
        supplied = {key for key, value in arguments.items() if value is not None}
        missing = sorted(required - supplied)
        extra = sorted(supplied - accepted)
        if missing or extra:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Invalid {action} arguments.",
                category="validation",
                details={"missing": missing, "unexpected": extra},
            )
        if action == "click":
            has_element = "element_index" in supplied
            has_coordinates = {"x", "y"} <= supplied
            partial_coordinates = bool({"x", "y"} & supplied) and not has_coordinates
            if partial_coordinates or (not has_element and not has_coordinates):
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "click requires element_index or both x and y.",
                    category="validation",
                )
        if action == "scroll" and str(arguments.get("direction")) not in {"up", "down", "left", "right"}:
            raise ToolFailure("INVALID_ARGUMENT", "scroll direction must be up, down, left, or right.", category="validation")
        if action == "click" and arguments.get("mouse_button") not in {None, "left", "right", "middle"}:
            raise ToolFailure("INVALID_ARGUMENT", "mouse_button must be left, right, or middle.", category="validation")
        if action == "select_text" and arguments.get("selection") not in {None, "text", "cursor_before", "cursor_after"}:
            raise ToolFailure("INVALID_ARGUMENT", "selection must be text, cursor_before, or cursor_after.", category="validation")
        payload = dict(arguments)
        if action == "perform_secondary_action":
            payload["action"] = payload.pop("secondary_action")
        return payload

    def interact(self, session_id: str, *, snapshot_id: str, action: str,
                 arguments: dict[str, Any], guard: Callable[[], None] | None = None) -> dict[str, Any]:
        normalized = self._validate_interaction(action, arguments)
        with self._lock:
            self._reap()
            lease = self._sessions.get(session_id)
            if lease is None:
                raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Codex Computer session not found.", category="not_found")
            if lease.status != "active" or self._closed:
                raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex Computer session is inactive.", category="permission")
            if lease.access != "control":
                raise ToolFailure("COMPUTER_CONTROL_REQUIRED", "This Codex session permits observation only.", category="permission")
            if lease.snapshot_id != snapshot_id or lease.observed_at is None:
                raise ToolFailure("COMPUTER_SNAPSHOT_STALE", "Observe the app again before acting.", category="conflict")
            if time.time() - lease.observed_at > MAX_SNAPSHOT_AGE_SECONDS:
                raise ToolFailure("COMPUTER_SNAPSHOT_STALE", "The app observation is too old. Observe again before acting.", category="conflict")
            provider_session = lease.provider_session

        # Re-observe immediately before the action and compare accessibility
        # state. This catches ordinary UI drift between model observation and
        # execution without relying on undocumented native element handles.
        current = provider_session.get_state(disable_diff=True, max_text_chars=MAX_STATE_TEXT_CHARS)
        current_snapshot_id = "codex_snapshot_" + hashlib.sha256(
            (current.app + "\0" + current.text).encode("utf-8", errors="replace")
        ).hexdigest()[:32]
        if current_snapshot_id != snapshot_id:
            with self._lock:
                lease = self._sessions.get(session_id)
                if lease is not None:
                    lease.snapshot_id = None
                    lease.observed_at = None
            raise ToolFailure("COMPUTER_SNAPSHOT_STALE", "The app changed after observation. Observe again before acting.", category="conflict")

        # The desktop can revoke the shared session while the pre-action
        # observation is in flight. Recheck the CTM-owned lease immediately
        # before handing the mutating action to Computer Use.
        if guard is not None:
            guard()

        with self._lock:
            lease = self._sessions.get(session_id)
            if lease is None or lease.status != "active":
                raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Codex Computer session is inactive.", category="permission")
            lease.snapshot_id = None
            lease.observed_at = None
        payload = provider_session.interact(action, normalized)
        return {"ok": True, "session_id": session_id, "provider": "codex", "action": action,
                "status": "submitted", "verified": False, "provider_result": payload.get("result")}

    def stop_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            lease = self._sessions.get(session_id)
            if lease is None:
                raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Codex observation session not found.", category="not_found")
            if lease.status == "active":
                lease.status = "stopped"
                lease.provider_session.close()
            return self._lease_payload(lease)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            leases = list(self._sessions.values())
            self._sessions.clear()
        for lease in leases:
            lease.provider_session.close()
        self.provider.close()

