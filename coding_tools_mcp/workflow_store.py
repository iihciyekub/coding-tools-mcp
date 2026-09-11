from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import ToolFailure


TASK_STATES = frozenset({"pending", "running", "waiting_input", "blocked", "completed", "failed", "cancelled"})
TASK_TRANSITIONS = {
    "pending": TASK_STATES,
    "running": TASK_STATES,
    "waiting_input": TASK_STATES,
    "blocked": TASK_STATES,
    "completed": frozenset({"completed", "running"}),
    "failed": frozenset({"failed", "running"}),
    "cancelled": frozenset({"cancelled", "running"}),
}
MAX_CHECKPOINT_FILES = 64
MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
MAX_CHECK_OUTPUT_CHARS = 8_000
MAX_REVIEW_SNAPSHOT_BYTES = 1024 * 1024


def default_state_root() -> Path:
    configured = os.environ.get("CODING_TOOLS_MCP_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / "coding-tools-mcp" if base else Path.home() / "AppData" / "Local" / "coding-tools-mcp"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "coding-tools-mcp"
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base) / "coding-tools-mcp" if base else Path.home() / ".local" / "state" / "coding-tools-mcp"


class WorkflowStore:
    def __init__(self, workspace: Path, state_root: Path | None = None) -> None:
        resolved = workspace.resolve(strict=True)
        self.workspace = resolved
        self.workspace_id = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]
        self.root = (state_root or default_state_root()) / "workspaces" / self.workspace_id
        self.db_path = self.root / "workflow.sqlite3"
        self._lock = threading.RLock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                self.root.chmod(0o700)
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        if not self._ready:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    head TEXT,
                    created_at REAL NOT NULL,
                    total_bytes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoint_files (
                    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    existed INTEGER NOT NULL,
                    content BLOB,
                    mode INTEGER,
                    digest TEXT,
                    PRIMARY KEY (checkpoint_id, path)
                );
                CREATE TABLE IF NOT EXISTS check_runs (
                    check_run_id TEXT PRIMARY KEY,
                    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
                    check_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    workdir TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    command_id TEXT,
                    operation_id TEXT,
                    before_fingerprint TEXT NOT NULL,
                    after_fingerprint TEXT NOT NULL,
                    fingerprint_complete INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    artifact_type TEXT,
                    artifact_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    code_fingerprint TEXT NOT NULL,
                    fingerprint_complete INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS protocol_tasks (
                    task_id TEXT PRIMARY KEY,
                    request_method TEXT NOT NULL,
                    tool_name TEXT,
                    arguments_json TEXT NOT NULL,
                    backing_type TEXT NOT NULL,
                    backing_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_message TEXT,
                    result_json TEXT,
                    error_json TEXT,
                    poll_interval_ms INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS check_runs_task_id ON check_runs(task_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS task_events_task_id ON task_events(task_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS protocol_tasks_backing ON protocol_tasks(backing_type, backing_id);
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

    def create_task(self, title: str, objective: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.time()
        task_id = f"task_{uuid.uuid4().hex}"
        payload = details or {}
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, title, objective, "pending", 1, json.dumps(payload, sort_keys=True), now, now),
            )
            self._insert_event(db, task_id, "created", "Task created.", details={"status": "pending"}, now=now)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise ToolFailure("TASK_NOT_FOUND", f"Task not found: {task_id}", category="not_found")
        return _task_row(row)

    def list_tasks(self, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY updated_at DESC LIMIT ?", (status, limit + 1)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit + 1,)).fetchall()
        return {
            "ok": True,
            "workspace_id": self.workspace_id,
            "tasks": [_task_row(row) for row in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "summary": f"Found {min(len(rows), limit)} tasks.",
        }

    def update_task(
        self,
        task_id: str,
        *,
        expected_revision: int,
        status: str | None,
        title: str | None,
        objective: str | None,
        details: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ToolFailure("TASK_NOT_FOUND", f"Task not found: {task_id}", category="not_found")
            if int(row["revision"]) != expected_revision:
                raise ToolFailure(
                    "TASK_CONFLICT",
                    "Task changed after it was read.",
                    category="conflict",
                    retryable=True,
                    details={"task_id": task_id, "expected_revision": expected_revision, "actual_revision": row["revision"]},
                )
            current_status = str(row["status"])
            next_status = status or current_status
            if next_status not in TASK_STATES or next_status not in TASK_TRANSITIONS[current_status]:
                raise ToolFailure(
                    "INVALID_TASK_TRANSITION",
                    f"Task cannot move from {current_status} to {next_status}.",
                    category="validation",
                )
            merged_details = json.loads(str(row["details_json"]))
            if details is not None:
                merged_details.update(details)
            revision = expected_revision + 1
            db.execute(
                "UPDATE tasks SET title=?, objective=?, status=?, revision=?, details_json=?, updated_at=? WHERE task_id=? AND revision=?",
                (
                    title if title is not None else row["title"],
                    objective if objective is not None else row["objective"],
                    next_status,
                    revision,
                    json.dumps(merged_details, sort_keys=True),
                    time.time(),
                    task_id,
                    expected_revision,
                ),
            )
            if db.total_changes != 1:
                raise ToolFailure("TASK_CONFLICT", "Task changed while it was being updated.", category="conflict", retryable=True)
            self._insert_event(
                db,
                task_id,
                "updated",
                f"Task updated to {next_status} at revision {revision}.",
                details={"status": next_status, "revision": revision},
            )
        return self.get_task(task_id)

    def update_plan(
        self,
        task_id: str,
        *,
        expected_revision: int,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ToolFailure("TASK_NOT_FOUND", f"Task not found: {task_id}", category="not_found")
            if int(row["revision"]) != expected_revision:
                raise ToolFailure(
                    "TASK_CONFLICT",
                    "Task changed after its plan was read.",
                    category="conflict",
                    retryable=True,
                    details={"task_id": task_id, "expected_revision": expected_revision, "actual_revision": row["revision"]},
                )
            details = json.loads(str(row["details_json"]))
            details["plan"] = steps
            revision = expected_revision + 1
            changed = db.execute(
                "UPDATE tasks SET revision=?, details_json=?, updated_at=? WHERE task_id=? AND revision=?",
                (revision, json.dumps(details, sort_keys=True), time.time(), task_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise ToolFailure("TASK_CONFLICT", "Task changed while its plan was updated.", category="conflict", retryable=True)
            self._insert_event(
                db,
                task_id,
                "plan_updated",
                f"Plan updated with {len(steps)} steps at revision {revision}.",
                details={"revision": revision, "step_count": len(steps)},
            )
        task = self.get_task(task_id)
        return {
            "ok": True,
            "task_id": task_id,
            "revision": task["revision"],
            "steps": task["details"].get("plan", []),
            "summary": f"Updated task {task_id} plan to {len(steps)} steps.",
        }

    def create_checkpoint(
        self,
        label: str,
        head: str | None,
        files: list[dict[str, Any]],
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        if not files or len(files) > MAX_CHECKPOINT_FILES:
            raise ToolFailure(
                "CHECKPOINT_SCOPE_INVALID",
                f"Checkpoint requires 1-{MAX_CHECKPOINT_FILES} explicit files.",
                category="validation",
            )
        total = sum(len(item.get("content") or b"") for item in files)
        if total > MAX_CHECKPOINT_BYTES:
            raise ToolFailure(
                "CHECKPOINT_TOO_LARGE",
                "Checkpoint content exceeds the supported size.",
                category="validation",
                details={"bytes": total, "max_bytes": MAX_CHECKPOINT_BYTES},
            )
        checkpoint_id = f"cp_{uuid.uuid4().hex}"
        now = time.time()
        with self._lock, self._connection() as db:
            if task_id is not None:
                self._require_task(db, task_id)
            db.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)",
                (checkpoint_id, label, head, now, total),
            )
            db.executemany(
                "INSERT INTO checkpoint_files VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        checkpoint_id,
                        item["path"],
                        1 if item["existed"] else 0,
                        item.get("content"),
                        item.get("mode"),
                        item.get("digest"),
                    )
                    for item in files
                ],
            )
            if task_id is not None:
                self._insert_event(
                    db,
                    task_id,
                    "checkpoint_created",
                    f"Created checkpoint {label} for {len(files)} files.",
                    artifact_type="checkpoint",
                    artifact_id=checkpoint_id,
                    details={"file_count": len(files), "total_bytes": total},
                    now=now,
                )
        return {
            "ok": True,
            "checkpoint_id": checkpoint_id,
            "label": label,
            "head": head,
            "created_at": now,
            "files": [_checkpoint_file_public(item) for item in files],
            "file_count": len(files),
            "total_bytes": total,
            "summary": f"Created checkpoint {checkpoint_id} for {len(files)} files.",
        }

    def add_task_event(
        self,
        task_id: str,
        event_type: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            self._require_task(db, task_id)
            event_id = self._insert_event(db, task_id, event_type, message, details=details)
            row = db.execute("SELECT * FROM task_events WHERE event_id = ?", (event_id,)).fetchone()
        assert row is not None
        return _event_row(row)

    def task_events(self, task_id: str, *, limit: int = 100) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            self._require_task(db, task_id)
            rows = db.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at DESC, event_id DESC LIMIT ?",
                (task_id, limit + 1),
            ).fetchall()
        return {
            "ok": True,
            "task_id": task_id,
            "events": [_event_row(row) for row in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "summary": f"Found {min(len(rows), limit)} events for task {task_id}.",
        }

    def record_check_run(
        self,
        check: dict[str, Any],
        result: dict[str, Any],
        *,
        before_fingerprint: str,
        after_fingerprint: str,
        fingerprint_complete: bool,
        task_id: str | None,
    ) -> dict[str, Any]:
        check_run_id = f"check_{uuid.uuid4().hex}"
        now = time.time()
        exit_code = result.get("exit_code")
        status = _check_status(result)
        retained = _retained_check_result(result)
        with self._lock, self._connection() as db:
            if task_id is not None:
                self._require_task(db, task_id)
            db.execute(
                "INSERT INTO check_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    check_run_id,
                    task_id,
                    check["id"],
                    check["command"],
                    check["workdir"],
                    check["kind"],
                    status,
                    exit_code,
                    result.get("command_id"),
                    result.get("operation_id"),
                    before_fingerprint,
                    after_fingerprint,
                    1 if fingerprint_complete else 0,
                    json.dumps(retained, sort_keys=True),
                    now,
                ),
            )
            if task_id is not None:
                self._insert_event(
                    db,
                    task_id,
                    "check_finished" if status != "running" else "check_started",
                    f"Check {check['id']} is {status}.",
                    artifact_type="check_run",
                    artifact_id=check_run_id,
                    details={"check_id": check["id"], "status": status, "exit_code": exit_code},
                    now=now,
                )
        return self.get_check_run(check_run_id)

    def update_check_run_result(
        self,
        check_run_id: str,
        result: dict[str, Any],
        *,
        after_fingerprint: str,
        fingerprint_complete: bool,
    ) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM check_runs WHERE check_run_id = ?", (check_run_id,)).fetchone()
            if row is None:
                raise ToolFailure("CHECK_RUN_NOT_FOUND", f"Check run not found: {check_run_id}", category="not_found")
            status = _check_status(result)
            db.execute(
                "UPDATE check_runs SET status=?, exit_code=?, command_id=?, operation_id=?, "
                "after_fingerprint=?, fingerprint_complete=?, result_json=? WHERE check_run_id=?",
                (
                    status,
                    result.get("exit_code"),
                    result.get("command_id") or row["command_id"],
                    result.get("operation_id") or row["operation_id"],
                    after_fingerprint,
                    1 if fingerprint_complete else 0,
                    json.dumps(_retained_check_result(result), sort_keys=True),
                    check_run_id,
                ),
            )
            if row["task_id"] is not None and row["status"] == "running" and status != "running":
                self._insert_event(
                    db,
                    str(row["task_id"]),
                    "check_finished",
                    f"Check {row['check_id']} is {status}.",
                    artifact_type="check_run",
                    artifact_id=check_run_id,
                    details={"check_id": row["check_id"], "status": status, "exit_code": result.get("exit_code")},
                )
        return self.get_check_run(check_run_id)

    def get_check_run(self, check_run_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM check_runs WHERE check_run_id = ?", (check_run_id,)).fetchone()
        if row is None:
            raise ToolFailure("CHECK_RUN_NOT_FOUND", f"Check run not found: {check_run_id}", category="not_found")
        return _check_run_row(row)

    def create_protocol_task(
        self,
        *,
        request_method: str,
        tool_name: str | None,
        arguments: dict[str, Any],
        backing_type: str,
        backing_id: str,
        status_message: str | None = None,
        poll_interval_ms: int = 1000,
    ) -> dict[str, Any]:
        now = time.time()
        task_id = uuid.uuid4().hex
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO protocol_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    request_method,
                    tool_name,
                    json.dumps(arguments, sort_keys=True),
                    backing_type,
                    backing_id,
                    "working",
                    status_message,
                    None,
                    None,
                    poll_interval_ms,
                    now,
                    now,
                ),
            )
        return self.get_protocol_task(task_id)

    def get_protocol_task(self, task_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM protocol_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise ToolFailure("PROTOCOL_TASK_NOT_FOUND", f"Protocol task not found: {task_id}", category="not_found")
        return _protocol_task_row(row)

    def finish_protocol_task(
        self,
        task_id: str,
        *,
        status: str,
        status_message: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ToolFailure("INVALID_ARGUMENT", f"Invalid terminal protocol task status: {status}", category="validation")
        if status == "completed" and result is None:
            raise ToolFailure("INVALID_ARGUMENT", "Completed protocol tasks require a result.", category="validation")
        if status == "failed" and error is None:
            raise ToolFailure("INVALID_ARGUMENT", "Failed protocol tasks require an error.", category="validation")
        with self._lock, self._connection() as db:
            row = db.execute("SELECT status FROM protocol_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise ToolFailure("PROTOCOL_TASK_NOT_FOUND", f"Protocol task not found: {task_id}", category="not_found")
            if str(row["status"]) in {"completed", "failed", "cancelled"}:
                return self.get_protocol_task(task_id)
            db.execute(
                "UPDATE protocol_tasks SET status=?, status_message=?, result_json=?, error_json=?, updated_at=? "
                "WHERE task_id=?",
                (
                    status,
                    status_message,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    json.dumps(error, sort_keys=True) if error is not None else None,
                    time.time(),
                    task_id,
                ),
            )
        return self.get_protocol_task(task_id)

    def task_context(self, task_id: str, *, event_limit: int = 50) -> dict[str, Any]:
        task = self.get_task(task_id)
        events = self.task_events(task_id, limit=event_limit)
        with self._lock, self._connection() as db:
            check_rows = db.execute(
                "SELECT * FROM check_runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 20", (task_id,)
            ).fetchall()
            checkpoint_rows = db.execute(
                "SELECT c.checkpoint_id, c.label, c.head, c.created_at, c.total_bytes, "
                "(SELECT COUNT(*) FROM checkpoint_files f WHERE f.checkpoint_id=c.checkpoint_id) AS file_count "
                "FROM checkpoints c JOIN task_events e ON e.artifact_type='checkpoint' "
                "AND e.artifact_id=c.checkpoint_id WHERE e.task_id=? ORDER BY c.created_at DESC LIMIT 20",
                (task_id,),
            ).fetchall()
            review_rows = db.execute(
                "SELECT * FROM reviews WHERE task_id = ? ORDER BY updated_at DESC LIMIT 20", (task_id,)
            ).fetchall()
        checks = [_check_run_row(row) for row in check_rows]
        checkpoints = [dict(row) for row in checkpoint_rows]
        reviews = [_review_row(row) for row in review_rows]
        return {
            "ok": True,
            "task": task,
            "events": events["events"],
            "checks": checks,
            "checkpoints": checkpoints,
            "reviews": reviews,
            "latest_check_status": checks[0]["status"] if checks else None,
            "summary": (
                f"Task {task_id} is {task['status']} with {len(checks)} recent checks, "
                f"{len(checkpoints)} checkpoints, {len(reviews)} reviews, and {len(events['events'])} events."
            ),
        }

    def create_review(
        self,
        snapshot: dict[str, Any],
        *,
        code_fingerprint: str,
        fingerprint_complete: bool,
        task_id: str | None,
    ) -> dict[str, Any]:
        review_id = f"review_{uuid.uuid4().hex}"
        now = time.time()
        snapshot_json = json.dumps(snapshot, sort_keys=True)
        if len(snapshot_json.encode("utf-8")) > MAX_REVIEW_SNAPSHOT_BYTES:
            raise ToolFailure(
                "REVIEW_TOO_LARGE",
                "Review snapshot exceeds the supported size.",
                category="validation",
                details={"max_bytes": MAX_REVIEW_SNAPSHOT_BYTES},
            )
        with self._lock, self._connection() as db:
            if task_id is not None:
                self._require_task(db, task_id)
            db.execute(
                "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    task_id,
                    "prepared",
                    1,
                    code_fingerprint,
                    1 if fingerprint_complete else 0,
                    snapshot_json,
                    "[]",
                    now,
                    now,
                ),
            )
            if task_id is not None:
                self._insert_event(
                    db,
                    task_id,
                    "review_prepared",
                    f"Prepared review {review_id}.",
                    artifact_type="review",
                    artifact_id=review_id,
                    details={"revision": 1},
                    now=now,
                )
        return self.get_review(review_id)

    def get_review(self, review_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
        if row is None:
            raise ToolFailure("REVIEW_NOT_FOUND", f"Review not found: {review_id}", category="not_found")
        return _review_row(row)

    def record_review(
        self,
        review_id: str,
        *,
        expected_revision: int,
        status: str,
        findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM reviews WHERE review_id = ?", (review_id,)).fetchone()
            if row is None:
                raise ToolFailure("REVIEW_NOT_FOUND", f"Review not found: {review_id}", category="not_found")
            if int(row["revision"]) != expected_revision:
                raise ToolFailure(
                    "REVIEW_CONFLICT",
                    "Review changed after it was read.",
                    category="conflict",
                    retryable=True,
                    details={"expected_revision": expected_revision, "actual_revision": row["revision"]},
                )
            revision = expected_revision + 1
            db.execute(
                "UPDATE reviews SET status=?, revision=?, findings_json=?, updated_at=? "
                "WHERE review_id=? AND revision=?",
                (status, revision, json.dumps(findings, sort_keys=True), time.time(), review_id, expected_revision),
            )
            if db.total_changes != 1:
                raise ToolFailure("REVIEW_CONFLICT", "Review changed while it was updated.", category="conflict", retryable=True)
            if row["task_id"] is not None:
                self._insert_event(
                    db,
                    str(row["task_id"]),
                    "review_recorded",
                    f"Review {review_id} recorded {len(findings)} findings as {status}.",
                    artifact_type="review",
                    artifact_id=review_id,
                    details={"revision": revision, "status": status, "finding_count": len(findings)},
                )
        return self.get_review(review_id)

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
            row = db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None:
                raise ToolFailure("APPROVAL_NOT_FOUND", f"Approval not found: {approval_id}", category="not_found")
            if row["status"] in {"pending", "approved"} and float(row["expires_at"]) <= time.time():
                db.execute(
                    "UPDATE approvals SET status='expired' WHERE approval_id=? AND status IN ('pending','approved')",
                    (approval_id,),
                )
                row = db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        assert row is not None
        return _approval_row(row)

    def list_approvals(self, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE approvals SET status='expired' WHERE status IN ('pending','approved') AND expires_at <= ?",
                (time.time(),),
            )
            if status:
                rows = db.execute(
                    "SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit + 1)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit + 1,)).fetchall()
        return {
            "ok": True,
            "approvals": [_approval_row(row) for row in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "summary": f"Found {min(len(rows), limit)} approval requests.",
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
        with self._lock, self._connection() as db:
            # Serialize the read/validate/consume sequence across runtime
            # processes so a one-shot approval cannot be observed as usable by
            # two callers before either writes its consumed state.
            db.execute("BEGIN IMMEDIATE")
            rows = [
                db.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
                for approval_id in approval_ids
            ]
            available: dict[str, sqlite3.Row] = {}
            now = time.time()
            for approval_id, row in zip(approval_ids, rows, strict=True):
                if row is None:
                    raise ToolFailure("APPROVAL_NOT_FOUND", f"Approval not found: {approval_id}", category="not_found")
                if row["status"] != "approved" or row["consumed_at"] is not None:
                    raise ToolFailure(
                        "APPROVAL_NOT_USABLE",
                        f"Approval {approval_id} is {row['status']} or already consumed.",
                        category="permission",
                    )
                if float(row["expires_at"]) <= now:
                    db.execute("UPDATE approvals SET status='expired' WHERE approval_id=?", (approval_id,))
                    raise ToolFailure("APPROVAL_EXPIRED", f"Approval expired: {approval_id}", category="permission")
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
                        "APPROVAL_NOT_USABLE", "Approval was consumed concurrently.", category="permission"
                    )

    def _require_task(self, db: sqlite3.Connection, task_id: str) -> None:
        if db.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
            raise ToolFailure("TASK_NOT_FOUND", f"Task not found: {task_id}", category="not_found")

    def _insert_event(
        self,
        db: sqlite3.Connection,
        task_id: str,
        event_type: str,
        message: str,
        *,
        artifact_type: str | None = None,
        artifact_id: str | None = None,
        details: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> int:
        cursor = db.execute(
            "INSERT INTO task_events (task_id, event_type, message, artifact_type, artifact_id, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                event_type,
                message,
                artifact_type,
                artifact_id,
                json.dumps(details or {}, sort_keys=True),
                time.time() if now is None else now,
            ),
        )
        if cursor.lastrowid is None:
            raise ToolFailure("WORKFLOW_STORE_ERROR", "Could not create the task event.", category="runtime")
        return int(cursor.lastrowid)

    def list_checkpoints(self, *, limit: int = 100) -> dict[str, Any]:
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT checkpoint_id, label, head, created_at, total_bytes, "
                "(SELECT COUNT(*) FROM checkpoint_files f WHERE f.checkpoint_id=c.checkpoint_id) AS file_count "
                "FROM checkpoints c ORDER BY created_at DESC LIMIT ?",
                (limit + 1,),
            ).fetchall()
        return {
            "ok": True,
            "workspace_id": self.workspace_id,
            "checkpoints": [dict(row) for row in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "summary": f"Found {min(len(rows), limit)} checkpoints.",
        }

    def checkpoint(self, checkpoint_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self._lock, self._connection() as db:
            row = db.execute("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)).fetchone()
            file_rows = db.execute(
                "SELECT path, existed, content, mode, digest FROM checkpoint_files WHERE checkpoint_id = ? ORDER BY path",
                (checkpoint_id,),
            ).fetchall()
        if row is None:
            raise ToolFailure("CHECKPOINT_NOT_FOUND", f"Checkpoint not found: {checkpoint_id}", category="not_found")
        files = [
            {
                "path": item["path"],
                "existed": bool(item["existed"]),
                "content": item["content"],
                "mode": item["mode"],
                "digest": item["digest"],
            }
            for item in file_rows
        ]
        return dict(row), files


def _task_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ok": True,
        "task_id": row["task_id"],
        "title": row["title"],
        "objective": row["objective"],
        "status": row["status"],
        "revision": row["revision"],
        "details": json.loads(str(row["details_json"])),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "summary": f"Task {row['task_id']} is {row['status']} at revision {row['revision']}.",
    }


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "task_id": row["task_id"],
        "event_type": row["event_type"],
        "message": row["message"],
        "artifact_type": row["artifact_type"],
        "artifact_id": row["artifact_id"],
        "details": json.loads(str(row["details_json"])),
        "created_at": row["created_at"],
    }


def _check_run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ok": True,
        "check_run_id": row["check_run_id"],
        "task_id": row["task_id"],
        "check_id": row["check_id"],
        "command": row["command"],
        "workdir": row["workdir"],
        "kind": row["kind"],
        "status": row["status"],
        "exit_code": row["exit_code"],
        "command_id": row["command_id"],
        "operation_id": row["operation_id"],
        "before_fingerprint": row["before_fingerprint"],
        "after_fingerprint": row["after_fingerprint"],
        "fingerprint_complete": bool(row["fingerprint_complete"]),
        "result": json.loads(str(row["result_json"])),
        "created_at": row["created_at"],
        "summary": f"Check {row['check_id']} is {row['status']}.",
    }


def _protocol_task_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "request_method": row["request_method"],
        "tool_name": row["tool_name"],
        "arguments": json.loads(str(row["arguments_json"])),
        "backing_type": row["backing_type"],
        "backing_id": row["backing_id"],
        "status": row["status"],
        "status_message": row["status_message"],
        "result": json.loads(str(row["result_json"])) if row["result_json"] is not None else None,
        "error": json.loads(str(row["error_json"])) if row["error_json"] is not None else None,
        "poll_interval_ms": int(row["poll_interval_ms"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
    }


def _review_row(row: sqlite3.Row) -> dict[str, Any]:
    findings = json.loads(str(row["findings_json"]))
    return {
        "ok": True,
        "review_id": row["review_id"],
        "task_id": row["task_id"],
        "status": row["status"],
        "revision": row["revision"],
        "code_fingerprint": row["code_fingerprint"],
        "fingerprint_complete": bool(row["fingerprint_complete"]),
        "snapshot": json.loads(str(row["snapshot_json"])),
        "findings": findings,
        "finding_count": len(findings),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "summary": f"Review {row['review_id']} is {row['status']} with {len(findings)} findings.",
    }


def _approval_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ok": True,
        "approval_id": row["approval_id"],
        "tool_name": row["tool_name"],
        "permission": row["permission"],
        "reason": row["reason"],
        "arguments": json.loads(str(row["arguments_json"])),
        "status": row["status"],
        "expires_at": row["expires_at"],
        "consumed_at": row["consumed_at"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "summary": f"Approval {row['approval_id']} is {row['status']} for {row['permission']}.",
    }


def _check_status(result: dict[str, Any]) -> str:
    if result.get("status") == "running":
        return "running"
    if result.get("status") in {"interrupted", "unknown"}:
        return "unknown"
    return "passed" if result.get("exit_code") == 0 else "failed"


def _retained_check_result(result: dict[str, Any]) -> dict[str, Any]:
    retained = {
        key: result.get(key)
        for key in (
            "status",
            "exit_code",
            "timed_out",
            "command_id",
            "operation_id",
            "output_ref",
            "stdout_ref",
            "stderr_ref",
            "truncated",
            "diagnostics",
        )
        if result.get(key) is not None
    }
    for key in ("stdout", "stderr"):
        if result.get(key) is not None:
            retained[key] = str(result[key])[:MAX_CHECK_OUTPUT_CHARS]
    return retained


def _checkpoint_file_public(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": item["path"],
        "existed": bool(item["existed"]),
        "bytes": len(item.get("content") or b""),
        "mode": item.get("mode"),
        "digest": item.get("digest"),
    }


def restore_token(checkpoint_id: str, states: list[dict[str, Any]]) -> str:
    normalized = [
        {"path": item["path"], "existed": item["existed"], "digest": item.get("digest"), "mode": item.get("mode")}
        for item in sorted(states, key=lambda value: value["path"])
    ]
    payload = json.dumps({"checkpoint_id": checkpoint_id, "states": normalized}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
