#!/usr/bin/env python3
"""Compare project routing latency when discovery becomes due, using a Git baseline."""
from __future__ import annotations

import argparse
import ast
import json
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import coding_tools_mcp.gateway as gateway
from coding_tools_mcp.gateway import ProjectDefinition, ProjectRegistry


def summarize(samples: list[float]) -> dict[str, Any]:
    return {
        "samples": len(samples),
        "p50_ms": round(statistics.median(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def run(baseline_ref: str, directories: int, iterations: int) -> dict[str, Any]:
    source = subprocess.check_output(
        ["git", "show", f"{baseline_ref}:coding_tools_mcp/gateway.py"], text=True,
    )
    registry_node = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "ProjectRegistry"
    )
    # Evaluate only the historical registry class against the unchanged shared
    # definition types. Neither implementation starts project runtimes here.
    namespace = dict(vars(gateway))
    exec(compile(ast.Module(body=[registry_node], type_ignores=[]), "<baseline-registry>", "exec"), namespace)
    results = {}
    with tempfile.TemporaryDirectory(prefix="coding-gateway-benchmark-") as tmp:
        root = Path(tmp).resolve()
        for index in range(directories):
            (root / f"project-{index:04}").mkdir()
        definition = ProjectDefinition("workspace", "workspace", root)
        for name, kind in (("baseline", namespace["ProjectRegistry"]), ("updated", ProjectRegistry)):
            registry = kind(None, None, runtime_factory=lambda _: None, workspace_roots=(definition,))
            samples = []
            try:
                for _ in range(iterations):
                    registry._last_scan = 0
                    start = time.perf_counter()
                    selected, _ = registry.resolve_selector(str(root / "project-0000"))
                    samples.append((time.perf_counter() - start) * 1000)
                    assert selected is not None and selected.path == root / "project-0000"
                    worker = getattr(registry, "_scan_thread", None)
                    if worker is not None:
                        worker.join(5)
                        assert not worker.is_alive()
                results[name] = summarize(samples)
            finally:
                registry.close()
    return {
        "baseline_commit": subprocess.check_output(["git", "rev-parse", baseline_ref], text=True).strip(),
        "platform": platform.platform(),
        "fixture_directories": directories,
        "metric": "project selector latency when discovery is due",
        "results": results,
        "notes": [
            "Local synthetic warm-filesystem microbenchmark; excludes network, model latency, installed app and Cloudflare.",
            "Background discovery still runs; request routing no longer waits for it. Force/startup scans remain synchronous.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--directories", type=int, default=1200)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args()
    if args.directories < 1 or args.iterations < 1:
        parser.error("directories and iterations must be positive")
    report = run(args.baseline_ref, args.directories, args.iterations)
    rendered = json.dumps(report, indent=2) + "\n"
    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
