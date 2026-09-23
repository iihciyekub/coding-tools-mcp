"""Persistent project gateway primitives.

The core Runtime intentionally keeps an immutable workspace. This module adds
the routing layer above it: one long-lived HTTP gateway can bind independent
MCP sessions to different immutable project runtimes without making the
underlying Runtime workspace mutable or coupling project selection to the
desktop frontmost window.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .local_capabilities import LocalCapabilityCatalog
from .tool_results import make_tool_result


DEFAULT_SESSION_TTL_SECONDS = 24 * 60 * 60

RETRYABLE_READ_TOOLS = frozenset({
    "server_info", "read_file", "read_files", "list_dir", "list_files", "search_text",
    "get_command", "list_commands", "read_output", "git_status", "git_diff",
    "code_diagnostics", "workspace_overview", "project_instructions", "view_image",
    "code_symbols", "code_definition", "code_references",
})


def _loopback_endpoint(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("project endpoint must be an http loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("project endpoint must not contain credentials, query, or fragment")
    if parsed.path.rstrip("/") != "/mcp":
        raise ValueError("project endpoint path must be /mcp")
    if parsed.port is None:
        raise ValueError("project endpoint requires an explicit port")
    return raw.rstrip("/")


class HTTPProjectRuntime:
    """Thin loopback proxy preserving an isolated project's existing Runtime."""

    def __init__(self, definition: "ProjectDefinition") -> None:
        if definition.endpoint is None:
            raise ValueError("HTTPProjectRuntime requires a project endpoint")
        self.definition = definition
        self.endpoint = definition.endpoint
        self._counter = 0
        self._lock = threading.Lock()
        self._state = "configured"

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set_state(self, value: str) -> None:
        with self._lock:
            self._state = value

    def _next_id(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = self._next_id()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body_params = dict(params)
        headers["MCP-Protocol-Version"] = "2025-11-25"
        data = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": body_params,
            }
        ).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        rpc_timeout = 30.0
        if method == "tools/call":
            tool_name = str(body_params.get("name") or "")
            raw_arguments = body_params.get("arguments")
            arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
            if tool_name in {"exec_command", "write_stdin"}:
                yield_ms = int(arguments.get("yield_time_ms", 10000))
                # The child runtime may legitimately hold the request for the
                # whole yield window before returning a command handle. Leave
                # transport headroom so a 30s yield is not mistaken for an
                # unavailable project runtime.
                rpc_timeout = max(rpc_timeout, min(35.0, (yield_ms / 1000.0) + 5.0))
        try:
            with urllib.request.urlopen(request, timeout=rpc_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Project runtime is unavailable for {self.definition.id}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Project runtime returned a non-object JSON-RPC response")
        error = payload.get("error")
        if isinstance(error, dict):
            raise RuntimeError(str(error.get("message") or "Project runtime RPC failed"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Project runtime response did not contain an object result")
        return result

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        del context
        tool_arguments = arguments or {}
        retry_safe = (
            name in RETRYABLE_READ_TOOLS
            or (name == "exec_command" and bool(tool_arguments.get("operation_id")))
            or (name == "apply_patch" and bool(tool_arguments.get("idempotency_key")))
        )
        attempts = 2 if retry_safe else 1
        last_error: RuntimeError | None = None
        for attempt in range(attempts):
            if attempt:
                self._set_state("recovering")
            try:
                result = self._rpc(
                    "tools/call",
                    {"name": name, "arguments": tool_arguments},
                )
                self._set_state("ready")
                return result
            except RuntimeError as exc:
                last_error = exc
                self._set_state("unreachable")
        assert last_error is not None
        payload = {
            "ok": False,
            "error": {
                "code": "PROJECT_RUNTIME_UNAVAILABLE",
                "message": str(last_error),
                "category": "runtime",
                "retryable": True,
                "details": {
                    "project_id": self.definition.id,
                    "endpoint": self.endpoint,
                    "attempts": attempts,
                },
            },
        }
        return make_tool_result(name, payload, is_error=True)

    def close(self) -> None:
        return


@dataclass(frozen=True)
class ProjectDefinition:
    id: str
    name: str
    path: Path
    endpoint: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProjectDefinition":
        raw_id = str(value.get("id") or "").strip()
        raw_name = str(value.get("name") or "").strip()
        raw_path = str(value.get("path") or "").strip()
        raw_endpoint = str(value.get("endpoint") or "").strip()
        if not raw_id:
            raise ValueError("project id is required")
        if len(raw_id) > 200:
            raise ValueError("project id must be at most 200 characters")
        if not raw_path:
            raise ValueError(f"project {raw_id!r} requires a path")
        path = Path(raw_path).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f"project path is not a directory: {path}")
        endpoint = _loopback_endpoint(raw_endpoint) if raw_endpoint else None
        return cls(
            id=raw_id,
            name=raw_name or path.name or raw_id,
            path=path,
            endpoint=endpoint,
        )

    def payload(self, *, runtime_state: str) -> dict[str, Any]:
        return {
            "project_id": self.id,
            "name": self.name,
            "root": str(self.path),
            "runtime_state": runtime_state,
        }


@dataclass
class SessionContext:
    session_id: str
    selected_project_id: str | None
    created_at: float
    last_seen: float


class SessionRegistry:
    """In-memory MCP session routing state only."""

    def __init__(self, *, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._items: dict[str, SessionContext] = {}
        self._lock = threading.RLock()

    def create(self, *, selected_project_id: str | None = None) -> SessionContext:
        now = time.time()
        context = SessionContext(
            session_id=uuid.uuid4().hex,
            selected_project_id=selected_project_id,
            created_at=now,
            last_seen=now,
        )
        with self._lock:
            self._prune_locked(now)
            self._items[context.session_id] = context
        return context

    def get(self, session_id: str) -> SessionContext | None:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            context = self._items.get(session_id)
            if context is not None:
                context.last_seen = now
            return context

    def select(self, session_id: str, project_id: str | None) -> SessionContext | None:
        with self._lock:
            context = self.get(session_id)
            if context is None:
                return None
            context.selected_project_id = project_id
            return context

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._items.pop(session_id, None) is not None

    def clear_project(self, project_id: str) -> None:
        with self._lock:
            for context in self._items.values():
                if context.selected_project_id == project_id:
                    context.selected_project_id = None

    def _prune_locked(self, now: float) -> None:
        expired = [
            session_id
            for session_id, context in self._items.items()
            if now - context.last_seen > self.ttl_seconds
        ]
        for session_id in expired:
            self._items.pop(session_id, None)


class ProjectRegistry:
    """Hot-reloadable project definitions plus lazily-created runtimes."""

    def __init__(
        self,
        bootstrap: ProjectDefinition | None,
        bootstrap_runtime: Any | None,
        *,
        runtime_factory: Callable[[ProjectDefinition], Any],
        registry_file: Path | None = None,
    ) -> None:
        self.bootstrap = bootstrap
        self.runtime_factory = runtime_factory
        self.registry_file = registry_file.expanduser() if registry_file else None
        self._definitions: dict[str, ProjectDefinition] = (
            {bootstrap.id: bootstrap} if bootstrap is not None else {}
        )
        self._runtimes: dict[str, Any] = (
            {bootstrap.id: bootstrap_runtime}
            if bootstrap is not None and bootstrap_runtime is not None
            else {}
        )
        self._owned_runtime_ids: set[str] = set()
        self._default_project_id: str | None = bootstrap.id if bootstrap is not None else None
        self._registry_mtime_ns: int | None = None
        self._generation: int | str | None = None
        self._lock = threading.RLock()
        self.refresh(force=True)

    @property
    def default_project_id(self) -> str | None:
        with self._lock:
            return self._default_project_id

    @property
    def generation(self) -> int | str | None:
        with self._lock:
            return self._generation

    def refresh(self, *, force: bool = False) -> set[str]:
        path = self.registry_file
        if path is None:
            if self.bootstrap is None or (self.bootstrap.path / ".git").exists():
                return set()
            discovered: dict[str, ProjectDefinition] = {}
            try:
                children = sorted(self.bootstrap.path.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                return set()
            for child in children:
                if child.name.startswith(".") or not child.is_dir() or not (child / ".git").exists():
                    continue
                resolved = child.resolve(strict=True)
                project_id = "project-" + hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
                discovered[project_id] = ProjectDefinition(project_id, resolved.name, resolved)
            if not discovered:
                discovered = {self.bootstrap.id: self.bootstrap}
                default_project_id: str | None = self.bootstrap.id
            elif len(discovered) == 1:
                default_project_id = next(iter(discovered))
            else:
                default_project_id = None
            with self._lock:
                removed = set(self._definitions) - set(discovered)
                self._definitions = discovered
                self._default_project_id = default_project_id
                for project_id in removed:
                    runtime = self._runtimes.pop(project_id, None)
                    if runtime is not None and project_id in self._owned_runtime_ids:
                        close = getattr(runtime, "close", None)
                        if callable(close):
                            close()
                    self._owned_runtime_ids.discard(project_id)
                return removed
        try:
            stat = path.stat()
        except FileNotFoundError:
            raw: dict[str, Any] = {}
            mtime_ns = -1
        else:
            mtime_ns = stat.st_mtime_ns
            if not force and self._registry_mtime_ns == mtime_ns:
                return set()
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("project registry must be a JSON object")
            raw = parsed

        definitions: dict[str, ProjectDefinition] = (
            {self.bootstrap.id: self.bootstrap} if self.bootstrap is not None else {}
        )
        raw_projects = raw.get("projects", [])
        if not isinstance(raw_projects, list):
            raise ValueError("project registry projects must be an array")
        for item in raw_projects:
            if not isinstance(item, dict):
                raise ValueError("each project registry entry must be an object")
            definition = ProjectDefinition.from_mapping(item)
            existing = definitions.get(definition.id)
            if existing is not None and existing.path != definition.path:
                raise ValueError(f"duplicate project id with different path: {definition.id}")
            definitions[definition.id] = definition

        raw_default = raw.get("default_project_id")
        default_project_id = (
            str(raw_default).strip()
            if raw_default is not None
            else (self.bootstrap.id if self.bootstrap is not None else "")
        )
        if default_project_id and default_project_id not in definitions:
            raise ValueError(f"default project is not registered: {default_project_id}")

        with self._lock:
            removed = set(self._definitions) - set(definitions)
            changed = {
                project_id
                for project_id, definition in definitions.items()
                if project_id in self._definitions
                and (
                    self._definitions[project_id].path != definition.path
                    or self._definitions[project_id].endpoint != definition.endpoint
                )
            }
            invalidated = removed | changed
            for project_id in invalidated:
                runtime = self._runtimes.pop(project_id, None)
                if runtime is not None and project_id in self._owned_runtime_ids:
                    close = getattr(runtime, "close", None)
                    if callable(close):
                        close()
                self._owned_runtime_ids.discard(project_id)
            self._definitions = definitions
            self._default_project_id = default_project_id or None
            self._registry_mtime_ns = mtime_ns
            self._generation = raw.get("generation")
            return removed

    def definitions(self) -> list[ProjectDefinition]:
        self.refresh()
        with self._lock:
            return sorted(self._definitions.values(), key=lambda item: (item.name.casefold(), item.id))

    def get(self, project_id: str) -> ProjectDefinition | None:
        self.refresh()
        with self._lock:
            return self._definitions.get(project_id)

    def resolve_selector(self, selector: str) -> tuple[ProjectDefinition | None, list[ProjectDefinition]]:
        """Resolve one agent-friendly project selector without exposing registry internals."""
        self.refresh()
        raw = selector.strip()
        folded = raw.casefold()
        resolved_selector: Path | None = None
        if "/" in raw or raw.startswith("~"):
            try:
                resolved_selector = Path(raw).expanduser().resolve(strict=False)
            except OSError:
                resolved_selector = None
        with self._lock:
            exact = self._definitions.get(raw)
            if exact is not None:
                return exact, [exact]
            matches = [
                definition
                for definition in self._definitions.values()
                if definition.name.casefold() == folded
                or definition.path.name.casefold() == folded
                or definition.path == resolved_selector
            ]
        unique = {definition.id: definition for definition in matches}
        ordered = sorted(unique.values(), key=lambda item: (item.name.casefold(), item.id))
        return (ordered[0] if len(ordered) == 1 else None), ordered

    def runtime_state(self, project_id: str) -> str:
        with self._lock:
            definition = self._definitions.get(project_id)
            if definition is not None and definition.endpoint is not None:
                runtime = self._runtimes.get(project_id)
                return runtime.state if isinstance(runtime, HTTPProjectRuntime) else "configured"
            return "running" if project_id in self._runtimes else "stopped"

    def runtime_for(self, project_id: str) -> Any | None:
        self.refresh()
        with self._lock:
            definition = self._definitions.get(project_id)
            if definition is None:
                return None
            runtime = self._runtimes.get(project_id)
            if runtime is None:
                runtime = self.runtime_factory(definition)
                self._runtimes[project_id] = runtime
                self._owned_runtime_ids.add(project_id)
            return runtime

    def close(self) -> None:
        with self._lock:
            owned = [
                runtime
                for project_id, runtime in self._runtimes.items()
                if project_id in self._owned_runtime_ids
            ]
            self._owned_runtime_ids.clear()
        for runtime in owned:
            close = getattr(runtime, "close", None)
            if callable(close):
                close()


class GatewayRuntime:
    """Protocol-compatible facade for one persistent multi-project gateway."""

    def __init__(
        self,
        control_runtime: Any,
        project_registry: ProjectRegistry,
        session_registry: SessionRegistry,
        *,
        project_tool_definition: Callable[[], dict[str, Any]],
        local_capabilities: LocalCapabilityCatalog | None = None,
        local_tool_definition: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.control_runtime = control_runtime
        self.projects = project_registry
        self.sessions = session_registry
        self._project_tool_definition = project_tool_definition
        self.local_capabilities = local_capabilities
        self._local_tool_definition = local_tool_definition
        self.enable_project_gateway = True
        self.enable_local_capabilities = local_capabilities is not None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.control_runtime, name)

    @property
    def telemetry(self) -> Any:
        return self.control_runtime.telemetry

    def bind(self, session_id: str | None) -> "BoundGatewayRuntime":
        return BoundGatewayRuntime(self, session_id)

    def refresh_projects(self) -> None:
        removed = self.projects.refresh()
        for project_id in removed:
            self.sessions.clear_project(project_id)

    def exposed_tool_names(self) -> list[str]:
        local = ["local_capabilities_search", "local_skill_read", "local_plugin_inspect"] if self.local_capabilities else []
        return ["project_context", *local, *self.control_runtime.exposed_tool_names()]

    def list_tools(self) -> dict[str, Any]:
        payload = self.control_runtime.list_tools()
        tools = list(payload.get("tools", []))
        tools.insert(0, self._project_tool_definition())
        if self.local_capabilities and self._local_tool_definition:
            tools[1:1] = [self._local_tool_definition(name) for name in (
                "local_capabilities_search", "local_skill_read", "local_plugin_inspect"
            )]
        return {"tools": tools}

    def tool_usage_instructions(self) -> str:
        base = (
            "This endpoint is a persistent project gateway. Use project_context to inspect or explicitly select "
            "the project for this MCP session. Project selection never follows the desktop frontmost window. "
            "After a project is selected, relative paths and default searches are rooted in that project's "
            "immutable workspace. Full Access changes the maximum permission scope, not the default search root. "
            + self.control_runtime.tool_usage_instructions()
        )
        if self.local_capabilities:
            base += (
                " When asked to use a named local Skill or inspect a local plugin, search the authorized catalog "
                "and read the matching Skill before acting. Reading plugin metadata does not make its tools "
                "or hooks available; use only separately exposed MCP tools for actions."
            )
        return base

    def discover_payload(self) -> dict[str, Any]:
        payload = dict(self.control_runtime.discover_payload())
        payload["instructions"] = self.tool_usage_instructions()
        return payload

    def close(self) -> None:
        self.projects.close()
        self.control_runtime.close()


class BoundGatewayRuntime:
    """Per-request facade carrying one transport session identity."""

    def __init__(self, gateway: GatewayRuntime, session_id: str | None) -> None:
        self.gateway = gateway
        self.session_id = session_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self.gateway, name)

    @property
    def telemetry(self) -> Any:
        return self.gateway.telemetry

    def _selected_project_id(self) -> str | None:
        self.gateway.refresh_projects()
        if self.session_id is None:
            return self.gateway.projects.default_project_id
        context = self.gateway.sessions.get(self.session_id)
        if context is None:
            return None
        project_id = context.selected_project_id
        if project_id is not None and self.gateway.projects.get(project_id) is None:
            self.gateway.sessions.select(self.session_id, None)
            return None
        return project_id

    def _selected_runtime(self) -> Any | None:
        project_id = self._selected_project_id()
        return self.gateway.projects.runtime_for(project_id) if project_id else None

    def initialize(
        self,
        client_info: dict[str, Any] | None = None,
        protocol_version: str | None = None,
    ) -> dict[str, Any]:
        result = self.gateway.control_runtime.initialize(client_info, protocol_version)
        result = dict(result)
        result["instructions"] = self.gateway.tool_usage_instructions()
        return result

    def initialize_result(self, protocol_version: str) -> dict[str, Any]:
        result = dict(self.gateway.control_runtime.initialize_result(protocol_version))
        result["instructions"] = self.gateway.tool_usage_instructions()
        return result

    def discover_payload(self) -> dict[str, Any]:
        return self.gateway.discover_payload()

    def list_tools(self) -> dict[str, Any]:
        return self.gateway.list_tools()

    def exposed_tool_names(self) -> list[str]:
        return self.gateway.exposed_tool_names()

    def server_identity(self) -> dict[str, Any]:
        return self.gateway.control_runtime.server_identity()

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        if name == "project_context":
            payload = self._project_context(arguments or {})
            return make_tool_result(name, payload, is_error=payload.get("ok") is False)
        catalog = self.gateway.local_capabilities
        if catalog and name in {"local_capabilities_search", "local_skill_read", "local_plugin_inspect"}:
            operation = {
                "local_capabilities_search": catalog.search,
                "local_skill_read": catalog.read_skill,
                "local_plugin_inspect": catalog.inspect_plugin,
            }[name]
            payload = operation(arguments or {})
            return make_tool_result(name, payload, is_error=payload.get("ok") is False)
        runtime = self._selected_runtime()
        if runtime is None:
            payload = self._project_error(
                "PROJECT_NOT_SELECTED",
                "No project is bound to this MCP session. Use project_context with action=select and project=<name-or-path> first.",
            )
            return make_tool_result(name, payload, is_error=True)
        return runtime.call_tool(name, arguments, context=context)

    def _require_runtime(self) -> Any:
        runtime = self._selected_runtime()
        if runtime is None:
            raise RuntimeError("No project is bound to this MCP session")
        return runtime

    def _project_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.gateway.refresh_projects()
        action = str(arguments.get("action") or "current")
        if action not in {"list", "current", "select"}:
            return self._project_error("INVALID_ARGUMENT", f"Unknown project_context action: {action}")
        current_id = self._selected_project_id()
        if action == "select":
            if self.session_id is None:
                return self._project_error(
                    "SESSION_REQUIRED",
                    "This client has no MCP session id, so persistent project switching is disabled.",
                )
            selector = str(arguments.get("project") or "").strip()
            if not selector:
                return self._project_error("INVALID_ARGUMENT", "project is required for action=select")
            definition, matches = self.gateway.projects.resolve_selector(selector)
            if definition is None:
                if matches:
                    return self._project_error(
                        "PROJECT_AMBIGUOUS",
                        f"Project selector is ambiguous: {selector}",
                        project=selector,
                        matches=[
                            {"project_id": item.id, "name": item.name, "root": str(item.path)}
                            for item in matches
                        ],
                    )
                return self._project_error(
                    "PROJECT_NOT_FOUND",
                    f"Project is not registered: {selector}",
                    project=selector,
                )
            project_id = definition.id
            context = self.gateway.sessions.select(self.session_id, project_id)
            if context is None:
                return self._project_error(
                    "SESSION_NOT_FOUND",
                    "The MCP session expired or is no longer registered. Reinitialize the connection.",
                )
            current_id = project_id

        projects = [
            definition.payload(runtime_state=self.gateway.projects.runtime_state(definition.id))
            for definition in self.gateway.projects.definitions()
        ]
        current = next((item for item in projects if item["project_id"] == current_id), None)
        payload: dict[str, Any] = {
            "ok": True,
            "action": action,
            "session_id": self.session_id,
            "project_id": current_id,
            "current": current,
            "default_project_id": self.gateway.projects.default_project_id,
            "registry_generation": self.gateway.projects.generation,
        }
        if action == "list":
            payload["projects"] = projects
            payload["count"] = len(projects)
        return payload

    def _project_error(self, code: str, message: str, **details: Any) -> dict[str, Any]:
        available = [
            {"project_id": definition.id, "name": definition.name, "root": str(definition.path)}
            for definition in self.gateway.projects.definitions()
        ]
        return {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "category": "validation",
                "retryable": code in {"PROJECT_NOT_SELECTED", "SESSION_NOT_FOUND"},
                "details": {**details, "available_projects": available},
            },
        }
