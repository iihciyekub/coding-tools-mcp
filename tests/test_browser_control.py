from __future__ import annotations

import io
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from coding_tools_mcp import browser, chrome_bridge, chrome_native_host
from coding_tools_mcp.tool_results import render_tool_text


class _DownloadHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/test"}:
            payload = b'''<!doctype html><meta charset="utf-8">
              <button name="action" onclick="window.clicked='first'">First</button>
              <button name="action" onclick="window.clicked='second'">Second</button>
              <label for="secret">Password</label><input id="secret" type="password" value="synthetic-secret">
              <label for="customer">Customer name</label><input id="customer">
              <div role="checkbox" aria-checked="true" tabindex="0">Agree</div>
              <select id="choice"><option value="one">One</option><option value="two">Two</option></select>
              <input id="upload" type="file">
              <a id="download-link" href="/captured.txt">Download</a>
              <button id="dialog" onclick="window.dialogValue=prompt('Name?','')">Dialog</button>
              <button id="event-source" onclick="
                console.log('captured-event');
                setTimeout(() => { throw new Error('captured-page-error'); }, 0);
                fetch('http://127.0.0.1:65534/captured-request-failure').catch(() => {});
                window.open('about:blank?captured-popup', '_blank');
                const link = document.createElement('a');
                link.href = '/captured.txt';
                link.download = 'captured.txt';
                document.body.appendChild(link);
                link.click();
                link.remove();
                alert('captured-dialog');
              ">Events</button>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/captured.txt":
            payload = b"captured-download"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Disposition", 'attachment; filename="captured.txt"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return


class BridgeRegressionTests(unittest.TestCase):
    def test_wait_requires_a_condition_and_navigation_rejects_non_http_urls(self) -> None:
        with self.assertRaisesRegex(Exception, "selector, url, text"):
            browser.wait({})
        with self.assertRaisesRegex(Exception, "http"):
            browser.navigate({"url": "file:///etc/passwd"})

    def test_late_reply_does_not_break_other_requests(self) -> None:
        bridge = chrome_native_host.NativeBridge()
        abandoned, peer = socket.socketpair()
        pending, reader = socket.socketpair()
        self.addCleanup(reader.close)
        peer.close()
        bridge._pending["old"] = (abandoned, time.monotonic() + 5)
        bridge._pending["live"] = (pending, time.monotonic() + 5)
        bridge._dispatch_extension_message({"id": "old", "ok": True})
        bridge._dispatch_extension_message({"id": "live", "ok": True})
        reader.settimeout(1)
        self.assertEqual(json.loads(reader.recv(4096))["id"], "live")
        self.assertEqual(bridge._pending, {})

    def test_expired_requests_release_the_connection(self) -> None:
        bridge = chrome_native_host.NativeBridge()
        conn, peer = socket.socketpair()
        self.addCleanup(peer.close)
        bridge._pending["expired"] = (conn, time.monotonic() - 1)
        bridge._expire_pending()
        self.assertEqual(bridge._pending, {})
        peer.settimeout(1)
        self.assertEqual(peer.recv(1), b"")
        bridge._dispatch_extension_message({"id": "expired", "ok": True})

    def test_oversized_native_message_is_rejected_before_writing(self) -> None:
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            chrome_native_host._write_native_message(output, {"script": "x" * 1048576}, threading.Lock())
        self.assertEqual(output.getvalue(), b"")

    @unittest.skipUnless(os.name == "posix", "Native launcher uses a POSIX shell")
    def test_module_install_can_create_a_native_launcher(self) -> None:
        with tempfile.TemporaryDirectory(prefix="chrome host ' space ") as directory:
            root = Path(directory)
            with patch.object(chrome_bridge.sys, "platform", "darwin"), \
                 patch.object(chrome_bridge, "bridge_dir", return_value=root), \
                 patch.object(chrome_bridge, "_bundled_host_candidate", return_value=None), \
                 patch.object(chrome_bridge, "_manifest_path", return_value=root / "native.json"):
                result = chrome_bridge.install({"open_extensions_page": False})
            host = Path(result["native_host_path"])
            self.assertTrue(os.access(host, os.X_OK))
            # EOF shuts down the host without Chrome; isolate its socket too.
            completed = subprocess.run([str(host)], input=b"", capture_output=True,
                                       env={**os.environ, "HOME": str(root)}, timeout=5)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, b"")
            self.assertEqual(json.loads((root / "native.json").read_text())["path"], str(host))

    def test_network_text_includes_resources_when_events_are_empty(self) -> None:
        text = render_tool_text("browser_network", {
            "events": [], "resources": [{"name": "https://example.invalid/a.js"}]
        }, is_error=False)
        self.assertIn("https://example.invalid/a.js", text)
        self.assertIn("events: 0", text)

    def test_snapshot_text_keeps_controls_after_the_first_hundred(self) -> None:
        text = render_tool_text("browser_snapshot", {
            "elements": [{"index": i, "text": f"control-{i}", "selector": f"#{i}"} for i in range(125)],
            "elements_truncated": True,
        }, is_error=False)
        self.assertIn("control-124", text)
        self.assertIn("interactive elements truncated", text)


@unittest.skipUnless(os.environ.get("CODING_TOOLS_MCP_TEST_CHROME"), "Set CODING_TOOLS_MCP_TEST_CHROME for isolated real-Chrome regressions")
class ChromeRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory(prefix="cmt-browser-regression-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
        cls.http_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.addClassCleanup(cls.httpd.server_close)
        cls.addClassCleanup(cls.httpd.shutdown)
        cls.page_url = f"http://127.0.0.1:{cls.httpd.server_port}/test"
        cls.process = subprocess.Popen([
            os.environ["CODING_TOOLS_MCP_TEST_CHROME"], "--headless=new", "--disable-gpu",
            "--window-size=1280,800",
            "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
            "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
            "--user-data-dir=" + cls.directory.name, "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop_chrome)
        port_file = Path(cls.directory.name) / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not port_file.exists() and cls.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if not port_file.exists():
            raise RuntimeError("Isolated Chrome did not start")
        cls.args = {"endpoint": "http://127.0.0.1:" + port_file.read_text().splitlines()[0], "tab_index": 0, "timeout_ms": 3000}

    @classmethod
    def stop_chrome(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    def setUp(self) -> None:
        with browser._browser_connection(self.args) as connection:
            pages = connection.contexts[0].pages
            for popup in pages[1:]:
                popup.close()
            page = pages[0]
            page.goto(self.page_url, wait_until="domcontentloaded")

    def test_snapshot_target_clicks_the_correct_same_name_button(self) -> None:
        elements = browser.snapshot(self.args)["elements"]
        first, second = elements[:2]
        self.assertNotEqual(first["selector"], second["selector"])
        browser.click({**self.args, "selector": second["selector"]})
        self.assertEqual(browser.evaluate({**self.args, "script": "window.clicked"})["result"], "second")

    def test_password_redaction_and_accessible_control_names(self) -> None:
        snapshot = browser.snapshot(self.args)
        self.assertNotIn("synthetic-secret", json.dumps(snapshot))
        by_selector = {e["selector"]: e for e in snapshot["elements"]}
        self.assertEqual(by_selector["#secret"]["text"], "Password")
        self.assertEqual(by_selector["#customer"]["text"], "Customer name")
        self.assertTrue(any(e["role"] == "checkbox" and e["checked"] == "true" for e in snapshot["elements"]))

    def test_element_truncation_and_inspect_count_are_truthful(self) -> None:
        snapshot = browser.snapshot({**self.args, "max_elements": 1})
        self.assertTrue(snapshot["elements_truncated"])
        self.assertEqual(snapshot["element_count"], 10)
        self.assertEqual(browser.inspect({**self.args, "selector": "button"})["matched"], 4)

    def test_extended_actions_cover_select_press_upload_wait_and_dialog(self) -> None:
        browser.hover({**self.args, "selector": "#customer"})
        browser.press({**self.args, "selector": "#customer", "key": "A"})
        self.assertEqual(browser.evaluate({**self.args, "script": "document.querySelector('#customer').value"})["result"], "A")
        selected = browser.select_option({**self.args, "selector": "#choice", "values": ["two"]})
        self.assertEqual(selected["values"], ["two"])
        upload = Path(self.directory.name) / "upload.txt"
        upload.write_text("upload", encoding="utf-8")
        result = browser.upload({**self.args, "selector": "#upload", "_resolved_files": [str(upload)]})
        self.assertEqual(result["file_count"], 1)
        browser.wait({**self.args, "selector": "#choice", "state": "visible"})
        clicked = browser.click(
            {**self.args, "selector": "#dialog", "dialog_action": "accept", "dialog_text": "Ada"}
        )
        self.assertEqual(clicked["dialog"]["type"], "prompt")
        self.assertEqual(browser.evaluate({**self.args, "script": "window.dialogValue"})["result"], "Ada")

    def test_browser_event_capture_covers_console_error_network_dialog_and_popup(self) -> None:
        self.addCleanup(self._close_extra_pages)
        captured = browser.events(
            {
                **self.args,
                "trigger_selector": "#event-source",
                "wait_ms": 1000,
                "dialog_action": "dismiss",
            }
        )
        kinds = {item["kind"] for item in captured["events"]}
        self.assertTrue(
            {"console", "pageerror", "requestfailed", "dialog", "popup"}.issubset(kinds),
            captured["events"],
        )
        self.assertEqual(captured["trigger_selector"], "#event-source")

    def test_controlled_download_streams_to_managed_storage_and_enforces_size_limit(self) -> None:
        root = Path(self.directory.name) / "managed-downloads"
        downloaded = browser.download(
            {
                **self.args,
                "selector": "#download-link",
                "_download_root": str(root),
                "max_bytes": 1024,
            }
        )
        self.assertEqual(downloaded["filename"], "captured.txt")
        self.assertEqual(downloaded["bytes"], len(b"captured-download"))
        self.assertEqual(Path(downloaded["managed_path"]).read_bytes(), b"captured-download")
        self.assertRegex(downloaded["download_id"], r"^[0-9a-f]{24}$")

        with self.assertRaisesRegex(Exception, "larger than|exceeded"):
            browser.download(
                {
                    **self.args,
                    "selector": "#download-link",
                    "_download_root": str(root),
                    "max_bytes": 4,
                }
            )
        self.assertFalse(any(path.name == ".partial" for path in root.rglob(".partial")))

    def test_runtime_browser_watch_survives_across_calls_and_stops_cleanly(self) -> None:
        manager = browser.BrowserWatchManager()
        self.addCleanup(manager.close)
        started = manager.start({**self.args, "max_entries": 20, "dialog_action": "dismiss"})
        watch_id = started["watch_id"]
        self.assertEqual(started["status"], "running")

        browser.evaluate(
            {
                **self.args,
                "script": "setTimeout(() => { console.log('persistent-watch'); alert('watch-dialog'); }, 100)",
            }
        )
        polled = manager.poll({"watch_id": watch_id, "after_seq": 0, "max_entries": 20, "wait_ms": 1500})
        kinds = {item["kind"] for item in polled["events"]}
        self.assertIn("console", kinds, polled)
        self.assertGreater(polled["next_after_seq"], 0)

        followup = manager.poll(
            {
                "watch_id": watch_id,
                "after_seq": polled["next_after_seq"],
                "max_entries": 20,
                "wait_ms": 1500,
            }
        )
        self.assertIn("dialog", {item["kind"] for item in followup["events"]}, followup)

        empty = manager.poll(
            {
                "watch_id": watch_id,
                "after_seq": followup["next_after_seq"],
                "max_entries": 20,
                "wait_ms": 50,
            }
        )
        self.assertEqual(empty["events"], [])
        stopped = manager.stop({"watch_id": watch_id})
        self.assertEqual(stopped["status"], "stopped")

    def _close_extra_pages(self) -> None:
        with browser._browser_connection(self.args) as connection:
            for popup in connection.contexts[0].pages[1:]:
                popup.close()


if __name__ == "__main__":
    unittest.main()
