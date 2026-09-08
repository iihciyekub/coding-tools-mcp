from __future__ import annotations

import base64
import os
import urllib.parse
from contextlib import contextmanager
from typing import Any, Iterator

from .errors import ToolFailure


DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_TIMEOUT_MS = 5000


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
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=_timeout(args))
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


def _tab_payload(page: Any, index: int) -> dict[str, Any]:
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = ""
    try:
        visibility = page.evaluate("document.visibilityState")
    except Exception:  # noqa: BLE001
        visibility = "unknown"
    return {"index": index, "title": title, "url": page.url, "visibility": visibility}


def _select_page(browser: Any, tab_index: int | None = None) -> tuple[Any, int]:
    pages = _pages(browser)
    if not pages:
        raise ToolFailure("NOT_FOUND", "Chrome has no inspectable pages.", category="not_found")
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
        page, index = _select_page(browser, args.get("tab_index"))
        data = page.evaluate(
            """({maxElements}) => {
              const visible = (el) => {
                const s = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
              };
              const cssPath = (el) => {
                if (el.id) return '#' + CSS.escape(el.id);
                const parts = [];
                let node = el;
                while (node && node.nodeType === 1 && node !== document.documentElement && parts.length < 6) {
                  let part = node.tagName.toLowerCase();
                  const name = node.getAttribute('name');
                  if (name) part += '[name="' + CSS.escape(name) + '"]';
                  else if (node.parentElement) {
                    const siblings = Array.from(node.parentElement.children).filter(x => x.tagName === node.tagName);
                    if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
                  }
                  parts.unshift(part);
                  node = node.parentElement;
                }
                return parts.join(' > ');
              };
              const nodes = Array.from(document.querySelectorAll(
                'a,button,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"]'
              )).filter(visible).slice(0, maxElements);
              return {
                text: document.body ? document.body.innerText : '',
                elements: nodes.map((el, i) => ({
                  index: i,
                  tag: el.tagName.toLowerCase(),
                  role: el.getAttribute('role'),
                  type: el.getAttribute('type'),
                  text: (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 300),
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
        }


def screenshot(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"))
        image = page.screenshot(type="png", full_page=bool(args.get("full_page", False)))
        return {
            "ok": True,
            "tab": _tab_payload(page, index),
            "mime_type": "image/png",
            "bytes": len(image),
            "_mcp_image_data": base64.b64encode(image).decode("ascii"),
        }


def evaluate(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"))
        try:
            result = page.evaluate(str(args["script"]))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"JavaScript evaluation failed: {exc}", category="runtime") from exc
        return {"ok": True, "tab": _tab_payload(page, index), "result": result}


def click(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"))
        selector = str(args["selector"])
        try:
            page.locator(selector).first.click(timeout=_timeout(args))
        except Exception as exc:  # noqa: BLE001
            raise ToolFailure("BROWSER_ERROR", f"Click failed for selector {selector!r}: {exc}", category="runtime") from exc
        return {"ok": True, "tab": _tab_payload(page, index), "selector": selector}


def type_text(args: dict[str, Any]) -> dict[str, Any]:
    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"))
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


def console(args: dict[str, Any]) -> dict[str, Any]:
    wait_ms = int(args.get("wait_ms", 250))
    max_entries = int(args.get("max_entries", 200))
    reload_page = bool(args.get("reload", False))
    entries: list[dict[str, Any]] = []

    with _browser_connection(args) as browser:
        page, index = _select_page(browser, args.get("tab_index"))

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
        page, index = _select_page(browser, args.get("tab_index"))

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
        page, index = _select_page(browser, args.get("tab_index"))
        locator = page.locator(selector).first
        try:
            count = locator.count()
            if count == 0:
                raise ToolFailure(
                    "NOT_FOUND",
                    f"No element matched selector {selector!r}.",
                    category="not_found",
                )
            data = locator.evaluate(
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
