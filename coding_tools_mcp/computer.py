"""Desktop computer tools: bounded native calls, explicit approvals and receipts."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from typing import Any, BinaryIO, Iterator, Protocol

from .codex_computer import CodexComputerObservationService
from .computer_contract import HELPER_PROTOCOL_VERSION, NATIVE_OPERATIONS
from .errors import ToolFailure
from .workflow_store import WorkflowStore, default_state_root


MAX_REPLY_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 8
MAX_OPERATIONS_PER_SESSION = 1000
PROTECTED_CONTROL_APPS = frozenset({"com.codingtoolsmcp.desktop", "com.codingtoolsmcp.desktop.app-helper", "com.apple.systempreferences"})


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Backend(Protocol):
    def call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def close(self) -> None: ...


class NativeHelper:
    """One private stdio helper. A timed-out call is never retried implicitly."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.process: subprocess.Popen[bytes] | None = None
        self.replies: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self.lock = threading.Lock()

    def _start(self) -> None:
        if sys.platform != "darwin":
            raise ToolFailure("UNSUPPORTED_PLATFORM", "Computer tools currently require macOS.", category="runtime")
        if self.path is None or not self.path.is_absolute() or not self.path.is_file():
            raise ToolFailure("COMPUTER_UNAVAILABLE", "The bundled computer helper is unavailable. Enable application control in Coding Tools MCP Desktop and restart the service.", category="runtime")
        self.replies = queue.Queue(maxsize=2)
        # Do not pass OAuth credentials or shell startup overrides to the helper.
        env = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG") if key in os.environ}
        try:
            self.process = subprocess.Popen([str(self.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=env)
        except OSError as exc:
            raise ToolFailure("COMPUTER_UNAVAILABLE", "Could not launch the native helper. Rebuild or reinstall the desktop app.", category="runtime") from exc
        stream = self.process.stdout
        assert stream is not None
        replies = self.replies

        def read() -> None:
            try:
                while True:
                    line = stream.readline(MAX_REPLY_BYTES + 1)
                    if not line or len(line) > MAX_REPLY_BYTES or not line.endswith(b"\n"):
                        replies.put(None, timeout=1)
                        return
                    replies.put(line, timeout=1)
            except (OSError, ValueError, queue.Full):
                return

        threading.Thread(target=read, daemon=True, name="computer-helper-output").start()

    def _close(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def close(self) -> None:
        with self.lock:
            self._close()

    def call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation not in NATIVE_OPERATIONS:
            raise ToolFailure("INVALID_ARGUMENT", "Unsupported native operation.", category="validation")
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._close()
                self._start()
            assert self.process is not None and self.process.stdin is not None
            request_id = uuid.uuid4().hex
            data = json.dumps({"version": HELPER_PROTOCOL_VERSION, "id": request_id,
                               "operation": operation, "arguments": arguments}).encode() + b"\n"
            if len(data) > 128 * 1024:
                raise ToolFailure("INVALID_ARGUMENT", "Native request is too large.", category="validation")
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
                line = self.replies.get(timeout=15)
                if line is None:
                    raise ValueError("Native helper closed or exceeded its output limit.")
                reply = json.loads(line)
                if not isinstance(reply, dict) or reply.get("id") != request_id or reply.get("version") != HELPER_PROTOCOL_VERSION:
                    raise ValueError("Native helper protocol mismatch.")
            except (OSError, ValueError, queue.Empty) as exc:
                self._close()
                raise ToolFailure("COMPUTER_HELPER_FAILED", "Native helper failed or timed out. An in-flight action may have completed; inspect state before taking another action.", category="runtime") from exc
            if reply.get("ok") is not True:
                error = reply.get("error", {})
                raise ToolFailure(str(error.get("code", "COMPUTER_HELPER_FAILED")),
                                  str(error.get("message", "Native operation failed.")),
                                  category=str(error.get("category", "runtime")),
                                  retryable=bool(error.get("retryable", False)))
            return reply


class ComputerState:
    """Shares only approval/session metadata with the desktop, never native handles."""

    def __init__(self, workflow: WorkflowStore) -> None:
        self.workflow = workflow
        # Initialize the authoritative workflow database before adding our tables.
        workflow.list_approvals(limit=1)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS computer_sessions (
                    session_id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL,
                    app_json TEXT NOT NULL, access TEXT NOT NULL, status TEXT NOT NULL,
                    helper_id TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS computer_operations (
                    session_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
                    PRIMARY KEY(session_id, operation_id)
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.workflow.db_path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def sessions(self, runtime_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            db.execute("UPDATE computer_sessions SET status='expired' WHERE status='active' AND expires_at<=?", (time.time(),))
            rows = db.execute("SELECT * FROM computer_sessions WHERE runtime_id=? ORDER BY (status='active') DESC,created_at DESC LIMIT 100", (runtime_id,)).fetchall()
        return [self.decode(row) for row in rows]

    @staticmethod
    def decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["app"] = json.loads(result.pop("app_json"))
        return result

    def session(self, session_id: str, runtime_id: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("UPDATE computer_sessions SET status='expired' WHERE status='active' AND expires_at<=?", (time.time(),))
            row = db.execute("SELECT * FROM computer_sessions WHERE session_id=? AND runtime_id=?", (session_id, runtime_id)).fetchone()
        if row is None:
            raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Session belongs to another or previous runtime. Request a new session.", category="not_found")
        return self.decode(row)

    def stop(self, session_id: str, runtime_id: str) -> None:
        self.session(session_id, runtime_id)
        with self.connect() as db:
            db.execute("UPDATE computer_sessions SET status='stopped' WHERE session_id=? AND runtime_id=? AND status='active'", (session_id, runtime_id))


class ComputerService:
    def __init__(self, workflow: WorkflowStore, helper_path: Path | None, *, backend: Backend | None = None,
                 lock_root: Path | None = None, codex_observer: CodexComputerObservationService | None = None) -> None:
        self.workflow = workflow
        self.state = ComputerState(workflow)
        self.backend: Backend = backend or NativeHelper(helper_path)
        self.codex_observer = codex_observer
        self.runtime_id = uuid.uuid4().hex
        self.lock_root = lock_root or default_state_root() / "computer-locks"
        self.locks: dict[str, BinaryIO] = {}
        self.lock = threading.RLock()
        self.native_lock = threading.RLock()
        self.closed = threading.Event()
        self.reaper = threading.Thread(target=self._reap, daemon=True, name="computer-sessions")
        self.reaper.start()

    def _reap(self) -> None:
        while not self.closed.wait(0.25):
            try:
                with self.lock:
                    rows = self.state.sessions(self.runtime_id)
                    by_id = {row["session_id"]: row for row in rows}
                    if self.codex_observer is not None:
                        for observed in self.codex_observer.list_sessions():
                            session_id = str(observed.get("session_id", ""))
                            persisted = by_id.get(session_id)
                            if observed.get("status") == "active":
                                if persisted is None or persisted.get("status") != "active":
                                    self.codex_observer.stop_session(session_id)
                            elif persisted is not None and persisted.get("status") == "active":
                                with self.state.connect() as db:
                                    db.execute(
                                        "UPDATE computer_sessions SET status=? WHERE session_id=? AND runtime_id=? AND status='active'",
                                        (str(observed.get("status", "stopped")), session_id, self.runtime_id),
                                    )
                        rows = self.state.sessions(self.runtime_id)
                    active = {row["session_id"] for row in rows if row["status"] == "active"}
                    for session_id in list(self.locks):
                        if session_id not in active:
                            self.locks.pop(session_id).close()
            except (sqlite3.Error, OSError, ToolFailure):
                continue

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            with self.state.connect() as db:
                db.execute("UPDATE computer_sessions SET status='stopped' WHERE runtime_id=? AND status='active'", (self.runtime_id,))
            for handle in self.locks.values():
                handle.close()
            self.locks.clear()
        self.backend.close()
        if self.codex_observer is not None:
            self.codex_observer.close()
        self.reaper.join(timeout=1)

    def _active(self, session_id: str, *, control: bool = False) -> dict[str, Any]:
        session = self.state.session(session_id, self.runtime_id)
        if session["status"] != "active" or self.closed.is_set():
            raise ToolFailure("COMPUTER_SESSION_INACTIVE", "Session expired or was stopped. Do not continue using its handles.", category="permission")
        if control and session["access"] != "control":
            raise ToolFailure("COMPUTER_CONTROL_REQUIRED", "This session permits observation only. Request control access.", category="permission")
        return session

    def _native(self, session: dict[str, Any], operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Recheck after acquiring the transport slot: a request queued behind a
        # slow capture must not execute after desktop revocation.
        with self.native_lock:
            self._active(session["session_id"], control=operation == "action")
            result = self.backend.call(operation, {**arguments, "app": session["app"], "helper_id": session["helper_id"]})
            self._active(session["session_id"])
        return {key: value for key, value in result.items() if key not in {"id", "version", "helper_instance_id"}}

    def _backend_call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.native_lock:
            return self.backend.call(operation, arguments)

    def _acquire_control_lock(self, app: dict[str, Any]) -> BinaryIO:
        import fcntl

        identity = str(app.get("bundle_id") or app.get("app_id") or "")
        if not identity:
            raise ToolFailure("COMPUTER_APP_CHANGED", "App control identity is unavailable.", category="permission")
        self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = self.lock_root / (digest(identity) + ".lock")
        handle = open(filename, "a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ToolFailure(
                "COMPUTER_APP_BUSY",
                "Another session controls this app. Stop that session first.",
                category="runtime",
                retryable=True,
            ) from exc
        return handle

    def _codex_active(self, session_id: str, *, control: bool = False) -> dict[str, Any]:
        if self.codex_observer is None:
            raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Codex Computer session is unavailable.", category="not_found")
        session = self.state.session(session_id, self.runtime_id)
        if session.get("helper_id") != "codex" or session["status"] != "active" or self.closed.is_set():
            raise ToolFailure(
                "COMPUTER_SESSION_INACTIVE",
                "Codex Computer session expired or was stopped. Request a new session.",
                category="permission",
            )
        if control and session["access"] != "control":
            raise ToolFailure("COMPUTER_CONTROL_REQUIRED", "This Codex session permits observation only.", category="permission")
        return session

    def computer_status(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._backend_call("status", {})
        except ToolFailure as exc:
            return {"ok": True, "available": False, "protocol_version": HELPER_PROTOCOL_VERSION,
                    "providers": {"native": {"available": False, "reason": exc.code},
                                  "codex": {"available": self.codex_observer is not None, "mode": "preview_v2"}},
                    "reason": exc.code, "message": exc.message,
                    "summary": f"Computer control unavailable: {exc.message}"}
        result.update(available=True, protocol_version=HELPER_PROTOCOL_VERSION)
        result["providers"] = {
            "native": {"available": True, "mode": "stable_v1"},
            "codex": {"available": self.codex_observer is not None, "mode": "preview_v2"},
        }
        return result

    def app_list(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("provider", "native") == "codex":
            if self.codex_observer is None:
                raise ToolFailure(
                    "CODEX_CAPABILITY_UNAVAILABLE",
                    "Codex Computer provider requires host mode and an installed Desktop Codex runtime.",
                    category="permission",
                )
            query = str(args.get("query", "")).casefold()
            max_results = int(args.get("max_results", 30))
            apps = self.codex_observer.list_apps()
            if query:
                apps = [app for app in apps if query in str(app.get("app_id", "")).casefold()
                        or query in str(app.get("display_name", "")).casefold()]
            return {"ok": True, "provider": "codex", "apps": apps[:max_results],
                    "count": min(len(apps), max_results), "truncated": len(apps) > max_results}
        return self._backend_call("list_apps", args)

    def computer_request_access(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("provider", "native") == "codex":
            if self.codex_observer is None:
                raise ToolFailure(
                    "CODEX_CAPABILITY_UNAVAILABLE",
                    "Codex Computer provider requires host mode and an installed Desktop Codex runtime.",
                    category="permission",
                )
            if args["access"] == "control" and str(args["app_id"]) in PROTECTED_CONTROL_APPS:
                raise ToolFailure(
                    "COMPUTER_PROTECTED_APP",
                    "Desktop approval controls and macOS permission settings are human-only. Their applications cannot receive a control session.",
                    category="permission",
                )
            approval = self.codex_observer.request_access(
                str(args["app_id"]), access=str(args["access"]), reason=str(args["reason"]),
                ttl_seconds=int(args.get("ttl_seconds", 600))
            )
            approval["next_action"] = {
                "tool": "computer_session_get",
                "arguments": {"approval_id": approval["approval_id"]},
                "message": "Approve this Codex app request in Coding Tools MCP Desktop, then start the session using approval_id.",
            }
            return approval
        app = self._backend_call("resolve_app", {"app_id": args["app_id"]})["app"]
        if args["access"] == "control" and app.get("bundle_id") in PROTECTED_CONTROL_APPS:
            raise ToolFailure("COMPUTER_PROTECTED_APP", "Desktop approval controls and macOS permission settings are human-only. Their applications cannot receive a control session.", category="permission")
        scope = {"provider": "native", "app": app, "access": args["access"], "ttl_seconds": args.get("ttl_seconds", 600)}
        approval = self.workflow.create_approval(
            tool_name="computer_session_start", permission=f"computer_{args['access']}", reason=f"{app['name']} [{app.get('bundle_id') or app['app_id']}; PID {app['pid']}] · {scope['access']} · {scope['ttl_seconds']}s — {args['reason']}",
            arguments_hash=digest(scope), displayed_arguments=scope, ttl_seconds=300,
        )
        approval["next_action"] = {"tool": "computer_session_get", "arguments": {"approval_id": approval["approval_id"]},
                                   "message": "Approve this app request in Coding Tools MCP Desktop, then start the session using approval_id."}
        return approval

    def computer_session_start(self, args: dict[str, Any]) -> dict[str, Any]:
        approval = self.workflow.get_approval(args["approval_id"])
        scope = approval["arguments"]
        if isinstance(scope, str):
            scope = json.loads(scope)
        if isinstance(scope, dict) and scope.get("provider") == "codex":
            if self.codex_observer is None:
                raise ToolFailure("CODEX_CAPABILITY_UNAVAILABLE", "Codex Computer provider is unavailable.", category="runtime")
            handle: BinaryIO | None = None
            if scope.get("access") == "control":
                app_scope = scope.get("app")
                if not isinstance(app_scope, dict):
                    raise ToolFailure("APPROVAL_SCOPE_MISMATCH", "Codex control approval has no app identity.", category="permission")
                handle = self._acquire_control_lock(app_scope)
            session_id: str | None = None
            try:
                result = self.codex_observer.start_session(str(args["approval_id"]))
                session_id = str(result["session_id"])
                app = result.get("app") if isinstance(result.get("app"), dict) else scope.get("app", {})
                app = dict(app) if isinstance(app, dict) else {}
                app["provider"] = "codex"
                app.setdefault("name", app.get("display_name") or app.get("app_id") or "Application")
                with self.state.connect() as db:
                    db.execute(
                        "INSERT INTO computer_sessions VALUES (?,?,?,?,?,?,?,?)",
                        (
                            session_id,
                            self.runtime_id,
                            json.dumps(app, sort_keys=True),
                            str(result.get("access", scope.get("access", "observe"))),
                            str(result.get("status", "active")),
                            "codex",
                            float(result["expires_at"]),
                            float(result["created_at"]),
                        ),
                    )
                if handle is not None:
                    self.locks[session_id] = handle
                return {**result, "app": app}
            except BaseException:
                if session_id is not None:
                    try:
                        self.codex_observer.stop_session(session_id)
                    except ToolFailure:
                        pass
                if handle is not None:
                    handle.close()
                raise
        with self.lock:
            if approval["tool_name"] != "computer_session_start":
                raise ToolFailure("APPROVAL_SCOPE_MISMATCH", "This approval does not authorize app control.", category="permission")
            current = self._backend_call("resolve_app", {"app_id": scope["app"]["app_id"]})
            if current["app"] != scope["app"]:
                raise ToolFailure("COMPUTER_APP_CHANGED", "The app instance changed after requesting access. Request access again.", category="permission")
            if len([s for s in self.state.sessions(self.runtime_id) if s["status"] == "active"]) >= MAX_SESSIONS:
                raise ToolFailure("COMPUTER_SESSION_LIMIT", "Stop an existing app session before starting another.", category="runtime")
            handle: BinaryIO | None = None
            if scope["access"] == "control":
                handle = self._acquire_control_lock(scope["app"])
            try:
                self.workflow.consume_approvals([args["approval_id"]], tool_name="computer_session_start",
                                               arguments_hash=digest(scope), required_permissions={f"computer_{scope['access']}"})
                session_id = "computer_" + uuid.uuid4().hex
                now = time.time()
                with self.state.connect() as db:
                    db.execute("INSERT INTO computer_sessions VALUES (?,?,?,?,?,?,?,?)", (
                        session_id, self.runtime_id, json.dumps(scope["app"], sort_keys=True), scope["access"],
                        "active", current["helper_instance_id"], now + scope["ttl_seconds"], now))
                if handle is not None:
                    self.locks[session_id] = handle
            except BaseException:
                if handle is not None:
                    handle.close()
                raise
            return {"ok": True, **self.state.session(session_id, self.runtime_id)}

    def computer_session_get(self, args: dict[str, Any]) -> dict[str, Any]:
        if args.get("approval_id"):
            if args.get("session_id") or args.get("operation_id"):
                raise ToolFailure("INVALID_ARGUMENT", "Query an approval or a session, not both.", category="validation")
            approval = self.workflow.get_approval(args["approval_id"])
            if approval["tool_name"] != "computer_session_start":
                raise ToolFailure("APPROVAL_SCOPE_MISMATCH", "Not an app access approval.", category="permission")
            return approval
        if "session_id" not in args:
            if "operation_id" in args:
                raise ToolFailure("INVALID_ARGUMENT", "operation_id requires session_id.", category="validation")
            sessions = self.state.sessions(self.runtime_id)
            return {"ok": True, "sessions": sessions}
        if str(args["session_id"]).startswith("codex_computer_"):
            persisted = self.state.session(str(args["session_id"]), self.runtime_id)
            result: dict[str, Any] = {"ok": True, **persisted, "provider": "codex"}
            if self.codex_observer is not None:
                try:
                    live = self.codex_observer.get_session(str(args["session_id"]))
                    result.update({key: value for key, value in live.items() if key not in {"app", "status"}})
                except ToolFailure:
                    pass
            if args.get("operation_id"):
                with self.state.connect() as db:
                    row = db.execute(
                        "SELECT status,result_json FROM computer_operations WHERE session_id=? AND operation_id=?",
                        (args["session_id"], args["operation_id"]),
                    ).fetchone()
                result["operation"] = {"status": "not_found"}
                if row:
                    result["operation"] = {
                        "status": row["status"],
                        "result": json.loads(row["result_json"]) if row["result_json"] else None,
                    }
            return result
        result = {"ok": True, **self.state.session(args["session_id"], self.runtime_id)}
        if args.get("operation_id"):
            with self.state.connect() as db:
                row = db.execute("SELECT status,result_json FROM computer_operations WHERE session_id=? AND operation_id=?", (args["session_id"], args["operation_id"])).fetchone()
            result["operation"] = dict(row) if row else {"status": "not_found"}
            if row:
                result["operation"] = {"status": row["status"], "result": json.loads(row["result_json"]) if row["result_json"] else None}
        return result

    def computer_session_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        if str(args["session_id"]).startswith("codex_computer_"):
            if self.codex_observer is None:
                raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Codex observation session is unavailable.", category="not_found")
            self.state.stop(str(args["session_id"]), self.runtime_id)
            result = self.codex_observer.stop_session(str(args["session_id"]))
            with self.lock:
                handle = self.locks.pop(str(args["session_id"]), None)
                if handle is not None:
                    handle.close()
            return {**result, "status": "stopped"}
        self.state.stop(args["session_id"], self.runtime_id)
        with self.lock:
            handle = self.locks.pop(args["session_id"], None)
            if handle is not None:
                handle.close()
        return {"ok": True, **self.state.session(args["session_id"], self.runtime_id)}

    def app_windows(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._native(self._active(args["session_id"]), "windows", {})

    def app_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._active(args["session_id"])
        result = self._native(session, "snapshot", args)
        self._active(session["session_id"])
        return result

    def app_observe(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args["session_id"])
        if not session_id.startswith("codex_computer_"):
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "app_observe currently requires a Codex provider observation session; use app_snapshot for Native Computer v1.",
                category="validation",
            )
        if self.codex_observer is None:
            raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Codex observation session is unavailable.", category="not_found")
        self._codex_active(session_id)
        result = self.codex_observer.observe(
            session_id,
            disable_diff=bool(args.get("disable_diff", True)),
            max_text_chars=int(args.get("max_text_chars", 50000)),
            include_image=bool(args.get("include_image", True)),
        )
        self._codex_active(session_id)
        return result

    def _operation_begin(self, session_id: str, operation_id: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        fingerprint = digest(arguments)
        with self.state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM computer_operations WHERE session_id=? AND operation_id=?",
                (session_id, operation_id),
            ).fetchone()
            if row:
                if row["arguments_hash"] != fingerprint:
                    raise ToolFailure(
                        "OPERATION_CONFLICT",
                        "operation_id was used with different arguments.",
                        category="validation",
                    )
                if row["result_json"]:
                    return {**json.loads(row["result_json"]), "replayed": True}
                raise ToolFailure(
                    "COMPUTER_ACTION_UNKNOWN",
                    "This action has no confirmed result. Inspect the app; do not repeat it.",
                    category="runtime",
                )
            count = db.execute(
                "SELECT count(*) FROM computer_operations WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            if count >= MAX_OPERATIONS_PER_SESSION:
                raise ToolFailure(
                    "COMPUTER_OPERATION_LIMIT",
                    "This session reached its action limit. Stop it and request a new approved session.",
                    category="runtime",
                )
            db.execute(
                "INSERT INTO computer_operations VALUES (?,?,?,?,?)",
                (session_id, operation_id, fingerprint, "pending", None),
            )
        return None

    def _operation_unknown(self, session_id: str, operation_id: str) -> None:
        with self.state.connect() as db:
            db.execute(
                "UPDATE computer_operations SET status='unknown' WHERE session_id=? AND operation_id=?",
                (session_id, operation_id),
            )

    def _operation_complete(self, session_id: str, operation_id: str, result: dict[str, Any]) -> dict[str, Any]:
        completed = {**result, "operation_id": operation_id, "verified": False}
        with self.state.connect() as db:
            db.execute(
                "UPDATE computer_operations SET status='completed',result_json=? WHERE session_id=? AND operation_id=?",
                (json.dumps(completed), session_id, operation_id),
            )
        return completed

    def app_interact(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args["session_id"])
        operation_id = str(args["operation_id"])
        if not session_id.startswith("codex_computer_"):
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "app_interact currently requires a Codex provider control session; use app_action for Native Computer v1.",
                category="validation",
            )
        if self.codex_observer is None:
            raise ToolFailure("COMPUTER_SESSION_NOT_FOUND", "Codex Computer session is unavailable.", category="not_found")
        with self.lock:
            self._codex_active(session_id, control=True)
            replay = self._operation_begin(session_id, operation_id, args)
            if replay is not None:
                return replay
            excluded = {"session_id", "snapshot_id", "action", "operation_id"}
            arguments = {key: value for key, value in args.items() if key not in excluded}
            try:
                result = self.codex_observer.interact(
                    session_id,
                    snapshot_id=str(args["snapshot_id"]),
                    action=str(args["action"]),
                    arguments=arguments,
                    guard=lambda: self._codex_active(session_id, control=True),
                )
            except BaseException:
                self._operation_unknown(session_id, operation_id)
                raise
            return self._operation_complete(session_id, operation_id, result)

    def app_action(self, args: dict[str, Any]) -> dict[str, Any]:
        if (args["action"] == "set_value") != ("value" in args):
            raise ToolFailure("INVALID_ARGUMENT", "set_value requires value; press must not include value.", category="validation")
        with self.lock:
            session = self._active(args["session_id"], control=True)
            replay = self._operation_begin(str(args["session_id"]), str(args["operation_id"]), args)
            if replay is not None:
                return replay
            try:
                result = self._native(session, "action", args)
            except BaseException:
                self._operation_unknown(str(args["session_id"]), str(args["operation_id"]))
                raise
            return self._operation_complete(str(args["session_id"]), str(args["operation_id"]), result)

    def app_wait(self, args: dict[str, Any]) -> dict[str, Any]:
        if (args["condition"] == "value_equals") != ("value" in args):
            raise ToolFailure("INVALID_ARGUMENT", "value_equals requires value; enabled must not include value.", category="validation")
        deadline = time.monotonic() + args.get("timeout_ms", 3000) / 1000
        while True:
            session = self._active(args["session_id"])
            result = self._native(session, "inspect", args)
            self._active(args["session_id"])
            element = result["element"]
            matched = element.get("enabled") is True if args["condition"] == "enabled" else element.get("value") == args["value"]
            if matched or time.monotonic() >= deadline:
                return {"ok": True, "status": "satisfied" if matched else "timed_out", "element": element}
            self.closed.wait(min(0.2, max(0, deadline - time.monotonic())))


def render_computer_result(payload: dict[str, Any]) -> str:
    """Keep handles and recovery state visible to clients that omit structuredContent."""
    public = {key: value for key, value in payload.items() if not key.startswith("_") and key not in {"id", "version", "runtime_id", "helper_id", "helper_instance_id"}}
    return json.dumps(public, ensure_ascii=False, separators=(",", ":"))
