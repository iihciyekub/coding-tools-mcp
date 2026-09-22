from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import ToolFailure


def default_state_root() -> Path:
    """macOS application state root for Coding Tools MCP."""

    try:
        home = Path.home()
    except (OSError, RuntimeError):
        raw_home = os.environ.get("HOME")
        home = Path(raw_home) if raw_home else Path(tempfile.gettempdir())
    return home / "Library" / "Application Support" / "Coding Tools MCP"


class RuntimeStateStore:
    """Small cross-process store for explicit operator approvals.

    Planning, reviews, checks, checkpoints, worktree workflow, and conversation
    memory belong to the model/client. The MCP persists only state required to
    enforce a local security decision.
    """

    def __init__(self, workspace: Path, state_root: Path | None = None) -> None:
        resolved = workspace.resolve(strict=True)
        self.workspace = resolved
        self.workspace_id = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]
        self.root = (state_root or default_state_root()) / "projects" / self.workspace_id
        self.db_path = self.root / "runtime.sqlite3"
        self._lock = threading.RLock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        if not self._ready:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    created_at REAL NOT NULL,
                    decided_at REAL
                );
                CREATE INDEX IF NOT EXISTS approvals_status_created
                    ON approvals(status, created_at DESC);
                """
            )
            db.commit()
            self._ready = True
        return db

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_approval(
        self,
        *,
        tool_name: str,
        permission: str,
        reason: str,
        arguments_hash: str,
        displayed_arguments: dict[str, Any],
        ttl_seconds: int,
    ) -> dict[str, Any]:
        approval_id = f"approval_{uuid.uuid4().hex}"
        now = time.time()
        expires_at = now + ttl_seconds
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    tool_name,
                    permission,
                    reason,
                    arguments_hash,
                    json.dumps(displayed_arguments, sort_keys=True),
                    "pending",
                    expires_at,
                    None,
                    now,
                    None,
                ),
            )
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ToolFailure(
                    "APPROVAL_NOT_FOUND",
                    f"Approval not found: {approval_id}",
                    category="not_found",
                )
            if row["status"] in {"pending", "approved"} and float(row["expires_at"]) <= time.time():
                db.execute(
                    "UPDATE approvals SET status='expired' "
                    "WHERE approval_id=? AND status IN ('pending','approved')",
                    (approval_id,),
                )
                row = db.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
        assert row is not None
        return _approval_row(row)

    def list_approvals(self, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE approvals SET status='expired' "
                "WHERE status IN ('pending','approved') AND expires_at <= ?",
                (time.time(),),
            )
            if status:
                rows = db.execute(
                    "SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit + 1),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?",
                    (limit + 1,),
                ).fetchall()
        return {
            "ok": True,
            "approvals": [_approval_row(row) for row in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
        }

    def consume_approvals(
        self,
        approval_ids: list[str],
        *,
        tool_name: str,
        arguments_hash: str,
        required_permissions: set[str],
    ) -> None:
        if not required_permissions:
            return
        with self._lock:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                rows = [
                    db.execute(
                        "SELECT * FROM approvals WHERE approval_id = ?",
                        (approval_id,),
                    ).fetchone()
                    for approval_id in approval_ids
                ]
                available: dict[str, sqlite3.Row] = {}
                now = time.time()
                for approval_id, row in zip(approval_ids, rows, strict=True):
                    if row is None:
                        raise ToolFailure(
                            "APPROVAL_NOT_FOUND",
                            f"Approval not found: {approval_id}",
                            category="not_found",
                        )
                    if row["status"] != "approved" or row["consumed_at"] is not None:
                        raise ToolFailure(
                            "APPROVAL_NOT_USABLE",
                            f"Approval {approval_id} is {row['status']} or already consumed.",
                            category="permission",
                        )
                    if float(row["expires_at"]) <= now:
                        db.execute(
                            "UPDATE approvals SET status='expired' WHERE approval_id=?",
                            (approval_id,),
                        )
                        raise ToolFailure(
                            "APPROVAL_EXPIRED",
                            f"Approval expired: {approval_id}",
                            category="permission",
                        )
                    if row["tool_name"] != tool_name or row["arguments_hash"] != arguments_hash:
                        raise ToolFailure(
                            "APPROVAL_SCOPE_MISMATCH",
                            "Approval does not match the exact tool arguments.",
                            category="permission",
                        )
                    available[str(row["permission"])] = row
                missing = sorted(required_permissions - set(available))
                if missing:
                    raise ToolFailure(
                        "APPROVAL_SCOPE_MISMATCH",
                        "Approvals do not cover every required permission.",
                        category="permission",
                        details={"missing_permissions": missing},
                    )
                for permission in sorted(required_permissions):
                    row = available[permission]
                    changed = db.execute(
                        "UPDATE approvals SET status='consumed', consumed_at=? "
                        "WHERE approval_id=? AND status='approved' AND consumed_at IS NULL",
                        (now, row["approval_id"]),
                    ).rowcount
                    if changed != 1:
                        raise ToolFailure(
                            "APPROVAL_NOT_USABLE",
                            "Approval was consumed concurrently.",
                            category="permission",
                        )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()


def _approval_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        arguments = json.loads(str(row["arguments_json"]))
    except json.JSONDecodeError:
        arguments = {}
    return {
        "ok": True,
        "approval_id": row["approval_id"],
        "tool_name": row["tool_name"],
        "permission": row["permission"],
        "reason": row["reason"],
        "arguments": arguments,
        "status": row["status"],
        "expires_at": row["expires_at"],
        "consumed_at": row["consumed_at"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }
