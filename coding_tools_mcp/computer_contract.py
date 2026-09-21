"""Authoritative, opt-in computer tool surface; no platform dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


HELPER_PROTOCOL_VERSION = 1
NATIVE_OPERATIONS = frozenset({"status", "list_apps", "resolve_app", "windows", "snapshot", "action", "inspect"})


def obj(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def text(max_length: int = 200) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def integer(default: int, minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "default": default, "minimum": minimum, "maximum": maximum}


@dataclass(frozen=True)
class ComputerTool:
    title: str
    description: str
    schema: dict[str, Any]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    image: bool = False


SESSION = {"session_id": text()}
WINDOW = {**SESSION, "window_id": text()}
ELEMENT = {**WINDOW, "snapshot_id": text(), "element_id": text()}
PROVIDER = {"provider": {"type": "string", "enum": ["native", "codex"], "default": "native"}}

COMPUTER_TOOLS: dict[str, ComputerTool] = {
    "computer_status": ComputerTool(
        "Computer status", "Check native helper availability, system permissions and supported background operations without prompting.",
        obj({}), read_only=True, idempotent=True,
    ),
    "computer_request_access": ComputerTool(
        "Request app access", "Ask the desktop user to authorize one app session. Use an app_id from app_list. Never grants access automatically, including in host mode.",
        obj({**PROVIDER, "app_id": text(500), "access": {"type": "string", "enum": ["observe", "control"]},
             "reason": text(1000), "ttl_seconds": integer(600, 30, 1800)}, ("app_id", "access", "reason")),
    ),
    "computer_session_start": ComputerTool(
        "Start app session", "Consume an approved app access request and start its bounded session. Reuse the returned session_id for app tools.",
        obj({"approval_id": text()}, ("approval_id",)),
    ),
    "computer_session_get": ComputerTool(
        "Get app session", "Read an app approval, session or operation receipt. Pass approval_id to check a pending request, session_id for a session, or no arguments to list this runtime's sessions.",
        obj({**SESSION, "approval_id": text(), "operation_id": text()}), read_only=True, idempotent=True,
    ),
    "computer_session_stop": ComputerTool(
        "Stop app session", "Revoke an app session and release its control lock. Already completed actions are not undone.",
        obj(SESSION, ("session_id",)), idempotent=True,
    ),
    "app_list": ComputerTool(
        "List apps", "Find app identities before requesting access. provider=native is the stable default; provider=codex is an optional host-mode Preview provider. Does not expose window contents.",
        obj({**PROVIDER, "query": {"type": "string", "maxLength": 200}, "max_results": integer(30, 1, 100)}), read_only=True, idempotent=True,
    ),
    "app_windows": ComputerTool(
        "List app windows", "List windows belonging to an authorized session. Use returned window_id values, never guessed window indices.",
        obj(SESSION, ("session_id",)), read_only=True, idempotent=True,
    ),
    "app_snapshot": ComputerTool(
        "Observe app window", "Read an authorized window's accessibility elements and optional screenshot. Use returned snapshot_id and element_id for actions. Set include_image=false for accessibility-only inspection.",
        obj({**WINDOW, "include_image": {"type": "boolean", "default": True},
             "max_elements": integer(200, 1, 500), "max_depth": integer(8, 1, 20),
             "max_dimension": integer(1600, 320, 2400)}, ("session_id", "window_id")),
        read_only=True, image=True,
    ),
    "app_observe": ComputerTool(
        "Observe app", "Observe a whole application through a provider that supports app-level state. In v2 Preview this is the read-only observation path of the Codex provider; the call never performs a click, keypress, drag, or text input.",
        obj({**SESSION, "include_image": {"type": "boolean", "default": True},
             "disable_diff": {"type": "boolean", "default": True},
             "max_text_chars": integer(50000, 1, 256000)}, ("session_id",)),
        read_only=True, image=True,
    ),
    "app_interact": ComputerTool(
        "Interact with app", "Perform one Computer v2 action in an explicitly approved Codex control session. Requires a fresh app_observe snapshot_id and unique operation_id; stale state fails closed and an uncertain action is never retried automatically.",
        obj({
            **SESSION,
            "snapshot_id": text(),
            "action": {"type": "string", "enum": ["click", "perform_secondary_action", "set_value", "select_text", "scroll", "drag", "press_key", "type_text"]},
            "operation_id": text(),
            "element_index": {"type": "string", "maxLength": 200},
            "x": {"type": "number"}, "y": {"type": "number"},
            "click_count": integer(1, 1, 3),
            "mouse_button": {"type": "string", "enum": ["left", "right", "middle"]},
            "secondary_action": {"type": "string", "maxLength": 200},
            "value": {"type": "string", "maxLength": 10000},
            "text": {"type": "string", "maxLength": 10000},
            "prefix": {"type": "string", "maxLength": 2000},
            "suffix": {"type": "string", "maxLength": 2000},
            "selection": {"type": "string", "enum": ["text", "cursor_before", "cursor_after"]},
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "pages": {"type": "number", "minimum": 0.05, "maximum": 20},
            "from_x": {"type": "number"}, "from_y": {"type": "number"},
            "to_x": {"type": "number"}, "to_y": {"type": "number"},
            "key": {"type": "string", "maxLength": 200},
        }, ("session_id", "snapshot_id", "action", "operation_id")),
        destructive=True,
    ),
    "app_action": ComputerTool(
        "Act on app element", "Press or set the value of an observed accessible element in a control session. Requires a fresh snapshot and unique operation_id. No foreground keyboard/mouse fallback. Verify the outcome afterwards.",
        obj({**ELEMENT, "action": {"type": "string", "enum": ["press", "set_value"]},
             "value": {"type": "string", "maxLength": 10000}, "operation_id": text()},
            ("session_id", "window_id", "snapshot_id", "element_id", "action", "operation_id")), destructive=True,
    ),
    "app_wait": ComputerTool(
        "Wait for app element", "Wait for an observed element to become enabled or have an expected value. Returns bounded evidence or timeout and responds to session revocation.",
        obj({**ELEMENT, "condition": {"type": "string", "enum": ["enabled", "value_equals"]},
             "value": {"type": "string", "maxLength": 10000}, "timeout_ms": integer(3000, 0, 10000)},
            ("session_id", "window_id", "snapshot_id", "element_id", "condition")), read_only=True,
    ),
}
