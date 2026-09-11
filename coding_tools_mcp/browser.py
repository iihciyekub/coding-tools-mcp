from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .errors import ToolFailure


DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_TIMEOUT_MS = 5000
DEFAULT_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024


def _endpoint(args: dict[str, Any]) -> str:
    value = str(
        args.get("endpoint")
        or os.environ.get("CODING_TOOLS_MCP_BROWSER_CDP_URL")
        or DEFAULT_CDP_URL
    ).strip()
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError as exc:
        raise ToolFailure("INVALID_ARGUMENT", "Invalid browser CDP endpoint.", category="validation") from exc
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "Browser CDP endpoint must be an http(s) or ws(s) URL.",
            category="validation",
        )
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "Browser CDP endpoint must use localhost/loopback.",
            category="security",
        )
    return value


def _timeout(args: dict[str, Any]) -> int:
    return int(args.get("timeout_ms", DEFAULT_TIMEOUT_MS))


@contextmanager
def _browser_connection(args: dict[str, Any]) -> Iterator[Any]:
    endpoint = _endpoint(args)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ToolFailure(
            "BROWSER_ERROR",
            "Playwright is not installed. Reinstall coding-tools-mcp with its declared dependencies.",
            category="runtime",
        ) from exc

    playwright = sync_playwright().start()
    try:
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=_timeout(args), no_defaults=True)
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure(
                "BROWSER_ERROR",
                f"Could not connect to Chrome at {endpoint}: {exc}",
                category="runtime",
                retryable=True,
                details={
                    "endpoint": endpoint,
                    "hint": "Start Chrome with --remote-debugging-port=9222 and --remote-debugging-address=127.0.0.1.",
                },
            ) from exc
        yield browser
    finally:
        # Do not call browser.close(): with a CDP attachment that could close
        # the user's real Chrome. Stopping Playwright only drops our client.
        playwright.stop()


def _pages(browser: Any) -> list[Any]:
    return [page for context in browser.contexts for page in context.pages if not page.is_closed()]


def _target_id(page: Any) -> str | None:
    try:
        session = page.context.new_cdp_session(page)
    except Exception:  # noqa: BLE001
        return None
    try:
        response = session.send("Target.getTargetInfo")
        target_info = response.get("targetInfo") if isinstance(response, dict) else None
        target_id = target_info.get("targetId") if isinstance(target_info, dict) else None
        return str(target_id) if target_id else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            session.detach()
        except Exception:  # noqa: BLE001
            pass


def _tab_payload(page: Any, index: int) -> dict[str, Any]:
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = ""
    try:
        visibility = page.evaluate("document.visibilityState")
    except Exception:  # noqa: BLE001
        visibility = "unknown"
    return {
        "index": index,
        "tab_id": _target_id(page),
        "title": title,
        "url": page.url,
        "visibility": visibility,
    }


def _select_page(
    browser: Any,
    tab_index: int | None = None,
    tab_id: str | None = None,
) -> tuple[Any, int]:
    pages = _pages(browser)
    if not pages:
        raise ToolFailure("NOT_FOUND", "Chrome has no inspectable pages.", category="not_found")
    if tab_id:
        for index, page in enumerate(pages):
            if _target_id(page) != tab_id:
                continue
            if tab_index is not None and tab_index != index:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "tab_id and tab_index refer to different Chrome tabs.",
                    category="validation",
                )
            return page, index
        raise ToolFailure(
            "NOT_FOUND",
            f"Chrome tab_id {tab_id!r} is no longer inspectable.",
            category="not_found",
            retryable=True,
        )
    if tab_index is not None:
        if tab_index < 0 or tab_index >= len(pages):
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"tab_index {tab_index} is outside 0..{len(pages) - 1}.",
                category="validation",
            )
        return pages[tab_index], tab_index
    visible: list[tuple[Any, int]] = []
    for index, page in enumerate(pages):
        try:
            if page.evaluate("document.visibilityState") == "visible":
                visible.append((page, index))
        except Exception:  # noqa: BLE001
            continue
    return visible[-1] if visible else (pages[-1], len(pages) - 1)


@dataclass
class _BrowserWatch:
    watch_id: str
    args: dict[str, Any]
    max_entries: int
    dialog_action: str
    dialog_text: str
    events: deque[dict[str, Any]] = field(default_factory=deque)
    condition: threading.Condition = field(default_factory=threading.Condition)
    stop_event: threading.Event = field(default_factory=threading.Event)
    ready_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    status: str = "starting"
    next_seq: int = 1
    dropped_total: int = 0
    tab: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def append(self, event: dict[str, Any]) -> None:
        with self.condition:
            if len(self.events) >= self.max_entries:
                self.events.popleft()
                self.dropped_total += 1
            item = {"seq": self.next_seq, "timestamp": time.time(), **event}
            self.next_seq += 1
            self.events.append(item)
            self.condition.notify_all()


class BrowserWatchManager:
    """Hold bounded browser event watches for one Runtime lifetime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watches: dict[str, _BrowserWatch] = {}

    def _get(self, watch_id: str) -> _BrowserWatch:
        with self._lock:
            watch = self._watches.get(watch_id)
        if watch is None:
            raise ToolFailure(
                "BROWSER_WATCH_NOT_FOUND",
                f"Browser watch {watch_id!r} was not found in this runtime.",
                category="not_found",
            )
        return watch

    def _worker(self, watch: _BrowserWatch) -> None:
        try:
            with _browser_connection(watch.args) as browser:
                page, index = _select_page(browser, watch.args.get("tab_index"), watch.args.get("tab_id"))
                watch.tab = _tab_payload(page, index)

                def on_console(message: Any) -> None:
                    watch.append({"kind": "console", "type": message.type, "text": message.text})

                def on_page_error(error: Any) -> None:
                    watch.append({"kind": "pageerror", "text": str(error)})

                def on_request_failed(request: Any) -> None:
                    watch.append(
                        {
                            "kind": "requestfailed",
                            "url": request.url,
                            "method": request.method,
                            "resource_type": request.resource_type,
                            "failure": request.failure,
                        }
                    )

                def on_dialog(dialog: Any) -> None:
                    watch.append({"kind": "dialog", "type": dialog.type, "message": dialog.message})
                    if watch.dialog_action == "accept":
                        dialog.accept(watch.dialog_text)
                    else:
                        dialog.dismiss()

                def on_popup(popup: Any) -> None:
                    watch.append({"kind": "popup", "url": popup.url})

                page.on("console", on_console)
                page.on("pageerror", on_page_error)
                page.on("requestfailed", on_request_failed)
                page.on("dialog", on_dialog)
                page.on("popup", on_popup)
                with watch.condition:
                    watch.status = "running"
                    watch.condition.notify_all()
                watch.ready_event.set()

                while not watch.stop_event.is_set():
                    if page.is_closed():
                        watch.append({"kind": "watch", "type": "page_closed"})
                        break
                    page.wait_for_timeout(100)
                with watch.condition:
                    if watch.status == "running":
                        watch.status = "stopped"
                    watch.condition.notify_all()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, ToolFailure):
                error = {
                    "code": exc.code,
                    "message": exc.message,
                    "category": exc.category,
                    "retryable": exc.retryable,
                    "details": exc.details,
                }
            else:
                error = {
                    "code": "BROWSER_ERROR",
                    "message": str(exc),
                    "category": "runtime",
                    "retryable": True,
                    "details": {},
                }
            with watch.condition:
                watch.status = "failed"
                watch.error = error
                watch.condition.notify_all()
            watch.ready_event.set()
        finally:
            watch.ready_event.set()

    def start(self, args: dict[str, Any]) -> dict[str, Any]:
        max_entries = int(args.get("max_entries", 1000))
        if max_entries <= 0:
            raise ToolFailure("INVALID_ARGUMENT", "max_entries must be positive.", category="validation")
        watch_id = secrets.token_hex(12)
        watch = _BrowserWatch(
            watch_id=watch_id,
            args=dict(args),
            max_entries=max_entries,
            dialog_action=str(args.get("dialog_action", "dismiss")),
            dialog_text=str(args.get("dialog_text", "")),
            events=deque(),
        )
        thread = threading.Thread(
            target=self._worker,
            args=(watch,),
            name=f"browser-watch-{watch_id[:8]}",
            daemon=True,
        )
        watch.thread = thread
        with self._lock:
            self._watches[watch_id] = watch
        thread.start()
        ready_timeout = (_timeout(args) / 1000.0) + 1.0
        if not watch.ready_event.wait(ready_timeout):
            watch.stop_event.set()
            thread.join(timeout=1.0)
            with self._lock:
                self._watches.pop(watch_id, None)
            raise ToolFailure(
                "BROWSER_TIMEOUT",
                "Browser watch did not become ready before the timeout.",
                category="runtime",
                retryable=True,
            )
        with watch.condition:
            if watch.status == "failed":
                error = watch.error or {}
                with self._lock:
                    self._watches.pop(watch_id, None)
                raise ToolFailure(
                    str(error.get("code") or "BROWSER_ERROR"),
                    str(error.get("message") or "Browser watch failed to start."),
                    category=str(error.get("category") or "runtime"),
                    retryable=bool(error.get("retryable", True)),
                    details=error.get("details") if isinstance(error.get("details"), dict) else {},
                )
            return {
                "ok": True,
                "watch_id": watch_id,
                "status": watch.status,
                "tab": watch.tab,
                "max_entries": max_entries,
                "latest_seq": watch.next_seq - 1,
                "note": "Browser watches are process-local and end when this runtime closes or restarts.",
            }

    def poll(self, args: dict[str, Any]) -> dict[str, Any]:
        watch = self._get(str(args["watch_id"]))
        after_seq = int(args.get("after_seq", 0))
        max_entries = int(args.get("max_entries", 200))
        wait_ms = int(args.get("wait_ms", 0))
        if after_seq < 0 or max_entries <= 0 or wait_ms < 0:
            raise ToolFailure("INVALID_ARGUMENT", "Invalid browser watch polling bounds.", category="validation")
        deadline = time.monotonic() + (wait_ms / 1000.0)
        with watch.condition:
            while (
                wait_ms > 0
                and watch.status in {"starting", "running"}
                and not any(int(item["seq"]) > after_seq for item in watch.events)
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                watch.condition.wait(timeout=remaining)
            retained = [item for item in watch.events if int(item["seq"]) > after_seq]
            oldest_seq = int(watch.events[0]["seq"]) if watch.events else watch.next_seq
            dropped_since_cursor = max(0, oldest_seq - after_seq - 1)
            truncated = len(retained) > max_entries or dropped_since_cursor > 0
            selected = retained[:max_entries]
            next_after_seq = int(selected[-1]["seq"]) if selected else after_seq
            return {
                "ok": True,
                "watch_id": watch.watch_id,
                "status": watch.status,
                "tab": watch.tab,
                "events": selected,
                "count": len(selected),
                "after_seq": after_seq,
                "next_after_seq": next_after_seq,
                "latest_seq": watch.next_seq - 1,
                "dropped_since_cursor": dropped_since_cursor,
                "dropped_total": watch.dropped_total,
                "truncated": truncated,
                "error": watch.error,
            }

    def stop(self, args: dict[str, Any]) -> dict[str, Any]:
        watch = self._get(str(args["watch_id"]))
        watch.stop_event.set()
        thread = watch.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with watch.condition:
            if watch.status == "running" and (thread is None or not thread.is_alive()):
                watch.status = "stopped"
            return {
                "ok": True,
                "watch_id": watch.watch_id,
                "status": watch.status,
                "latest_seq": watch.next_seq - 1,
                "dropped_total": watch.dropped_total,
                "error": watch.error,
            }

    def close(self) -> None:
        with self._lock:
            watches = list(self._watches.values())
        for watch in watches:
            watch.stop_event.set()
        for watch in watches:
            if watch.thread is not None and watch.thread.is_alive():
                watch.thread.join(timeout=2.0)


def status(args: dict[str, Any]) -> dict[str, Any]:
    endpoint = _endpoint(args)
    with _browser_connection(args) as browser:
        pages = _pages(browser)
        return {
            "ok": True,
            "connected": True,
            "endpoint": endpoint,
            "browser_version": browser.version,
            "contexts": len(browser.contexts),
            "tabs": len(pages),
        }


def tabs(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        items = [_tab_payload(page, index) for index, page in enumerate(_pages(browser))]
        return {"ok": True, "tabs": items, "count": len(items)}


def active_tab(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser)
        return {"ok": True, "tab": _tab_payload(page, index)}


def snapshot(args: dict[str, Any]) -> dict[str, Any]:
    max_chars = int(args.get("max_chars", 50000))
    max_elements = int(args.get("max_elements", 150))
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        data = page.evaluate(
            """({maxElements}) => {
              const visible = (el) => {
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
              };
              const cssPath = (el) => {
                if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
                const parts = [];
                let node = el;
                while (node && node.nodeType === 1) {
                  let part = node.tagName.toLowerCase();
                  const name = node.getAttribute('name');
                  if (name) part += '[name="' + CSS.escape(name) + '"]';
                  if (node.parentElement) {
                    const siblings = Array.from(node.parentElement.children).filter(x => x.tagName === node.tagName);
                    if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
                  }
                  parts.unshift(part);
                  node = node.parentElement;
                }
                return parts.join(' > ');
              };
              const nodes = Array.from(document.querySelectorAll(
                'a,button,input,textarea,select,[role],[tabindex],[contenteditable="true"]'
              )).filter(visible);
              const label = (el) => {
                const labelled = (el.getAttribute('aria-labelledby') || '').split(/\\s+/)
                  .map(id => document.getElementById(id)?.textContent || '').join(' ').trim();
                return el.getAttribute('aria-label') || labelled ||
                  Array.from(el.labels || []).map(x => x.innerText).join(' ').trim() ||
                  el.innerText || el.getAttribute('placeholder') || '';
              };
              return {
                text: document.body ? document.body.innerText : '',
                elements_truncated: nodes.length > maxElements,
                element_count: nodes.length,
                elements: nodes.slice(0, maxElements).map((el, i) => ({
                  index: i,
                  tag: el.tagName.toLowerCase(),
                  role: el.getAttribute('role'),
                  type: el.getAttribute('type'),
                  text: (label(el) || (el.type === 'password' ? '' : el.value) || '').trim().slice(0, 300),
                  disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
                  checked: typeof el.checked === 'boolean' ? el.checked : el.getAttribute('aria-checked'),
                  selector: cssPath(el),
                })),
              };
            }""",
            {"maxElements": max_elements},
        )
        text = str(data.get("text") or "")
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "text": text,
            "text_truncated": truncated,
            "elements": data.get("elements", []),
            "elements_truncated": bool(data.get("elements_truncated")),
            "element_count": data.get("element_count", len(data.get("elements", []))),
        }


def screenshot(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        image = page.screenshot(type="png", full_page=bool(args.get("full_page", False)), timeout=_timeout(args))
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "mime_type": "image/png",
            "bytes": len(image),
            "_mcp_image_data": base64.b64encode(image).decode("ascii"),
        }


def evaluate(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        timeout_ms = _timeout(args)
        timer_ms = max(1, timeout_ms - min(25, max(1, timeout_ms // 10)))
        script = str(args["script"])
        timeout_marker = "__CODING_TOOLS_MCP_BROWSER_TIMEOUT__"
        expression = f"""
            (async () => {{
              const source = {json.dumps(script)};
              const run = Promise.resolve().then(async () => {{
                const value = (0, eval)(source);
                return await (typeof value === 'function' ? value() : value);
              }});
              const deadline = new Promise((_, reject) => {{
                setTimeout(() => reject(new Error({json.dumps(timeout_marker)})), {timer_ms});
              }});
              return await Promise.race([run, deadline]);
            }})()
        """
        session = page.context.new_cdp_session(page)
        try:
            response = session.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                    # CDP's Runtime.evaluate timeout also terminates scripts
                    # that block the page event loop, where setTimeout cannot
                    # fire. This is the operation deadline, not only the CDP
                    # connection timeout.
                    "timeout": timeout_ms,
                },
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "timed out" in message.lower() or "execution was terminated" in message.lower():
                raise ToolFailure(
                    "BROWSER_TIMEOUT",
                    f"JavaScript evaluation exceeded {timeout_ms} ms.",
                    category="runtime",
                    retryable=True,
                    details={"timeout_ms": timeout_ms},
                ) from exc
            raise ToolFailure("BROWSER_ERROR", f"JavaScript evaluation failed: {exc}", category="runtime") from exc
        finally:
            try:
                session.detach()
            except Exception:  # noqa: BLE001
                pass

        exception = response.get("exceptionDetails")
        if isinstance(exception, dict):
            text = str(exception.get("text") or "JavaScript evaluation failed")
            raw_exception = exception.get("exception")
            description = (
                str(raw_exception.get("description") or "")
                if isinstance(raw_exception, dict)
                else ""
            )
            if timeout_marker in description or timeout_marker in text:
                raise ToolFailure(
                    "BROWSER_TIMEOUT",
                    f"JavaScript evaluation exceeded {timeout_ms} ms.",
                    category="runtime",
                    retryable=True,
                    details={"timeout_ms": timeout_ms},
                )
            raise ToolFailure(
                "BROWSER_ERROR",
                f"JavaScript evaluation failed: {description or text}",
                category="runtime",
            )
        remote = response.get("result")
        remote_result: dict[str, Any] = remote if isinstance(remote, dict) else {}
        if "value" in remote_result:
            result = remote_result["value"]
        elif remote_result.get("type") == "undefined":
            result = None
        else:
            result = remote_result.get("unserializableValue") or remote_result.get("description")
        return {"ok": True, "tab": _tab_payload(page, index), "result": result}


def click(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        selector = str(args["selector"])
        dialog_result: dict[str, Any] = {}
        dialog_action = args.get("dialog_action")
        if dialog_action:
            def handle_dialog(dialog: Any) -> None:
                dialog_result.update({"type": dialog.type, "message": dialog.message})
                if dialog_action == "accept":
                    dialog.accept(str(args.get("dialog_text", "")))
                else:
                    dialog.dismiss()

            page.once("dialog", handle_dialog)
        try:
            page.locator(selector).first.click(timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Click failed for selector {selector!r}: {exc}", category="runtime") from exc
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "selector": selector,
            "dialog": dialog_result or None,
        }


def type_text(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        selector = str(args["selector"])
        text = str(args["text"])
        locator = page.locator(selector).first
        try:
            if bool(args.get("clear", True)):
                locator.fill(text, timeout=_timeout(args))
            else:
                locator.press_sequentially(text, delay=int(args.get("delay_ms", 0)), timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Typing failed for selector {selector!r}: {exc}", category="runtime") from exc
        return {"ok": True, "tab": _tab_payload(page, index), "selector": selector, "characters": len(text)}


def navigate(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args["url"])
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolFailure("INVALID_ARGUMENT", "Browser navigation requires an http(s) URL.", category="validation")
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        try:
            response = page.goto(url, wait_until=str(args.get("wait_until", "domcontentloaded")), timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Navigation failed for {url!r}: {exc}", category="runtime") from exc
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "status": response.status if response is not None else None,
        }


def back(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        try:
            response = page.go_back(wait_until=str(args.get("wait_until", "domcontentloaded")), timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Back navigation failed: {exc}", category="runtime") from exc
        return {"ok": True, "tab": _tab_payload(page, index), "navigated": response is not None}


def reload(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        try:
            response = page.reload(wait_until=str(args.get("wait_until", "domcontentloaded")), timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Reload failed: {exc}", category="runtime") from exc
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "status": response.status if response is not None else None,
        }


def hover(args: dict[str, Any]) -> dict[str, Any]:
    selector = str(args["selector"])
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        try:
            page.locator(selector).first.hover(timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Hover failed for selector {selector!r}: {exc}", category="runtime") from exc
        return {"ok": True, "tab": _tab_payload(page, index), "selector": selector}


def select_option(args: dict[str, Any]) -> dict[str, Any]:
    selector = str(args["selector"])
    values = [str(item) for item in args["values"]]
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        try:
            selected = page.locator(selector).first.select_option(values, timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Select failed for selector {selector!r}: {exc}", category="runtime") from exc
        return {"ok": True, "tab": _tab_payload(page, index), "selector": selector, "values": selected}


def press(args: dict[str, Any]) -> dict[str, Any]:
    key = str(args["key"])
    selector = args.get("selector")
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        try:
            if selector:
                page.locator(str(selector)).first.press(key, timeout=_timeout(args))
            else:
                page.keyboard.press(key)
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Key press {key!r} failed: {exc}", category="runtime") from exc
        return {"ok": True, "tab": _tab_payload(page, index), "selector": selector, "key": key}


def upload(args: dict[str, Any]) -> dict[str, Any]:
    selector = str(args["selector"])
    files = [str(item) for item in args["_resolved_files"]]
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        try:
            page.locator(selector).first.set_input_files(files, timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Upload failed for selector {selector!r}: {exc}", category="runtime") from exc
        return {"ok": True, "tab": _tab_payload(page, index), "selector": selector, "file_count": len(files)}


def _download_url(page: Any, args: dict[str, Any]) -> str:
    selector = args.get("selector")
    supplied_url = args.get("url")
    if bool(selector) == bool(supplied_url):
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "Provide exactly one of selector or url for browser_download.",
            category="validation",
        )
    if selector:
        try:
            href = page.locator(str(selector)).first.get_attribute("href", timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure(
                "BROWSER_ERROR",
                f"Could not resolve download href for selector {selector!r}: {exc}",
                category="runtime",
            ) from exc
        if not href:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Download selector {selector!r} does not expose an href.",
                category="validation",
            )
        candidate = urllib.parse.urljoin(page.url, href)
    else:
        candidate = urllib.parse.urljoin(page.url, str(supplied_url))
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolFailure(
            "INVALID_ARGUMENT",
            "Browser download requires an HTTP(S) resource URL.",
            category="validation",
        )
    return candidate


def _header_value(headers: dict[str, Any], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _suggested_filename(url: str, headers: dict[str, Any], explicit: Any) -> str:
    if explicit is not None:
        requested = str(explicit).strip()
        if not requested or requested in {".", ".."} or "/" in requested or "\\" in requested:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "Download filename must be a single file name without path separators.",
                category="validation",
            )
        candidate = requested
    else:
        candidate = ""
        disposition = _header_value(headers, "content-disposition") or ""
        match = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8''|)([^;]+)", disposition, re.IGNORECASE)
        if match:
            candidate = urllib.parse.unquote(match.group(1).strip().strip('"'))
        if not candidate:
            match = re.search(r"filename\s*=\s*(?:\"([^\"]+)\"|([^;]+))", disposition, re.IGNORECASE)
            if match:
                candidate = (match.group(1) or match.group(2) or "").strip()
        if not candidate:
            candidate = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name) or "download"
        candidate = candidate.replace("\\", "/").split("/")[-1]
    candidate = re.sub(r"[\x00-\x1f\x7f]", "_", candidate).strip().strip(".")
    if not candidate:
        candidate = "download"
    if len(candidate) > 180:
        suffix = Path(candidate).suffix[:32]
        stem_limit = max(1, 180 - len(suffix))
        candidate = candidate[:stem_limit] + suffix
    return candidate


def download(args: dict[str, Any]) -> dict[str, Any]:
    max_bytes = int(args.get("max_bytes", DEFAULT_DOWNLOAD_MAX_BYTES))
    if max_bytes <= 0:
        raise ToolFailure("INVALID_ARGUMENT", "max_bytes must be positive.", category="validation")
    download_root_raw = args.get("_download_root")
    if not download_root_raw:
        raise ToolFailure("INTERNAL_ERROR", "Managed download root was not supplied.", category="internal")
    download_root = Path(str(download_root_raw)).expanduser().resolve()
    download_root.mkdir(parents=True, mode=0o700, exist_ok=True)

    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        url = _download_url(page, args)
        session = page.context.new_cdp_session(page)
        stream_handle: str | None = None
        download_dir: Path | None = None
        partial_path: Path | None = None
        try:
            frame_tree = session.send("Page.getFrameTree")
            frame_id = str(frame_tree["frameTree"]["frame"]["id"])
            loaded = session.send(
                "Network.loadNetworkResource",
                {
                    "frameId": frame_id,
                    "url": url,
                    "options": {"disableCache": True, "includeCredentials": True},
                },
            )
            resource = loaded.get("resource") if isinstance(loaded, dict) else None
            if not isinstance(resource, dict) or not resource.get("success"):
                raise ToolFailure(
                    "BROWSER_ERROR",
                    f"Chrome could not load download resource {url!r}.",
                    category="runtime",
                    details={
                        "url": url,
                        "net_error": resource.get("netError") if isinstance(resource, dict) else None,
                        "net_error_name": resource.get("netErrorName") if isinstance(resource, dict) else None,
                    },
                )
            status = int(resource.get("httpStatusCode") or 0)
            raw_headers = resource.get("headers")
            headers: dict[str, Any] = (
                {str(key): value for key, value in raw_headers.items()}
                if isinstance(raw_headers, dict)
                else {}
            )
            if status >= 400:
                raise ToolFailure(
                    "BROWSER_ERROR",
                    f"Download resource returned HTTP {status}.",
                    category="runtime",
                    details={"url": url, "status": status},
                )
            length = _header_value(headers, "content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ToolFailure(
                    "BROWSER_DOWNLOAD_TOO_LARGE",
                    f"Download is larger than the configured {max_bytes}-byte limit.",
                    category="validation",
                    details={"url": url, "content_length": int(length), "max_bytes": max_bytes},
                )
            stream = resource.get("stream")
            if not stream:
                raise ToolFailure(
                    "BROWSER_DOWNLOAD_UNAVAILABLE",
                    "Chrome did not provide a readable stream for this resource.",
                    category="runtime",
                    retryable=True,
                    details={"url": url},
                )
            stream_handle = str(stream)
            filename = _suggested_filename(url, headers, args.get("filename"))
            download_id = secrets.token_hex(12)
            download_dir = download_root / download_id
            download_dir.mkdir(mode=0o700)
            partial_path = download_dir / ".partial"
            final_path = download_dir / filename
            digest = hashlib.sha256()
            received = 0
            fd = os.open(partial_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk_result = session.send(
                        "IO.read",
                        {"handle": stream_handle, "size": min(65536, max_bytes - received + 1)},
                    )
                    chunk_text = str(chunk_result.get("data") or "")
                    if chunk_result.get("base64Encoded"):
                        chunk = base64.b64decode(chunk_text)
                    else:
                        chunk = chunk_text.encode("utf-8")
                    received += len(chunk)
                    if received > max_bytes:
                        raise ToolFailure(
                            "BROWSER_DOWNLOAD_TOO_LARGE",
                            f"Download exceeded the configured {max_bytes}-byte limit while streaming.",
                            category="validation",
                            details={"url": url, "max_bytes": max_bytes},
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    if chunk_result.get("eof"):
                        break
            os.replace(partial_path, final_path)
            partial_path = None
            return {
                "ok": True,
                "tab": _tab_payload(page, index),
                "download_id": download_id,
                "filename": filename,
                "bytes": received,
                "sha256": digest.hexdigest(),
                "url": url,
                "status": status,
                "content_type": _header_value(headers, "content-type"),
                "managed_path": str(final_path),
                "note": (
                    "Downloaded through the selected tab's CDP network context into the runtime-managed "
                    "download directory. The browser's native Downloads UI is not used."
                ),
            }
        except ToolFailure:
            if partial_path is not None:
                partial_path.unlink(missing_ok=True)
            if download_dir is not None:
                try:
                    download_dir.rmdir()
                except OSError:
                    pass
            raise
        except Exception as exc:  # noqa: BLE001
            if partial_path is not None:
                partial_path.unlink(missing_ok=True)
            if download_dir is not None:
                try:
                    download_dir.rmdir()
                except OSError:
                    pass
            raise ToolFailure("BROWSER_ERROR", f"Browser download failed: {exc}", category="runtime") from exc
        finally:
            if stream_handle is not None:
                try:
                    session.send("IO.close", {"handle": stream_handle})
                except Exception:  # noqa: BLE001
                    pass
            try:
                session.detach()
            except Exception:  # noqa: BLE001
                pass


def wait(args: dict[str, Any]) -> dict[str, Any]:
    selector = args.get("selector")
    url = args.get("url")
    text = args.get("text")
    wait_ms = int(args.get("wait_ms", 0))
    if not selector and not url and text is None and wait_ms <= 0:
        raise ToolFailure("INVALID_ARGUMENT", "Provide selector, url, text, or a positive wait_ms.", category="validation")
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        try:
            if selector:
                page.locator(str(selector)).first.wait_for(
                    state=str(args.get("state", "visible")), timeout=_timeout(args)
                )
            if url:
                page.wait_for_url(str(url), timeout=_timeout(args))
            if text is not None:
                page.get_by_text(str(text), exact=bool(args.get("exact", False))).first.wait_for(
                    state=str(args.get("state", "visible")), timeout=_timeout(args)
                )
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_TIMEOUT", f"Browser wait condition was not met: {exc}", category="runtime", retryable=True) from exc
        return {"ok": True, "tab": _tab_payload(page, index), "matched": True}


def events(args: dict[str, Any]) -> dict[str, Any]:
    wait_ms = int(args.get("wait_ms", 500))
    max_entries = int(args.get("max_entries", 300))
    trigger_selector = args.get("trigger_selector")
    captured: list[dict[str, Any]] = []
    truncated = False

    def append(event: dict[str, Any]) -> None:
        nonlocal truncated
        if len(captured) >= max_entries:
            truncated = True
            return
        captured.append(event)

    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))

        def on_console(message: Any) -> None:
            append({"kind": "console", "type": message.type, "text": message.text})

        def on_page_error(error: Any) -> None:
            append({"kind": "pageerror", "text": str(error)})

        def on_request_failed(request: Any) -> None:
            append({
                "kind": "requestfailed",
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "failure": request.failure,
            })

        def on_dialog(dialog: Any) -> None:
            append({"kind": "dialog", "type": dialog.type, "message": dialog.message})
            if args.get("dialog_action", "dismiss") == "accept":
                dialog.accept(str(args.get("dialog_text", "")))
            else:
                dialog.dismiss()

        def on_popup(popup: Any) -> None:
            append({"kind": "popup", "url": popup.url})

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("dialog", on_dialog)
        page.on("popup", on_popup)
        try:
            if bool(args.get("reload", False)):
                page.reload(wait_until="domcontentloaded", timeout=_timeout(args))
            if trigger_selector:
                page.locator(str(trigger_selector)).first.click(timeout=_timeout(args))
            if wait_ms:
                page.wait_for_timeout(wait_ms)
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Browser event capture failed: {exc}", category="runtime") from exc
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "events": captured,
            "count": len(captured),
            "truncated": truncated,
            "wait_ms": wait_ms,
            "trigger_selector": trigger_selector,
            "note": (
                "Only events emitted after this Playwright attachment are observable. "
                "Use trigger_selector when one click must be observed in the same call; "
                "this is bounded sampling, not a persistent subscription. Download events "
                "are not reliable in the current attach-to-default-context CDP architecture."
            ),
        }


def console(args: dict[str, Any]) -> dict[str, Any]:
    wait_ms = int(args.get("wait_ms", 250))
    max_entries = int(args.get("max_entries", 200))
    reload_page = bool(args.get("reload", False))
    entries: list[dict[str, Any]] = []

    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))

        def on_console(message: Any) -> None:
            if len(entries) >= max_entries:
                return
            try:
                location = message.location
            except Exception:  # noqa: BLE001
                location = {}
            entries.append(
                {
                    "kind": "console",
                    "type": message.type,
                    "text": message.text,
                    "location": location,
                }
            )

        def on_page_error(error: Any) -> None:
            if len(entries) >= max_entries:
                return
            entries.append(
                {
                    "kind": "pageerror",
                    "type": "error",
                    "text": str(error),
                    "location": {},
                }
            )

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        try:
            if reload_page:
                page.reload(wait_until="domcontentloaded", timeout=_timeout(args))
            if wait_ms:
                page.wait_for_timeout(wait_ms)
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Console capture failed: {exc}", category="runtime") from exc

        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "entries": entries,
            "count": len(entries),
            "reload": reload_page,
            "wait_ms": wait_ms,
            "note": (
                "Only messages emitted after this Playwright attachment are observable; "
                "set reload=true to capture page-load console output."
            ),
        }


def network(args: dict[str, Any]) -> dict[str, Any]:
    wait_ms = int(args.get("wait_ms", 250))
    max_entries = int(args.get("max_entries", 300))
    reload_page = bool(args.get("reload", False))
    include_resources = bool(args.get("include_resources", True))
    events: list[dict[str, Any]] = []

    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))

        def on_response(response: Any) -> None:
            if len(events) >= max_entries:
                return
            request = response.request
            events.append(
                {
                    "kind": "response",
                    "url": response.url,
                    "status": response.status,
                    "ok": response.ok,
                    "method": request.method,
                    "resource_type": request.resource_type,
                }
            )

        def on_request_failed(request: Any) -> None:
            if len(events) >= max_entries:
                return
            events.append(
                {
                    "kind": "requestfailed",
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "failure": request.failure,
                }
            )

        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        try:
            if reload_page:
                page.reload(wait_until="domcontentloaded", timeout=_timeout(args))
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            resources = (
                page.evaluate(
                    """() => performance.getEntriesByType('resource').map((entry) => ({
                      name: entry.name,
                      initiator_type: entry.initiatorType,
                      duration_ms: Math.round(entry.duration * 100) / 100,
                      transfer_size: entry.transferSize || 0,
                      encoded_body_size: entry.encodedBodySize || 0,
                      decoded_body_size: entry.decodedBodySize || 0,
                      response_status: entry.responseStatus || null,
                    }))"""
                )
                if include_resources
                else []
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Network capture failed: {exc}", category="runtime") from exc

        if len(resources) > max_entries:
            resources = resources[-max_entries:]
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "events": events,
            "resources": resources,
            "event_count": len(events),
            "resource_count": len(resources),
            "reload": reload_page,
            "wait_ms": wait_ms,
            "note": (
                "events contains traffic observed after attachment; resources contains current "
                "PerformanceResourceTiming entries when include_resources=true."
            ),
        }


def inspect(args: dict[str, Any]) -> dict[str, Any]:
    selector = str(args["selector"])
    max_html_chars = int(args.get("max_html_chars", 20000))
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"), args.get("tab_id"))
        locator = page.locator(selector)
        try:
            count = locator.count()
            if count == 0:
                raise ToolFailure(
                    "NOT_FOUND",
                    f"No element matched selector {selector!r}.",
                    category="not_found",
                )
            data = locator.first.evaluate(
                """(el) => {
                  const style = getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  const attrs = {};
                  for (const attr of el.attributes || []) attrs[attr.name] = attr.value;
                  const parents = [];
                  let node = el.parentElement;
                  while (node && parents.length < 8) {
                    parents.push({
                      tag: node.tagName.toLowerCase(),
                      id: node.id || null,
                      class: node.className && typeof node.className === 'string' ? node.className : null,
                    });
                    node = node.parentElement;
                  }
                  const animations = el.getAnimations ? el.getAnimations().map((animation) => {
                    let keyframes = [];
                    try {
                      keyframes = animation.effect && animation.effect.getKeyframes
                        ? animation.effect.getKeyframes()
                        : [];
                    } catch (_) {}
                    return {
                      play_state: animation.playState,
                      current_time: animation.currentTime,
                      playback_rate: animation.playbackRate,
                      keyframes,
                    };
                  }) : [];
                  return {
                    tag: el.tagName.toLowerCase(),
                    text: (el.innerText || el.textContent || '').trim(),
                    attributes: attrs,
                    outer_html: el.outerHTML || '',
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    visible: style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0,
                    computed_style: {
                      display: style.display,
                      position: style.position,
                      visibility: style.visibility,
                      opacity: style.opacity,
                      z_index: style.zIndex,
                      width: style.width,
                      height: style.height,
                      margin: style.margin,
                      padding: style.padding,
                      color: style.color,
                      background_color: style.backgroundColor,
                      font: style.font,
                      transform: style.transform,
                      transition: style.transition,
                      animation: style.animation,
                      overflow: style.overflow,
                      pointer_events: style.pointerEvents,
                    },
                    parents,
                    animations,
                  };
                }"""
            )
        except ToolFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Element inspection failed: {exc}", category="runtime") from exc

        html = str(data.get("outer_html") or "")
        html_truncated = len(html) > max_html_chars
        if html_truncated:
            data["outer_html"] = html[:max_html_chars]
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "selector": selector,
            "matched": count,
            "html_truncated": html_truncated,
            "element": data,
        }
