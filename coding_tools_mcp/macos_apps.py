from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import ToolFailure


K_CFSTRING_ENCODING_UTF8 = 0x08000100
K_CG_HID_EVENT_TAP = 0
K_CG_EVENT_LEFT_MOUSE_DOWN = 1
K_CG_EVENT_LEFT_MOUSE_UP = 2
K_CG_EVENT_FLAG_MASK_SHIFT = 1 << 17
K_CG_EVENT_FLAG_MASK_CONTROL = 1 << 18
K_CG_EVENT_FLAG_MASK_ALTERNATE = 1 << 19
K_CG_EVENT_FLAG_MASK_COMMAND = 1 << 20
K_CG_EVENT_FLAG_MASK_SECONDARY_FN = 1 << 23


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _unsupported() -> ToolFailure:
    return ToolFailure(
        "UNSUPPORTED_PLATFORM",
        "macOS application automation is only available on macOS.",
        category="runtime",
    )


class AXBridge:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise _unsupported()
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self._configure()

    def _configure(self) -> None:
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cf.CFRetain.argtypes = [ctypes.c_void_p]
        self.cf.CFRetain.restype = ctypes.c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        self.cf.CFGetTypeID.restype = ctypes.c_ulong
        self.cf.CFStringGetTypeID.restype = ctypes.c_ulong
        self.cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        self.cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        self.cf.CFBooleanGetValue.restype = ctypes.c_bool
        self.cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        self.cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.cf.CFNumberGetValue.restype = ctypes.c_bool
        self.cf.CFArrayGetTypeID.restype = ctypes.c_ulong
        self.cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self.cf.CFArrayGetCount.restype = ctypes.c_long
        self.cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self.cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p

        self.ax.AXIsProcessTrusted.restype = ctypes.c_bool
        self.ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        self.ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
        self.ax.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int
        self.ax.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.ax.AXUIElementPerformAction.restype = ctypes.c_int
        self.ax.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.ax.AXUIElementSetAttributeValue.restype = ctypes.c_int
        self.ax.AXValueGetTypeID.restype = ctypes.c_ulong
        self.ax.AXValueGetType.argtypes = [ctypes.c_void_p]
        self.ax.AXValueGetType.restype = ctypes.c_int
        self.ax.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.ax.AXValueGetValue.restype = ctypes.c_bool

        self.ax.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_ushort, ctypes.c_bool]
        self.ax.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        self.ax.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
        self.ax.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong]
        self.ax.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self.ax.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
        self.ax.CGEventCreateMouseEvent.restype = ctypes.c_void_p

    def trusted(self) -> bool:
        return bool(self.ax.AXIsProcessTrusted())

    def cfstr(self, value: str) -> ctypes.c_void_p:
        ref = self.cf.CFStringCreateWithCString(None, value.encode("utf-8"), K_CFSTRING_ENCODING_UTF8)
        if not ref:
            raise RuntimeError("Could not allocate CFString")
        return ctypes.c_void_p(ref)

    def string_value(self, ref: ctypes.c_void_p) -> str | None:
        if not ref or self.cf.CFGetTypeID(ref) != self.cf.CFStringGetTypeID():
            return None
        size = 65536
        buffer = ctypes.create_string_buffer(size)
        if not self.cf.CFStringGetCString(ref, buffer, size, K_CFSTRING_ENCODING_UTF8):
            return None
        return buffer.value.decode("utf-8", errors="replace")

    def python_value(self, ref: ctypes.c_void_p) -> Any:
        if not ref:
            return None
        type_id = self.cf.CFGetTypeID(ref)
        if type_id == self.cf.CFStringGetTypeID():
            return self.string_value(ref)
        if type_id == self.cf.CFBooleanGetTypeID():
            return bool(self.cf.CFBooleanGetValue(ref))
        if type_id == self.cf.CFNumberGetTypeID():
            number = ctypes.c_double()
            if self.cf.CFNumberGetValue(ref, 13, ctypes.byref(number)):
                return number.value
        if type_id == self.ax.AXValueGetTypeID():
            value_type = self.ax.AXValueGetType(ref)
            if value_type == 1:
                point = CGPoint()
                if self.ax.AXValueGetValue(ref, value_type, ctypes.byref(point)):
                    return {"x": point.x, "y": point.y}
            if value_type == 2:
                size = CGSize()
                if self.ax.AXValueGetValue(ref, value_type, ctypes.byref(size)):
                    return {"width": size.width, "height": size.height}
            if value_type == 3:
                rect = CGRect()
                if self.ax.AXValueGetValue(ref, value_type, ctypes.byref(rect)):
                    return {
                        "x": rect.origin.x,
                        "y": rect.origin.y,
                        "width": rect.size.width,
                        "height": rect.size.height,
                    }
        return None

    def attribute_ref(self, element: ctypes.c_void_p, name: str) -> ctypes.c_void_p | None:
        attr = self.cfstr(name)
        output = ctypes.c_void_p()
        try:
            error = self.ax.AXUIElementCopyAttributeValue(element, attr, ctypes.byref(output))
        finally:
            self.cf.CFRelease(attr)
        if error != 0 or not output.value:
            return None
        return output

    def attribute(self, element: ctypes.c_void_p, name: str) -> Any:
        ref = self.attribute_ref(element, name)
        if ref is None:
            return None
        try:
            return self.python_value(ref)
        finally:
            self.cf.CFRelease(ref)

    def element_array(self, element: ctypes.c_void_p, name: str) -> list[ctypes.c_void_p]:
        ref = self.attribute_ref(element, name)
        if ref is None:
            return []
        try:
            if self.cf.CFGetTypeID(ref) != self.cf.CFArrayGetTypeID():
                return []
            count = self.cf.CFArrayGetCount(ref)
            values: list[ctypes.c_void_p] = []
            for index in range(count):
                child = ctypes.c_void_p(self.cf.CFArrayGetValueAtIndex(ref, index))
                if child.value:
                    self.cf.CFRetain(child)
                    values.append(child)
            return values
        finally:
            self.cf.CFRelease(ref)

    def app(self, pid: int) -> ctypes.c_void_p:
        ref = self.ax.AXUIElementCreateApplication(pid)
        if not ref:
            raise ToolFailure("APP_CONTROL_ERROR", f"Could not create AX application for pid {pid}.", category="runtime")
        return ctypes.c_void_p(ref)

    def perform(self, element: ctypes.c_void_p, action: str) -> None:
        action_ref = self.cfstr(action)
        try:
            error = self.ax.AXUIElementPerformAction(element, action_ref)
        finally:
            self.cf.CFRelease(action_ref)
        if error != 0:
            raise ToolFailure("APP_CONTROL_ERROR", f"Accessibility action {action} failed with AXError {error}.", category="runtime")

    def set_string(self, element: ctypes.c_void_p, attr_name: str, value: str) -> None:
        attr = self.cfstr(attr_name)
        text = self.cfstr(value)
        try:
            error = self.ax.AXUIElementSetAttributeValue(element, attr, text)
        finally:
            self.cf.CFRelease(text)
            self.cf.CFRelease(attr)
        if error != 0:
            raise ToolFailure("APP_CONTROL_ERROR", f"Setting {attr_name} failed with AXError {error}.", category="runtime")

    def click_point(self, x: float, y: float) -> None:
        point = CGPoint(x, y)
        down = self.ax.CGEventCreateMouseEvent(None, K_CG_EVENT_LEFT_MOUSE_DOWN, point, 0)
        up = self.ax.CGEventCreateMouseEvent(None, K_CG_EVENT_LEFT_MOUSE_UP, point, 0)
        if not down or not up:
            raise ToolFailure("APP_CONTROL_ERROR", "Could not create mouse events.", category="runtime")
        try:
            self.ax.CGEventPost(K_CG_HID_EVENT_TAP, down)
            self.ax.CGEventPost(K_CG_HID_EVENT_TAP, up)
        finally:
            self.cf.CFRelease(down)
            self.cf.CFRelease(up)

    def key(self, key_code: int, modifiers: list[str]) -> None:
        flags = _modifier_flags(modifiers)
        down = self.ax.CGEventCreateKeyboardEvent(None, key_code, True)
        up = self.ax.CGEventCreateKeyboardEvent(None, key_code, False)
        if not down or not up:
            raise ToolFailure("APP_CONTROL_ERROR", "Could not create keyboard events.", category="runtime")
        try:
            if flags:
                self.ax.CGEventSetFlags(down, flags)
                self.ax.CGEventSetFlags(up, flags)
            self.ax.CGEventPost(K_CG_HID_EVENT_TAP, down)
            self.ax.CGEventPost(K_CG_HID_EVENT_TAP, up)
        finally:
            self.cf.CFRelease(down)
            self.cf.CFRelease(up)

    def type_unicode(self, text: str) -> None:
        data = text.encode("utf-16-le")
        units = (ctypes.c_uint16 * (len(data) // 2)).from_buffer_copy(data)
        down = self.ax.CGEventCreateKeyboardEvent(None, 0, True)
        up = self.ax.CGEventCreateKeyboardEvent(None, 0, False)
        if not down or not up:
            raise ToolFailure("APP_CONTROL_ERROR", "Could not create text keyboard events.", category="runtime")
        try:
            self.ax.CGEventKeyboardSetUnicodeString(down, len(units), units)
            self.ax.CGEventKeyboardSetUnicodeString(up, len(units), units)
            self.ax.CGEventPost(K_CG_HID_EVENT_TAP, down)
            self.ax.CGEventPost(K_CG_HID_EVENT_TAP, up)
        finally:
            self.cf.CFRelease(down)
            self.cf.CFRelease(up)


_AX: AXBridge | None = None


def _ax() -> AXBridge:
    global _AX
    if _AX is None:
        _AX = AXBridge()
    return _AX


def _require_accessibility() -> AXBridge:
    bridge = _ax()
    if not bridge.trusted():
        raise ToolFailure(
            "ACCESSIBILITY_PERMISSION_REQUIRED",
            "Coding Tools MCP does not have macOS Accessibility permission.",
            category="permission",
            retryable=True,
            details={
                "settings": "System Settings > Privacy & Security > Accessibility",
                "hint": "Enable Coding Tools MCP (or coding-tools-mcp-runtime), then retry.",
            },
        )
    return bridge


def _running_apps() -> list[dict[str, Any]]:
    if sys.platform != "darwin":
        raise _unsupported()
    script = r'''const se=Application("System Events"); JSON.stringify(se.applicationProcesses().map(p=>{try{return {name:p.name(),bundle_id:p.bundleIdentifier(),pid:p.unixId(),background_only:p.backgroundOnly(),frontmost:p.frontmost()}}catch(e){return null}}).filter(Boolean))'''
    completed = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise ToolFailure("APP_CONTROL_ERROR", completed.stderr.strip() or "Could not enumerate applications.", category="runtime")
    value = json.loads(completed.stdout or "[]")
    if not isinstance(value, list):
        return []
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        key = (int(item.get("pid") or 0), str(item.get("bundle_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _resolve_app(ref: str) -> dict[str, Any]:
    needle = ref.casefold()
    for app in _running_apps():
        if str(app.get("name") or "").casefold() == needle or str(app.get("bundle_id") or "").casefold() == needle:
            return app
    raise ToolFailure("NOT_FOUND", f"Application {ref!r} is not running.", category="not_found")


def _element_summary(bridge: AXBridge, element: ctypes.c_void_p) -> dict[str, Any]:
    return {
        "role": bridge.attribute(element, "AXRole"),
        "subrole": bridge.attribute(element, "AXSubrole"),
        "title": bridge.attribute(element, "AXTitle"),
        "identifier": bridge.attribute(element, "AXIdentifier"),
        "description": bridge.attribute(element, "AXDescription"),
        "value": bridge.attribute(element, "AXValue"),
        "enabled": bridge.attribute(element, "AXEnabled"),
        "focused": bridge.attribute(element, "AXFocused"),
        "position": bridge.attribute(element, "AXPosition"),
        "size": bridge.attribute(element, "AXSize"),
    }


def _walk(bridge: AXBridge, root: ctypes.c_void_p, max_depth: int) -> Iterator[tuple[ctypes.c_void_p, int, bool]]:
    stack: list[tuple[ctypes.c_void_p, int, bool]] = [(root, 0, False)]
    seen: set[int] = set()
    try:
        while stack:
            element, depth, owned = stack.pop()
            pointer = int(element.value or 0)
            if not pointer or pointer in seen:
                if owned:
                    bridge.cf.CFRelease(element)
                continue
            seen.add(pointer)
            if depth < max_depth:
                children = bridge.element_array(element, "AXChildren")
                for child in reversed(children):
                    stack.append((child, depth + 1, True))
            yield element, depth, owned
    finally:
        for element, _depth, owned in stack:
            if owned:
                bridge.cf.CFRelease(element)


def _find_element(
    bridge: AXBridge,
    root: ctypes.c_void_p,
    *,
    role: str | None,
    title: str | None,
    identifier: str | None,
    index: int,
    max_depth: int = 12,
) -> tuple[ctypes.c_void_p, dict[str, Any]]:
    matched = 0
    for element, _depth, owned in _walk(bridge, root, max_depth):
        try:
            summary = _element_summary(bridge, element)
            if role and str(summary.get("role") or "").casefold() != role.casefold():
                continue
            if title and str(summary.get("title") or summary.get("description") or summary.get("value") or "").casefold() != title.casefold():
                continue
            if identifier and str(summary.get("identifier") or "").casefold() != identifier.casefold():
                continue
            if matched == index:
                retained = ctypes.c_void_p(bridge.cf.CFRetain(element))
                return retained, summary
            matched += 1
        finally:
            if owned:
                bridge.cf.CFRelease(element)
    raise ToolFailure(
        "NOT_FOUND",
        "No accessibility element matched the requested selector.",
        category="not_found",
        details={"role": role, "title": title, "identifier": identifier, "index": index},
    )


def _app_root(app_ref: str) -> tuple[AXBridge, dict[str, Any], ctypes.c_void_p]:
    bridge = _require_accessibility()
    app = _resolve_app(app_ref)
    return bridge, app, bridge.app(int(app["pid"]))


def accessibility(args: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise _unsupported()
    trusted = _ax().trusted()
    if bool(args.get("open_settings", False)) and not trusted:
        subprocess.run(
            ["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            check=False,
        )
    return {
        "ok": True,
        "trusted": trusted,
        "settings": "System Settings > Privacy & Security > Accessibility",
    }


def list_apps(args: dict[str, Any]) -> dict[str, Any]:
    apps = _running_apps()
    if not bool(args.get("include_background", False)):
        apps = [app for app in apps if not app.get("background_only")]
    query = str(args.get("query") or "").casefold()
    if query:
        apps = [app for app in apps if query in str(app.get("name") or "").casefold() or query in str(app.get("bundle_id") or "").casefold()]
    limit = int(args.get("max_results", 200))
    return {
        "ok": True,
        "accessibility_trusted": _ax().trusted(),
        "apps": apps[:limit],
        "count": min(len(apps), limit),
        "truncated": len(apps) > limit,
    }


def launch(args: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise _unsupported()
    ref = str(args["app"])
    cmd = ["/usr/bin/open", "-b" if "." in ref and " " not in ref else "-a", ref]
    if bool(args.get("new_instance", False)):
        cmd.insert(1, "-n")
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    if completed.returncode != 0:
        raise ToolFailure("APP_CONTROL_ERROR", completed.stderr.strip() or f"Could not launch {ref}.", category="runtime")
    return {"ok": True, "app": ref}


def activate(args: dict[str, Any]) -> dict[str, Any]:
    result = launch({"app": args["app"], "new_instance": False})
    time.sleep(min(int(args.get("wait_ms", 150)), 2000) / 1000)
    app = _resolve_app(str(args["app"]))
    return {**result, "resolved": app}


def windows(args: dict[str, Any]) -> dict[str, Any]:
    bridge, app, root = _app_root(str(args["app"]))
    try:
        items = []
        windows_list = bridge.element_array(root, "AXWindows")
        try:
            for index, window in enumerate(windows_list):
                item = _element_summary(bridge, window)
                item["index"] = index
                items.append(item)
        finally:
            for window in windows_list:
                bridge.cf.CFRelease(window)
        return {"ok": True, "app": app, "windows": items, "count": len(items)}
    finally:
        bridge.cf.CFRelease(root)


def snapshot(args: dict[str, Any]) -> dict[str, Any]:
    bridge, app, root = _app_root(str(args["app"]))
    max_depth = int(args.get("max_depth", 6))
    max_elements = int(args.get("max_elements", 500))
    try:
        elements: list[dict[str, Any]] = []
        for element, depth, owned in _walk(bridge, root, max_depth):
            try:
                summary = _element_summary(bridge, element)
                summary["depth"] = depth
                elements.append(summary)
                if len(elements) >= max_elements:
                    break
            finally:
                if owned:
                    bridge.cf.CFRelease(element)
        return {
            "ok": True,
            "app": app,
            "elements": elements,
            "count": len(elements),
            "truncated": len(elements) >= max_elements,
        }
    finally:
        bridge.cf.CFRelease(root)


def click(args: dict[str, Any]) -> dict[str, Any]:
    if not (args.get("role") or args.get("title") or args.get("identifier")):
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "app_click requires at least one of role, title, or identifier.",
            category="validation",
        )
    bridge, app, root = _app_root(str(args["app"]))
    element: ctypes.c_void_p | None = None
    try:
        element, summary = _find_element(
            bridge,
            root,
            role=args.get("role"),
            title=args.get("title"),
            identifier=args.get("identifier"),
            index=int(args.get("index", 0)),
        )
        try:
            bridge.perform(element, "AXPress")
            method = "AXPress"
        except ToolFailure:
            position = summary.get("position") or {}
            size = summary.get("size") or {}
            if not position or not size:
                raise
            bridge.click_point(
                float(position.get("x", 0)) + float(size.get("width", 0)) / 2,
                float(position.get("y", 0)) + float(size.get("height", 0)) / 2,
            )
            method = "mouse"
        return {"ok": True, "app": app, "element": summary, "method": method}
    finally:
        if element is not None:
            bridge.cf.CFRelease(element)
        bridge.cf.CFRelease(root)


def type_text(args: dict[str, Any]) -> dict[str, Any]:
    bridge, app, root = _app_root(str(args["app"]))
    text = str(args["text"])
    element: ctypes.c_void_p | None = None
    try:
        if args.get("role") or args.get("title") or args.get("identifier"):
            element, summary = _find_element(
                bridge,
                root,
                role=args.get("role"),
                title=args.get("title"),
                identifier=args.get("identifier"),
                index=int(args.get("index", 0)),
            )
            if bool(args.get("clear", True)):
                try:
                    bridge.set_string(element, "AXValue", text)
                    return {"ok": True, "app": app, "element": summary, "characters": len(text), "method": "AXValue"}
                except ToolFailure:
                    pass
            try:
                bridge.perform(element, "AXPress")
            except ToolFailure:
                position = summary.get("position") or {}
                size = summary.get("size") or {}
                if position and size:
                    bridge.click_point(
                        float(position.get("x", 0)) + float(size.get("width", 0)) / 2,
                        float(position.get("y", 0)) + float(size.get("height", 0)) / 2,
                    )
        if bool(args.get("clear", True)):
            bridge.key(0, ["command"])
            bridge.key(51, [])
        bridge.type_unicode(text)
        return {"ok": True, "app": app, "characters": len(text), "method": "keyboard"}
    finally:
        if element is not None:
            bridge.cf.CFRelease(element)
        bridge.cf.CFRelease(root)


KEY_CODES = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "-": 27,
    "8": 28,
    "0": 29,
    "]": 30,
    "o": 31,
    "u": 32,
    "[": 33,
    "i": 34,
    "p": 35,
    "enter": 36,
    "return": 36,
    "l": 37,
    "j": 38,
    "'": 39,
    "k": 40,
    ";": 41,
    "\\": 42,
    ",": 43,
    "/": 44,
    "n": 45,
    "m": 46,
    ".": 47,
    "tab": 48,
    "space": 49,
    "backspace": 51,
    "delete": 51,
    "escape": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
}


def _modifier_flags(modifiers: list[str]) -> int:
    flags = 0
    for modifier in modifiers:
        name = modifier.casefold()
        if name in {"cmd", "command", "meta"}:
            flags |= K_CG_EVENT_FLAG_MASK_COMMAND
        elif name in {"shift"}:
            flags |= K_CG_EVENT_FLAG_MASK_SHIFT
        elif name in {"ctrl", "control"}:
            flags |= K_CG_EVENT_FLAG_MASK_CONTROL
        elif name in {"alt", "option"}:
            flags |= K_CG_EVENT_FLAG_MASK_ALTERNATE
        elif name in {"fn", "function"}:
            flags |= K_CG_EVENT_FLAG_MASK_SECONDARY_FN
        else:
            raise ToolFailure("INVALID_ARGUMENT", f"Unknown modifier {modifier!r}.", category="validation")
    return flags


def press(args: dict[str, Any]) -> dict[str, Any]:
    bridge = _require_accessibility()
    app = activate({"app": args["app"], "wait_ms": args.get("wait_ms", 100)})["resolved"]
    key = str(args["key"]).casefold()
    if key not in KEY_CODES:
        raise ToolFailure("INVALID_ARGUMENT", f"Unsupported key {key!r}.", category="validation")
    modifiers = [str(value) for value in args.get("modifiers", [])]
    bridge.key(KEY_CODES[key], modifiers)
    return {"ok": True, "app": app, "key": key, "modifiers": modifiers}


def menu(args: dict[str, Any]) -> dict[str, Any]:
    bridge, app, root = _app_root(str(args["app"]))
    path = [str(item) for item in args["path"]]
    if not path:
        raise ToolFailure("INVALID_ARGUMENT", "Menu path cannot be empty.", category="validation")
    try:
        for depth, title in enumerate(path):
            role = "AXMenuBarItem" if depth == 0 else "AXMenuItem"
            element, _summary = _find_element(
                bridge,
                root,
                role=role,
                title=title,
                identifier=None,
                index=0,
                max_depth=4,
            )
            try:
                bridge.perform(element, "AXPress")
            finally:
                bridge.cf.CFRelease(element)
            if depth < len(path) - 1:
                time.sleep(0.08)
        return {"ok": True, "app": app, "path": path}
    finally:
        bridge.cf.CFRelease(root)


def screenshot(args: dict[str, Any]) -> dict[str, Any]:
    bridge, app, root = _app_root(str(args["app"]))
    window_index = int(args.get("window_index", 0))
    windows_list: list[ctypes.c_void_p] = []
    try:
        windows_list = bridge.element_array(root, "AXWindows")
        if window_index < 0 or window_index >= len(windows_list):
            raise ToolFailure("INVALID_ARGUMENT", f"window_index {window_index} is out of range.", category="validation")
        window = windows_list[window_index]
        position = bridge.attribute(window, "AXPosition") or {}
        size = bridge.attribute(window, "AXSize") or {}
        x, y = int(position.get("x", 0)), int(position.get("y", 0))
        width, height = int(size.get("width", 0)), int(size.get("height", 0))
        if width <= 0 or height <= 0:
            raise ToolFailure("APP_CONTROL_ERROR", "Window has no capturable bounds.", category="runtime")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            output = Path(handle.name)
        try:
            completed = subprocess.run(
                ["/usr/sbin/screencapture", "-x", "-R", f"{x},{y},{width},{height}", "-t", "png", str(output)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if completed.returncode != 0 or not output.is_file():
                raise ToolFailure(
                    "SCREEN_RECORDING_PERMISSION_REQUIRED",
                    completed.stderr.strip() or "Window screenshot failed. Check Screen Recording permission.",
                    category="permission",
                    details={"settings": "System Settings > Privacy & Security > Screen Recording"},
                )
            data = output.read_bytes()
        finally:
            output.unlink(missing_ok=True)
        return {
            "ok": True,
            "app": app,
            "window_index": window_index,
            "bounds": {"x": x, "y": y, "width": width, "height": height},
            "mime_type": "image/png",
            "bytes": len(data),
            "_mcp_image_data": base64.b64encode(data).decode("ascii"),
        }
    finally:
        for window in windows_list:
            bridge.cf.CFRelease(window)
        bridge.cf.CFRelease(root)
