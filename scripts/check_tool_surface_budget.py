from __future__ import annotations

import json
import math
from typing import Any

from coding_tools_mcp.server import DEFAULT_HIDDEN_TOOLS, TOOL_REGISTRY, input_schemas

MAX_REGISTERED_TOOLS = 32
MAX_SINGLE_SCHEMA_BYTES = 2 * 1024
MAX_TOTAL_SCHEMA_BYTES = 16_000
TOP_COUNT = 10


def measure() -> dict[str, Any]:
    schemas = input_schemas()
    default_names = {
        name
        for name, spec in TOOL_REGISTRY.items()
        if name not in DEFAULT_HIDDEN_TOOLS
        and spec.gated_by not in {"enable_project_gateway", "enable_local_capabilities"}
    }
    rows: list[dict[str, Any]] = []
    for name in sorted(default_names):
        encoded = json.dumps(
            schemas[name],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        rows.append({"name": name, "schema_bytes": len(encoded)})
    rows.sort(key=lambda item: (-int(item["schema_bytes"]), str(item["name"])))
    total_bytes = sum(int(item["schema_bytes"]) for item in rows)
    gate_counts: dict[str, int] = {}
    for spec in TOOL_REGISTRY.values():
        key = spec.gated_by or "core"
        gate_counts[key] = gate_counts.get(key, 0) + 1
    return {
        "registered_tools": len(TOOL_REGISTRY),
        "default_exposed_tools": len(default_names),
        "hidden_default_tools": sorted(DEFAULT_HIDDEN_TOOLS),
        "gate_counts": dict(sorted(gate_counts.items())),
        "total_schema_bytes": total_bytes,
        "estimated_schema_tokens": math.ceil(total_bytes / 4),
        "largest_tools": rows[:TOP_COUNT],
        "largest_schema_bytes": int(rows[0]["schema_bytes"]) if rows else 0,
    }


def main() -> int:
    result = measure()
    failures: list[str] = []
    if int(result["default_exposed_tools"]) > MAX_REGISTERED_TOOLS:
        failures.append(
            f"default exposed tools {result['default_exposed_tools']} exceed budget {MAX_REGISTERED_TOOLS}"
        )
    if int(result["total_schema_bytes"]) > MAX_TOTAL_SCHEMA_BYTES:
        failures.append(
            f"total schema bytes {result['total_schema_bytes']} exceed budget {MAX_TOTAL_SCHEMA_BYTES}"
        )
    if int(result["largest_schema_bytes"]) > MAX_SINGLE_SCHEMA_BYTES:
        failures.append(
            f"largest schema {result['largest_schema_bytes']} bytes exceeds budget {MAX_SINGLE_SCHEMA_BYTES}"
        )

    print(
        f"tool surface: {result['default_exposed_tools']} default tools "
        f"({result['registered_tools']} registered), "
        f"{result['total_schema_bytes']} schema bytes, "
        f"~{result['estimated_schema_tokens']} tokens"
    )
    print(f"hidden by default: {result['hidden_default_tools']}")
    print(f"tool groups: {result['gate_counts']}")
    print("largest schemas:")
    for item in result["largest_tools"]:
        print(f"  {item['name']}: {item['schema_bytes']} bytes")
    if failures:
        print("tool surface budget FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        "tool surface budget PASS "
        f"(tools <= {MAX_REGISTERED_TOOLS}, total <= {MAX_TOTAL_SCHEMA_BYTES} bytes, "
        f"single <= {MAX_SINGLE_SCHEMA_BYTES} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
