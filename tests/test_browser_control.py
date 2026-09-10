from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from coding_tools_mcp import browser, chrome_bridge, chrome_native_host
from coding_tools_mcp.tool_results import render_tool_text


class BridgeRegressionTests(unittest.TestCase):
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
        cls.process = subprocess.Popen([
            os.environ["CODING_TOOLS_MCP_TEST_CHROME"], "--headless=new", "--disable-gpu",
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
            connection.contexts[0].pages[0].set_content('''
              <button name="action" onclick="window.clicked='first'">First</button>
              <button name="action" onclick="window.clicked='second'">Second</button>
              <label for="secret">Password</label><input id="secret" type="password" value="synthetic-secret">
              <label for="customer">Customer name</label><input id="customer">
              <div role="checkbox" aria-checked="true" tabindex="0">Agree</div>
            ''')

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
        self.assertEqual(snapshot["element_count"], 5)
        self.assertEqual(browser.inspect({**self.args, "selector": "button"})["matched"], 2)


if __name__ == "__main__":
    unittest.main()
