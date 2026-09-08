from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO


HOST_NAME = "com.codingtoolsmcp.chrome_bridge"


def bridge_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Coding Tools MCP"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Coding Tools MCP"
    return Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".cache")) / "coding-tools-mcp"


def socket_path() -> Path:
    return bridge_dir() / "chrome-native.sock"


def _read_native_message(stream: BinaryIO) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise EOFError("Incomplete native-messaging message header")
    length = struct.unpack("<I", header)[0]
    if length > 16 * 1024 * 1024:
        raise ValueError("Native-messaging message exceeds 16 MiB")
    body = stream.read(length)
    if len(body) != length:
        raise EOFError("Incomplete native-messaging message body")
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Native-messaging message must be a JSON object")
    return value


def _write_native_message(stream: BinaryIO, payload: dict[str, Any], lock: threading.Lock) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packet = struct.pack("<I", len(body)) + body
    with lock:
        stream.write(packet)
        stream.flush()


class NativeBridge:
    def __init__(self) -> None:
        self._pending: dict[str, socket.socket] = {}
        self._pending_lock = threading.Lock()
        self._stdout_lock = threading.Lock()
        self._server: socket.socket | None = None
        self._stopped = threading.Event()

    def _send_to_extension(self, payload: dict[str, Any]) -> None:
        _write_native_message(sys.stdout.buffer, payload, self._stdout_lock)

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            file = conn.makefile("r", encoding="utf-8", newline="\n")
            line = file.readline(4 * 1024 * 1024)
            if not line:
                return
            request = json.loads(line)
            request_id = str(request.get("id") or "")
            if not request_id:
                conn.sendall(b'{"ok":false,"error":"missing request id"}\n')
                return
            with self._pending_lock:
                self._pending[request_id] = conn
            self._send_to_extension(request)
        except Exception as exc:  # noqa: BLE001
            try:
                conn.sendall(
                    (json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":")) + "\n").encode("utf-8")
                )
            except OSError:
                pass

    def _serve_clients(self) -> None:
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(16)
        server.settimeout(0.5)
        while not self._stopped.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            thread.start()

    def _dispatch_extension_message(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("id") or "")
        if not request_id:
            return
        with self._pending_lock:
            conn = self._pending.pop(request_id, None)
        if conn is None:
            return
        try:
            conn.sendall((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def run(self) -> int:
        thread = threading.Thread(target=self._serve_clients, daemon=True)
        thread.start()
        try:
            while True:
                message = _read_native_message(sys.stdin.buffer)
                if message is None:
                    return 0
                self._dispatch_extension_message(message)
        except (EOFError, BrokenPipeError):
            return 0
        finally:
            self._stopped.set()
            if self._server is not None:
                try:
                    self._server.close()
                except OSError:
                    pass
            try:
                socket_path().unlink(missing_ok=True)
            except OSError:
                pass


def main() -> int:
    return NativeBridge().run()


if __name__ == "__main__":
    raise SystemExit(main())
