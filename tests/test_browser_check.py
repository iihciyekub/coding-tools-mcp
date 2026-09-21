from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from coding_tools_mcp.browser_check import (
    MAX_STEPS, bounded_text, local_origin, run_check, safe_message, safe_url, validate_scenario,
)


class BrowserCheckInputTests(unittest.TestCase):
    def test_only_explicit_loopback_http_targets(self) -> None:
        self.assertEqual(local_origin("http://127.0.0.1:3000/a"), ("http", "127.0.0.1", 3000))
        self.assertEqual(local_origin("http://[::1]:3000/"), ("http", "::1", 3000))
        self.assertEqual(local_origin("ws://localhost:3000/socket", websocket=True), ("http", "localhost", 3000))
        for value in ("https://example.com", "file:///tmp/test", "http://127.0.0.1.example.com", "http://user:pass@localhost", "javascript:alert(1)", "http://localhost:bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_origin(value)

    def test_scenario_is_small_and_not_a_script_executor(self) -> None:
        steps = [{"action": "click", "target": {"role": "button", "name": "Save"}}]
        self.assertEqual(validate_scenario({"steps": steps}), steps)
        for value in (
            [], {"script": "alert(1)"}, {"steps": [steps[0]] * (MAX_STEPS + 1)},
            {"steps": [{"action": "evaluate", "script": "alert(1)"}]},
            {"steps": [{"action": "click", "target": {"selector": "button"}}]},
            {"steps": [{"action": "click", "target": {"label": "Save"}, "timeout_ms": True}]},
            {"steps": [{"action": "press", "target": {"label": "Name"}, "value": "Control+L"}]},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_scenario(value)

    def test_logs_strip_url_secrets_and_bound_text(self) -> None:
        self.assertEqual(safe_url("http://user:secret@localhost:3000/api?token=secret#secret"), "http://localhost:3000/api")
        message = safe_message("failed http://localhost/api?token=private password=private")
        self.assertNotIn("private", message)
        self.assertIn("[redacted]", message)
        self.assertLess(len(bounded_text("abc" * 1000, 20)), 40)

    def test_invalid_check_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "not-created"
            with self.assertRaises(ValueError):
                asyncio.run(run_check("https://example.com", {}, output))
            self.assertFalse(output.exists())


@unittest.skipUnless(os.environ.get("CODING_TOOLS_MCP_BROWSER_SMOKE") == "1", "opt-in real Chromium fixture")
class BrowserCheckChromiumTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.external_hits: list[str] = []

        class External(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                cls.external_hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"outside")

            def log_message(self, *_args: object) -> None:
                pass

        cls.external = ThreadingHTTPServer(("127.0.0.1", 0), External)
        external_origin = f"http://127.0.0.1:{cls.external.server_port}"

        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/socket" and self.headers.get("Upgrade", "").lower() == "websocket":
                    # A single controlled text frame is enough to exercise real
                    # Playwright forwarding; this is not a general WS server.
                    key = self.headers.get("Sec-WebSocket-Key", "")
                    accept = base64.b64encode(hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                    ).digest()).decode()
                    self.protocol_version = "HTTP/1.1"
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.wfile.write(b"\x81\x07fixture")
                    self.wfile.flush()
                    return
                if self.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", external_origin + "/redirected?token=private")
                    self.end_headers()
                    return
                if self.path in {"/save", "/fail"}:
                    self.send_response(200 if self.path == "/save" else 500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                body = """<!doctype html><html><head><title>CTM fixture</title></head><body>
<label>Name<input id="name"></label><button id="save">Save</button><p id="status">Ready</p>
<script>document.querySelector('#save').onclick=async()=>{
const response=await fetch('/save');if(response.ok)document.querySelector('#status').textContent='Saved';};</script>
</body></html>"""
                if self.path == "/broken":
                    body = body.replace("fetch('/save')", "fetch('/fail')")
                if self.path == "/external":
                    body = body.replace("</body>", f"<script>fetch('{external_origin}/probe?token=private').catch(()=>{{document.querySelector('#status').textContent='Blocked';}});</script></body>")
                if self.path == "/websocket":
                    body = body.replace("</body>", "<script>const socket=new WebSocket('ws://'+location.host+'/socket');socket.onmessage=()=>{document.querySelector('#status').textContent='Socket ready';};</script></body>")
                if self.path == "/external-websocket":
                    external_socket = external_origin.replace("http://", "ws://") + "/socket"
                    body = body.replace("</body>", f"<script>const socket=new WebSocket('{external_socket}');socket.onclose=()=>{{document.querySelector('#status').textContent='Socket blocked';}};</script></body>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *_args: object) -> None:
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (cls.external, cls.server)]
        for thread in cls.threads:
            thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        for server in (cls.external, cls.server):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join(timeout=2)

    async def check(self, route: str, steps: list[dict], output: Path, **options: object) -> dict:
        return await run_check(f"http://127.0.0.1:{self.server.server_port}{route}", {"steps": steps}, output, **options)

    async def test_real_success_and_independent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            steps = [
                {"action": "fill", "target": {"label": "Name"}, "value": "fixture-private-input"},
                {"action": "click", "target": {"role": "button", "name": "Save"}},
                {"action": "wait_text", "text": "Saved"},
            ]
            first = await self.check("/", steps, output)
            self.assertTrue(first["ok"], first)
            self.assertEqual([item["status"] for item in first["steps"]], ["completed"] * 3)
            self.assertTrue(Path(first["evidence"]["screenshot"]).read_bytes().startswith(b"\x89PNG"))
            self.assertIn("Saved", Path(first["evidence"]["snapshot"]).read_text())
            self.assertNotIn("fixture-private-input", Path(first["evidence"]["report"]).read_text())
            second = await self.check("/", [{"action": "wait_text", "text": "Ready"}], output)
            self.assertTrue(second["ok"], second)
            self.assertNotEqual(first["evidence"]["report"], second["evidence"]["report"])

    async def test_real_http_failure_keeps_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = await self.check("/broken", [
                {"action": "click", "target": {"role": "button", "name": "Save"}},
                {"action": "wait_text", "text": "Saved", "timeout_ms": 300},
            ], Path(temp))
            self.assertFalse(result["ok"])
            self.assertTrue(any(item["status"] == 500 for item in result["events"]["http_errors"]))
            self.assertEqual(result["steps"][-1]["status"], "failed")
            self.assertTrue(Path(result["evidence"]["screenshot"]).exists())

    async def test_other_local_project_origin_is_blocked(self) -> None:
        before = len(self.external_hits)
        with tempfile.TemporaryDirectory() as temp:
            result = await self.check("/external", [{"action": "wait_text", "text": "Blocked"}], Path(temp))
            self.assertFalse(result["ok"])
            self.assertTrue(result["events"]["blocked_requests"])
            self.assertNotIn("private", json.dumps(result["events"]))
        self.assertEqual(len(self.external_hits), before)

    async def test_redirect_cannot_escape_target_origin(self) -> None:
        before = len(self.external_hits)
        with tempfile.TemporaryDirectory() as temp:
            result = await self.check("/redirect", [], Path(temp))
            self.assertFalse(result["ok"], result)
        self.assertEqual(len(self.external_hits), before)

    async def test_execution_deadline_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = await self.check("/", [{"action": "wait_text", "text": "Never", "timeout_ms": 10000}], Path(temp), timeout_ms=1000)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "BROWSER_DEADLINE")

    async def test_same_origin_websocket_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = await self.check("/websocket", [{"action": "wait_text", "text": "Socket ready"}], Path(temp))
            self.assertTrue(result["ok"], result)

    async def test_other_project_websocket_is_blocked(self) -> None:
        before = len(self.external_hits)
        with tempfile.TemporaryDirectory() as temp:
            result = await self.check("/external-websocket", [{"action": "wait_text", "text": "Socket blocked"}], Path(temp))
            self.assertFalse(result["ok"], result)
            self.assertTrue(any(event.get("transport") == "websocket" for event in result["events"]["blocked_requests"]))
        self.assertEqual(len(self.external_hits), before)


if __name__ == "__main__":
    unittest.main()
