"""Optional, disposable Web acceptance checks through the existing exec tool.

This is not a browser MCP server or a personal Chrome-session connector. The
CLI has no model integration and never installs its optional dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit


MAX_SCENARIO_BYTES = 131_072
MAX_STEPS = 32
MAX_EVENTS = 100
MAX_TEXT_BYTES = 32_768
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ACTIONS = frozenset({"click", "fill", "press", "wait_visible", "wait_text"})
KEYS = frozenset({"Enter", "Tab", "Escape", "Space", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"})
EVENT_GROUPS = ("console_errors", "page_errors", "failed_requests", "http_errors", "blocked_requests")


def bounded_text(value: str, limit: int = 2048) -> str:
    raw = value.encode("utf-8", errors="replace")
    return raw[:limit].decode("utf-8", errors="ignore") + (" [truncated]" if len(raw) > limit else "")


def safe_url(value: str) -> str:
    """Never write URL credentials, query values or fragments into evidence."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        authority = host + (f":{parsed.port}" if parsed.port is not None else "")
        return bounded_text(urlunsplit((parsed.scheme, authority, parsed.path, "", "")))
    except ValueError:
        return "[invalid URL]"


def safe_message(value: str) -> str:
    value = re.sub(r"(?:https?|wss?)://[^\s<>\"']+", lambda match: safe_url(match.group()), value)
    value = re.sub(
        r"(?i)\b(authorization|cookie|password|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]", value,
    )
    return bounded_text(value)


def local_origin(value: str, *, websocket: bool = False) -> tuple[str, str, int]:
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("URL must be a string of at most 8192 characters.")
    parsed = urlsplit(value)
    scheme = "http" if websocket and parsed.scheme == "ws" else parsed.scheme
    if scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Only an explicit HTTP loopback development URL is supported.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not supported.")
    return scheme, parsed.hostname, parsed.port or 80


def validate_scenario(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) - {"steps"}:
        raise ValueError("Scenario must be an object containing only steps.")
    steps = value.get("steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ValueError(f"steps must be an array with at most {MAX_STEPS} entries.")
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("action"), str) or step["action"] not in ACTIONS:
            raise ValueError(f"steps[{index}] has an unsupported action.")
        action = step["action"]
        allowed = {"action", "timeout_ms", "target"}
        if action in {"fill", "press"}:
            allowed.add("value")
        if action == "wait_text":
            allowed = {"action", "timeout_ms", "text"}
        if set(step) - allowed:
            raise ValueError(f"steps[{index}] contains unknown properties.")
        timeout = step.get("timeout_ms", 5000)
        if type(timeout) is not int or not 1 <= timeout <= 10000:
            raise ValueError("Step timeout_ms must be an integer between 1 and 10000.")
        if action == "wait_text":
            if not isinstance(step.get("text"), str) or not 1 <= len(step["text"]) <= 2000:
                raise ValueError("wait_text requires 1-2000 characters of exact visible text.")
        else:
            target = step.get("target")
            if not isinstance(target, dict) or set(target) not in ({"role", "name"}, {"label"}, {"test_id"}):
                raise ValueError("target must contain role/name, label, or test_id; selectors and scripts are unsupported.")
            if any(not isinstance(item, str) or not 1 <= len(item) <= 500 for item in target.values()):
                raise ValueError("Target fields must be nonempty strings of at most 500 characters.")
        if action in {"fill", "press"} and (not isinstance(step.get("value"), str) or len(step["value"]) > 10000):
            raise ValueError("fill/press requires a string value of at most 10000 characters.")
        if action == "press" and step["value"] not in KEYS:
            raise ValueError("press supports only the documented page-interaction keys.")
    return steps


def _locator(page: Any, target: dict[str, str]) -> Any:
    if "role" in target:
        return page.get_by_role(target["role"], name=target["name"], exact=True)
    if "label" in target:
        return page.get_by_label(target["label"], exact=True)
    return page.get_by_test_id(target["test_id"])


async def run_check(
    url: str, scenario: Any, output: Path, *, timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Run a bounded scenario, returning paths to explicit, local evidence."""
    origin = local_origin(url)
    steps = validate_scenario(scenario)
    if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 120000:
        raise ValueError("timeout_ms must be between 1000 and 120000.")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="browser-check-", dir=output))
    report: dict[str, Any] = {
        "ok": False, "target_url": safe_url(url), "workdir": str(Path.cwd()),
        "steps": [], "cleanup_complete": True, "events": {key: [] for key in EVENT_GROUPS},
        "events_omitted": {key: 0 for key in EVENT_GROUPS}, "evidence": {}, "warnings": [],
        "scope": "disposable Chromium context; requested loopback origin only; not an OS sandbox",
    }
    report["evidence"]["report"] = str(run_dir / "report.json")
    driver = browser = context = page = None
    started = time.monotonic()

    def record(group: str, **item: Any) -> None:
        if len(report["events"][group]) < MAX_EVENTS:
            report["events"][group].append(item)
        else:
            report["events_omitted"][group] += 1

    def allowed(value: str, *, websocket: bool = False) -> bool:
        try:
            return local_origin(value, websocket=websocket) == origin
        except ValueError:
            return False

    async def route_request(route: Any) -> None:
        request_url = route.request.url
        if not allowed(request_url):
            record("blocked_requests", url=safe_url(request_url), transport="http")
            await route.abort("blockedbyclient")
            return
        response = None
        try:
            # Playwright routes only the first URL of a native redirect chain.
            # Fetch without following redirects and refuse redirects entirely
            # in v1, before the browser can issue the next request.
            response = await route.fetch(max_redirects=0, timeout=5000)
            if response.status in {301, 302, 303, 307, 308}:
                destination = urljoin(request_url, response.headers.get("location", ""))
                record("blocked_requests", url=safe_url(destination), transport="http", reason="redirect_not_supported")
                await route.abort("blockedbyclient")
            else:
                await route.fulfill(response=response)
        except Exception as exc:
            record("failed_requests", url=safe_url(request_url), failure=type(exc).__name__)
            try:
                await route.abort("failed")
            except Exception:
                pass  # The deadline may already have closed this page.
        finally:
            if response is not None:
                try:
                    await response.dispose()
                except Exception:
                    pass

    async def route_socket(socket: Any) -> None:
        if allowed(socket.url, websocket=True):
            # This method returns a route immediately, even in the async API.
            socket.connect_to_server()
        else:
            record("blocked_requests", url=safe_url(socket.url), transport="websocket")
            await socket.close(code=1008, reason="Outside requested development origin")

    async def execute() -> None:
        nonlocal driver, browser, context, page
        from playwright.async_api import async_playwright

        driver = await async_playwright().start()
        browser = await driver.chromium.launch(headless=True, timeout=min(timeout_ms, 10000))
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, accept_downloads=False,
            service_workers="block", permissions=[],
        )
        # Browser-only permission for the explicitly selected disposable test
        # origin. Routing below still rejects other local ports and hosts.
        # No persistent Chrome profile or operating-system permission changes.
        target = urlsplit(url)
        await context.grant_permissions(
            ["local-network-access"], origin=urlunsplit((target.scheme, target.netloc, "", "", "")),
        )
        await context.route("**/*", route_request)
        await context.route_web_socket("**/*", route_socket)
        context.on("console", lambda message: record("console_errors", message=safe_message(message.text)) if message.type == "error" else None)
        context.on("weberror", lambda error: record("page_errors", message=safe_message(str(error.error))))
        context.on("requestfailed", lambda request: record("failed_requests", url=safe_url(request.url), failure=safe_message(request.failure or "failed")))
        context.on("response", lambda response: record("http_errors", url=safe_url(response.url), status=response.status) if response.status >= 400 else None)
        page = await context.new_page()
        page.set_default_timeout(5000)
        await page.goto(url, wait_until="domcontentloaded", timeout=min(timeout_ms, 10000))
        for index, step in enumerate(steps):
            # Intentionally omit fill values and page text from action receipts.
            receipt = {"index": index, "action": step["action"], "status": "running"}
            report["steps"].append(receipt)
            options = {"timeout": step.get("timeout_ms", 5000)}
            try:
                if step["action"] == "wait_text":
                    await page.get_by_text(step["text"], exact=True).wait_for(state="visible", **options)
                else:
                    locator = _locator(page, step["target"])
                    if step["action"] == "click":
                        await locator.click(**options)
                    elif step["action"] == "fill":
                        await locator.fill(step["value"], **options)
                    elif step["action"] == "press":
                        await locator.press(step["value"], **options)
                    else:
                        await locator.wait_for(state="visible", **options)
                receipt["status"] = "completed"
            except BaseException:
                receipt["status"] = "failed"
                raise
        report["completed"] = True

    try:
        await asyncio.wait_for(execute(), timeout_ms / 1000)
    except ImportError:
        report["error"] = {"code": "BROWSER_DEPENDENCY_MISSING", "message": "Install coding-tools-mcp[browser] and run python -m playwright install chromium in the same environment."}
    except TimeoutError:
        report["error"] = {"code": "BROWSER_DEADLINE", "message": "Browser check exceeded its total execution deadline."}
    except Exception as exc:
        # Playwright exception strings include action call logs and fill values.
        # The scenario index and bounded runtime events provide recovery evidence
        # without copying those raw exception messages into the report.
        report["error"] = {"code": "BROWSER_CHECK_FAILED", "message": f"{type(exc).__name__}: browser setup or scenario failed; inspect step receipts and events. Check Playwright Chromium installation if setup did not complete."}
    finally:
        if page is not None:
            report["final_url"] = safe_url(page.url)
            try:
                snapshot = await asyncio.wait_for(page.locator("body").aria_snapshot(timeout=2000), 3)
                snapshot_path = run_dir / "snapshot.txt"
                snapshot_path.write_text(bounded_text(snapshot, MAX_TEXT_BYTES), encoding="utf-8")
                report["evidence"]["snapshot"] = str(snapshot_path)
            except Exception:
                report["warnings"].append("Page structure snapshot unavailable.")
            try:
                image = await asyncio.wait_for(page.screenshot(type="png", full_page=False, timeout=2000), 3)
                if len(image) <= MAX_IMAGE_BYTES:
                    image_path = run_dir / "screenshot.png"
                    image_path.write_bytes(image)
                    report["evidence"]["screenshot"] = str(image_path)
                else:
                    report["warnings"].append("Screenshot exceeded image byte limit and was not retained.")
            except Exception:
                report["warnings"].append("Screenshot unavailable.")
        for resource, method in ((context, "close"), (browser, "close"), (driver, "stop")):
            if resource is not None:
                try:
                    await asyncio.wait_for(getattr(resource, method)(), 3)
                except Exception:
                    report["cleanup_complete"] = False
                    report["warnings"].append(f"Browser {method} did not finish cleanly.")
        report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        report["ok"] = bool(report.get("completed")) and report["cleanup_complete"] and "error" not in report and not any(report["events"].values())
        if report.get("completed") and not report["ok"] and "error" not in report:
            report["error"] = {"code": "BROWSER_RUNTIME_ERRORS", "message": "Scenario completed but browser errors, blocked requests, or incomplete cleanup were observed."}
        (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a local Web UI in disposable Chromium; reuse MCP exec/read/image tools.")
    parser.add_argument("--url", required=True, help="HTTP loopback development URL")
    parser.add_argument("--scenario", type=Path, help="Bounded JSON scenario; omitted means observe only")
    parser.add_argument("--output", type=Path, required=True, help="Parent directory for a new, unique evidence folder")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Execution deadline, 1000-120000 ms; evidence/cleanup have separate short bounds")
    args = parser.parse_args(argv)
    try:
        scenario: Any = {"steps": []}
        if args.scenario is not None:
            with args.scenario.open("rb") as handle:
                raw = handle.read(MAX_SCENARIO_BYTES + 1)
            if len(raw) > MAX_SCENARIO_BYTES:
                raise ValueError("Scenario exceeds 128 KiB.")
            scenario = json.loads(raw)
        report = asyncio.run(run_check(args.url, scenario, args.output, timeout_ms=args.timeout_ms))
    except (OSError, ValueError, RecursionError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "INVALID_BROWSER_CHECK", "message": safe_message(str(exc))}}))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
