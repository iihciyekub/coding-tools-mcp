"""Exercise DMG detach recovery with fake tools, without mounting disks or opening Finder."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).resolve().parents[1] / "apps/desktop-client/create-dmg.sh"
FAKE_TOOL = r'''
import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["DMG_TEST_LOG"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")
if tool == "mktemp":
    Path(os.environ["DMG_TEST_STAGE"]).mkdir()
    print(os.environ["DMG_TEST_STAGE"])
elif tool == "osascript":
    Path(os.environ["DMG_TEST_LAYOUT"]).write_text(sys.stdin.read())
elif tool == "hdiutil":
    if args[0] == "attach":
        print("/dev/disk999\tApple_partition_scheme")
    elif args[0] == "detach":
        if "-force" in args:
            sys.exit(int(os.environ["DMG_TEST_FORCE_FAIL"]))
        counter = Path(os.environ["DMG_TEST_COUNT"])
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        if count <= int(os.environ["DMG_TEST_BUSY_COUNT"]):
            print("hdiutil: couldn't eject disk999 - Resource busy", file=sys.stderr)
            sys.exit(16)
    elif args[0] == "convert":
        Path(args[args.index("-o") + 1]).write_bytes(b"test dmg")
'''


@unittest.skipIf(os.name == "nt", "DMG packager requires a POSIX shell")
class DesktopDmgTests(unittest.TestCase):
    def run_packager(self, *, busy_count: int, force_fail: bool = False):
        root = Path(self.enterContext(TemporaryDirectory()))
        tools = root / "bin"
        tools.mkdir()
        for name in ("hdiutil", "osascript", "ditto", "sync", "sleep", "mktemp"):
            path = tools / name
            path.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
            path.chmod(0o755)
        app = root / "App With Spaces.app"
        app.mkdir()
        output = root / "release" / "Coding-Tools-MCP-0.5.6-arm64.dmg"
        stage = root / "stage"
        log = root / "events.jsonl"
        layout = root / "layout.applescript"
        env = {
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
            "CODING_TOOLS_MCP_DESKTOP_VERSION": "0.5.6",
            "DMG_TEST_LOG": str(log),
            "DMG_TEST_STAGE": str(stage),
            "DMG_TEST_COUNT": str(root / "count"),
            "DMG_TEST_LAYOUT": str(layout),
            "DMG_TEST_BUSY_COUNT": str(busy_count),
            "DMG_TEST_FORCE_FAIL": str(int(force_fail)),
        }
        result = subprocess.run(["bash", str(SCRIPT), str(app), str(output)], env=env, capture_output=True, text=True, timeout=15)
        events = [json.loads(line) for line in log.read_text().splitlines()]
        return result, events, output, stage, layout.read_text()

    def test_normal_detach_closes_layout_and_converts(self):
        result, events, output, stage, layout = self.run_packager(busy_count=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.is_file())
        self.assertFalse(stage.exists())
        self.assertIn("delay 2\n    close", layout)
        self.assertEqual([event for event in events if event[:2] == ["hdiutil", "detach"]], [["hdiutil", "detach", "/dev/disk999"]])

    def test_busy_volume_recovers_with_bounded_normal_retries(self):
        result, events, output, stage, _ = self.run_packager(busy_count=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.exists())
        self.assertFalse(stage.exists())
        self.assertEqual([event for event in events if event[0] == "sleep"], [["sleep", "1"], ["sleep", "2"]])
        self.assertFalse(any("-force" in event for event in events))

    def test_force_detach_is_limited_to_the_owned_image_after_retries(self):
        result, events, output, stage, _ = self.run_packager(busy_count=99)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.exists())
        self.assertFalse(stage.exists())
        detaches = [event for event in events if event[:2] == ["hdiutil", "detach"]]
        self.assertEqual(len(detaches), 6)
        self.assertEqual(detaches[-1], ["hdiutil", "detach", "/dev/disk999", "-force"])

    def test_failed_detach_never_converts_or_deletes_mounted_staging_files(self):
        result, events, output, stage, _ = self.run_packager(busy_count=99, force_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertTrue(stage.is_dir())
        self.assertFalse(any(event[:2] == ["hdiutil", "convert"] for event in events))
        self.assertIn("refusing to convert", result.stderr)
