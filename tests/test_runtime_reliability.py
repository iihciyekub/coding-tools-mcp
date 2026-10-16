"""Selected upstream fixes adapted to the persistent Gateway's runtime contract."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from coding_tools_mcp.errors import JsonRpcError, ToolFailure
from coding_tools_mcp.gateway import BoundGatewayRuntime, HTTPProjectRuntime, ProjectDefinition
from coding_tools_mcp.processes import CommandRun
from coding_tools_mcp.protocol import RequestContext, _call_tool
from coding_tools_mcp.server import COMPLETED_COMMAND_TTL_SECONDS, Runtime, Workspace, json_response_payload
from coding_tools_mcp.tool_results import make_tool_result


class RuntimeReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.runtime = Runtime(self.root, state_root=self.root / "state", permission_mode="trusted")
        self.addCleanup(self.runtime.close)

    def completed_command(self, *, command_id: str = "finished", stdout: bytes = b"done\n", stderr: bytes = b"", limit: int = 524288) -> CommandRun:
        process = self.enterContext(subprocess.Popen([sys.executable, "-c", "pass"]))
        process.wait(timeout=5)
        command = CommandRun(command_id=command_id, process=process, buffer_limit=limit)
        command.append_stdout(stdout)
        command.append_stderr(stderr)
        command.refresh_status()
        self.runtime._remember_output_command(command)
        return command

    def test_invalid_falsey_arguments_are_rejected_at_both_entry_points(self) -> None:
        for value in ([], "", False, 0):
            for dispatch in (
                lambda: self.runtime.call_tool("server_info", value),
                lambda: _call_tool(self.runtime, {"name": "server_info", "arguments": value}, RequestContext()),
            ):
                with self.subTest(value=value), self.assertRaises(JsonRpcError) as error:
                    dispatch()
                self.assertEqual(error.exception.code, -32602)

    def test_missing_and_null_arguments_remain_compatible(self) -> None:
        for params in ({"name": "server_info"}, {"name": "server_info", "arguments": None}):
            self.assertFalse(_call_tool(self.runtime, params, RequestContext())["isError"])

    def test_gateway_rejects_malformed_arguments_before_routing(self) -> None:
        gateway = mock.Mock()
        bound = BoundGatewayRuntime(gateway, None)
        proxy = HTTPProjectRuntime(ProjectDefinition("p", "Project", self.root, endpoint="http://127.0.0.1:1/mcp"))
        with mock.patch.object(proxy, "_rpc") as rpc:
            for runtime in (bound, proxy):
                for value in ([], "", False, 0):
                    with self.subTest(runtime=type(runtime).__name__, value=value), self.assertRaises(JsonRpcError):
                        runtime.call_tool("project_context", value)
        rpc.assert_not_called()
        self.assertEqual(gateway.mock_calls, [])

    def test_surrogate_path_is_safe_in_text_and_lossless_in_json(self) -> None:
        path = "raw-\udcff-文件.txt"
        result = make_tool_result("list_files", {"ok": True, "files": [{"path": path}], "truncated": False}, is_error=False)
        text = result["content"][0]["text"]
        self.assertIn("\\udcff", text)
        self.assertIn("文件", text)
        text.encode("utf-8")
        decoded = json.loads(json_response_payload(result))
        self.assertEqual(decoded["structuredContent"]["files"][0]["path"], path)

    def test_git_ignore_preserves_filename_bytes(self) -> None:
        name = os.fsdecode(b"ignored-\xff.txt")
        with mock.patch("coding_tools_mcp.server.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"ignored-\xff.txt\0")) as run:
            self.assertEqual(Workspace(self.root).git_ignored_paths([name]), {name})
        self.assertEqual(run.call_args.kwargs["input"], b"ignored-\xff.txt\0")
        self.assertFalse(run.call_args.kwargs["text"])

    def test_git_decoding_does_not_depend_on_default_pipe_encoding(self) -> None:
        with mock.patch("subprocess._text_encoding", return_value="cp1252"):
            result = self.runtime._run_git_text(["git", "--version"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("git version", result.stdout)

    def test_invalid_git_stderr_is_structured_and_newlines_are_normalized(self) -> None:
        with mock.patch.object(self.runtime, "_run_git_bytes", return_value=subprocess.CompletedProcess([], 1, b"ok", b"bad\xff")):
            with self.assertRaises(ToolFailure) as error:
                self.runtime._run_git_text(["git", "status"])
            self.assertEqual(error.exception.code, "GIT_ERROR")
        with mock.patch.object(self.runtime, "_run_git_bytes", return_value=subprocess.CompletedProcess([], 0, b"one\r\ntwo\r", b"warn\r\n")):
            result = self.runtime._run_git_text(["git", "status"])
            self.assertEqual(result.stdout, "one\ntwo\n")
            self.assertEqual(result.stderr, "warn\n")

    def test_untruncated_exec_always_returns_recoverable_stream_refs(self) -> None:
        result = self.runtime.exec_command({"cmd": "echo hello", "yield_time_ms": 1000})
        command_id = result["command_id"]
        self.assertFalse(result["truncated"])
        self.assertEqual(result["output_refs"]["stderr"], f"command:{command_id}:stderr")
        page = self.runtime.read_output({"output_ref": result["output_refs"]["stdout"]})
        self.assertIn("hello", page["content"])

    def test_read_output_bare_id_reports_exit_and_keeps_stream_binding(self) -> None:
        command = self.completed_command(stderr=b"warning\n")
        result = self.runtime.call_tool("read_output", {"output_ref": command.command_id, "stream": "stderr"})
        page = result["structuredContent"]
        self.assertEqual(page["status"], "exited")
        self.assertEqual(page["exit_code"], 0)
        self.assertEqual(page["content"], "warning\n")
        self.assertIn("command exited", result["content"][0]["text"])
        with self.assertRaises(ToolFailure):
            self.runtime.read_output({"output_ref": "command:finished:stdout", "stream": "stderr"})

    def test_eviction_warning_survives_last_output_page(self) -> None:
        command = self.completed_command(stdout=b"abcdefghijklmnopqrstuvwxyz", limit=8)
        result = self.runtime.call_tool("read_output", {"output_ref": command.command_id, "offset": 20, "limit": 64})
        self.assertIsNone(result["structuredContent"]["next_offset"])
        self.assertIn("evicted", result["content"][0]["text"])

    def test_killing_exited_command_does_not_claim_a_signal(self) -> None:
        command = self.completed_command()
        with mock.patch("coding_tools_mcp.server.terminate_process_group") as terminate:
            result = self.runtime.kill_command({"command_id": command.command_id})
        terminate.assert_not_called()
        self.assertFalse(result["killed"])
        self.assertIsNone(result["signal_sent"])

    def test_closed_stdin_explains_restart_and_polling(self) -> None:
        result = self.runtime.exec_command({"cmd": f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(15)'", "yield_time_ms": 0})
        command_id = result["command_id"]
        try:
            error = self.runtime.call_tool("write_stdin", {"command_id": command_id, "chars": "hello"})["structuredContent"]["error"]
            self.assertEqual(error["details"]["reason"], "stdin_closed")
            self.assertIn("keep_stdin_open", error["message"])
        finally:
            self.runtime.kill_command({"command_id": command_id})

    def test_unobserved_completion_survives_ttl_then_expires_after_observation(self) -> None:
        command = self.completed_command()
        command.completed_at = time.time() - 2 * COMPLETED_COMMAND_TTL_SECONDS
        self.runtime._prune_commands()
        self.assertIn(command.command_id, self.runtime.output_commands)
        status = self.runtime.get_command({"command_id": command.command_id})
        self.assertGreater(status["expires_at"], time.time())
        observed = command.observed_at
        self.runtime.read_output({"output_ref": command.command_id})
        self.assertEqual(command.observed_at, observed)
        command.observed_at = time.time() - COMPLETED_COMMAND_TTL_SECONDS - 1
        self.runtime._prune_commands()
        self.assertNotIn(command.command_id, self.runtime.output_commands)

    def test_unobserved_output_is_still_bounded_by_capacity(self) -> None:
        self.completed_command(command_id="first")
        with mock.patch("coding_tools_mcp.server.MAX_RETAINED_OUTPUT_COMMANDS", 1):
            self.completed_command(command_id="second")
        self.assertEqual(list(self.runtime.output_commands), ["second"])

    def test_mixed_line_endings_and_max_lines_remain_bound_across_pages(self) -> None:
        (self.root / "mixed.txt").write_bytes(b"alpha\rbravo\r\ncharlie\ndelta\n")
        first = self.runtime.read_file({"path": "mixed.txt", "max_lines": 2, "max_bytes": 8})
        second = self.runtime.read_file(first["next_action"]["arguments"])
        self.assertEqual(first["content"] + second["content"], "alpha\rbravo\r\n")
        self.assertIsNone(second["next_start_line"])

    def test_oversized_final_requested_line_never_continues_outside_range(self) -> None:
        (self.root / "large.txt").write_text("abcdef\nnext\n", encoding="utf-8")
        result = self.runtime.read_file({"path": "large.txt", "end_line": 1, "max_bytes": 3})
        self.assertTrue(result["first_line_exceeds_limit"])
        self.assertIsNone(result["next_start_line"])
        self.assertNotIn("next_action", result)
