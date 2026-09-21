#!/usr/bin/env python3
"""Probe the installed Codex app-server as a model-free local capability broker.

This is a read-only compatibility check. It creates an isolated temporary
CODEX_HOME, starts an ephemeral app-server thread, calls Computer Use list_apps
through node_repl, verifies the thread still contains zero model turns, and
prints a JSON result.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coding_tools_mcp.codex_bridge import probe_codex_computer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--codex", type=Path, default=None, help="explicit Codex executable")
    parser.add_argument("--codex-home", type=Path, default=None, help="source Codex home used only to discover node_repl paths")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--no-schema", action="store_true", help="skip app-server schema generation")
    args = parser.parse_args(argv)

    result = probe_codex_computer(
        args.workspace.resolve(),
        executable=args.codex.resolve() if args.codex else None,
        codex_home=args.codex_home.resolve() if args.codex_home else None,
        include_schema=not args.no_schema,
        timeout=args.timeout,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())

