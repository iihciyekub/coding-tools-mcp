from __future__ import annotations

import json
from typing import Any


MODEL_TEXT_SAFETY_LIMIT_BYTES = (2 * 1_048_576) + 65_536


def make_tool_result(
    tool_name: str,
    payload: dict[str, Any],
    *,
    is_error: bool,
    content: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an MCP result without mirroring structured JSON into model text.

    Normal model-facing text is sized by each tool's own per-call limits. A
    generous final safety ceiling remains as defense in depth for count-bounded
    tools whose individual entries (for example a source line) can be huge.
    """

    result_content = list(content or [])
    text = render_tool_text(tool_name, payload, is_error=is_error)
    if text:
        result_content.append(
            {"type": "text", "text": _bounded_model_text(text, tool_name)}
        )
    return {"content": result_content, "structuredContent": payload, "isError": is_error}


def render_tool_text(tool_name: str, payload: dict[str, Any], *, is_error: bool) -> str:
    if is_error or payload.get("ok") is False:
        return _render_error(payload)
    renderer = _RENDERERS.get(tool_name)
    if renderer is not None:
        return renderer(payload)
    summary = payload.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    status = payload.get("status")
    return f"{tool_name}: {status or 'completed'}."


def _render_error(payload: dict[str, Any]) -> str:
    raw_error = payload.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    code = str(error.get("code") or "TOOL_ERROR")
    message = str(error.get("message") or "Tool call failed.")
    lines = [f"{code}: {message}"]
    # Most clients feed the model this text and nothing else, so terminality
    # has to be stated here; leaving it in structuredContent alone is what
    # lets a model retry a call that can never succeed.
    retryable = error.get("retryable")
    category = error.get("category")
    facts: list[str] = []
    if isinstance(category, str) and category:
        facts.append(f"Category: {category}.")
    if isinstance(retryable, bool):
        facts.append(f"Retryable: {'yes' if retryable else 'no'}.")
        if not retryable:
            facts.append("Do not repeat this call unchanged.")
    if facts:
        lines.append(" ".join(facts))
    raw_details = error.get("details")
    details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
    retry_hint = details.get("retry_hint")
    if isinstance(retry_hint, str) and retry_hint:
        lines.append(f"Retry: {retry_hint}")
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, list):
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            suggestion = item.get("suggested_fix") or item.get("suggested_next_command")
            if isinstance(suggestion, str) and suggestion:
                lines.append(f"Suggested action: {suggestion}")
    return "\n".join(lines)


def _render_server_info(payload: dict[str, Any]) -> str:
    return (
        f"{payload.get('server', 'coding-tools-mcp')} {payload.get('version', 'unknown')}\n"
        f"Workspace: {payload.get('workspace', '.')}"
    )


def _render_exec_environment(payload: dict[str, Any]) -> str:
    raw_landlock = payload.get("landlock")
    landlock: dict[str, Any] = raw_landlock if isinstance(raw_landlock, dict) else {}
    state = "available" if landlock.get("available") else "unavailable"
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    suffix = "\n" + "\n".join(str(item) for item in warnings) if warnings else ""
    return f"Execution environment checked. Landlock: {state}.{suffix}"


def _render_read_file(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, str):
        return ""
    if not payload.get("truncated"):
        return content
    shown = (
        f"Showing lines {payload.get('start_line', '?')}-{payload.get('end_line', '?')}"
        f" of {payload.get('total_lines', '?')}"
    )
    next_start = payload.get("next_start_line")
    next_call = _render_next_action(payload)
    if not next_call and next_start:
        next_call = _render_tool_call(
            "read_file",
            {"path": payload.get("path", ""), "start_line": next_start},
        )
    if next_call:
        hint = f"; continue with {next_call}"
    else:
        hint = "; content truncated; raise max_bytes or request a narrower range"
    return f"[{shown}{hint}]\n{content}"


def _render_list(payload: dict[str, Any]) -> str:
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else payload.get("files")
    if not isinstance(entries, list) or not entries:
        return "No entries found."
    lines: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("name")
        kind = item.get("type")
        lines.append(f"{path}{' [' + str(kind) + ']' if kind else ''}")
    if payload.get("truncated"):
        lines.append("… results truncated; narrow the path/patterns or raise the entry limit.")
    return "\n".join(lines)


def _render_search(payload: dict[str, Any]) -> str:
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return "No matches found."
    lines: list[str] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        path = match.get("path", "")
        line = match.get("line", "")
        column = match.get("column", "")
        preview = match.get("preview", "")
        location = f"{path}:{line}" + (f":{column}" if column else "")
        lines.append(f"{location}: {preview}")
        before = match.get("before")
        after = match.get("after")
        if isinstance(before, list):
            lines.extend(f"  {value}" for value in before)
        if isinstance(after, list):
            lines.extend(f"  {value}" for value in after)
    if payload.get("truncated"):
        total = payload.get("total_matches")
        if isinstance(total, int):
            shown = sum(1 for match in matches if isinstance(match, dict))
            exact = payload.get("total_matches_exact", True)
            lines.append(
                f"… showing {shown} of {total}{'' if exact else '+'} matches;"
                " narrow the query/path or raise max_results."
            )
        else:
            lines.append("… results truncated; narrow the query/path or raise max_results.")
    return "\n".join(lines)


def _render_patch(payload: dict[str, Any]) -> str:
    prefix = "Patch validated" if payload.get("dry_run") else "Patch applied"
    files = payload.get("affected_files")
    count = len(files) if isinstance(files, list) else 0
    changes = f" (+{payload.get('additions', 0)} -{payload.get('removals', 0)})"
    summary = str(payload.get("summary") or "").strip()
    return f"{prefix} to {count} file{'s' if count != 1 else ''}{changes}." + (
        f"\n{summary}" if summary else ""
    )


def _render_exec(payload: dict[str, Any]) -> str:
    # Decision-critical fields lead every command result so the model never
    # has to infer success from output alone.
    header = [f"Status: {payload.get('status', 'unknown')}"]
    exit_code = payload.get("exit_code")
    if exit_code is not None:
        header.append(f"exit code {exit_code}")
    if payload.get("signal"):
        header.append(f"signal {payload['signal']}")
    if payload.get("timed_out"):
        header.append("timed out")
    elapsed_ms = payload.get("elapsed_ms")
    if isinstance(elapsed_ms, (int, float)):
        header.append(f"{int(elapsed_ms)} ms")
    sections: list[str] = [" | ".join(header)]
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    preview = payload.get("preview")
    if isinstance(stdout, str) and stdout:
        sections.append(stdout)
    if isinstance(stderr, str) and stderr:
        sections.append(f"stderr:\n{stderr}")
    if len(sections) == 1 and isinstance(preview, str) and preview:
        sections.append(preview)
    summary = payload.get("summary")
    if len(sections) == 1 and isinstance(summary, str) and summary:
        sections.append(summary)
    command_id = payload.get("command_id")
    if payload.get("status") == "running" and command_id:
        sections.append(
            f'Command still running; poll with write_stdin(command_id="{command_id}", chars="", yield_time_ms=10000).'
        )
    if payload.get("truncated"):
        continuations = _render_exec_continuations(payload)
        if continuations:
            sections.extend(continuations)
        else:
            sections.append("Output truncated; use read_output with the returned output_ref to read more.")
    return "\n".join(sections)


def _render_read_output(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, str):
        return ""
    next_offset = payload.get("next_offset")
    if next_offset is None:
        return content
    next_call = _render_next_action(payload)
    if not next_call:
        ref = payload.get("stream_output_ref") or payload.get("output_ref") or ""
        next_call = _render_tool_call("read_output", {"output_ref": ref, "offset": next_offset})
    return f"{content}\n[more: {next_call}]"


def _render_kill(payload: dict[str, Any]) -> str:
    signal_sent = payload.get("signal_sent")
    suffix = f" (signal {signal_sent})" if isinstance(signal_sent, str) and signal_sent else ""
    return f"Command {payload.get('command_id', '')}: {payload.get('status', 'completed')}{suffix}."


def _render_command_status(payload: dict[str, Any]) -> str:
    command_id = payload.get("command_id") or "pending"
    operation_id = payload.get("operation_id")
    parts = [f"Command {command_id}: {payload.get('status', 'unknown')}"]
    if operation_id:
        parts.append(f"operation_id={operation_id}")
    if payload.get("exit_code") is not None:
        parts.append(f"exit={payload['exit_code']}")
    if payload.get("timed_out"):
        parts.append("timed out")
    stdout_bytes = payload.get("stdout_total_bytes")
    stderr_bytes = payload.get("stderr_total_bytes")
    if isinstance(stdout_bytes, int) or isinstance(stderr_bytes, int):
        parts.append(f"stdout={int(stdout_bytes or 0)}B stderr={int(stderr_bytes or 0)}B")
    text = " | ".join(parts)
    if payload.get("deduplicated"):
        text += "\nNo new process was started; this is the existing operation result."
    next_call = _render_next_action(payload)
    if next_call:
        text += f"\nNext: {next_call}"
    return text


def _render_command_list(payload: dict[str, Any]) -> str:
    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands:
        return "No retained commands found."
    lines: list[str] = []
    for item in commands:
        if not isinstance(item, dict):
            continue
        command_id = item.get("command_id") or "pending"
        operation_id = item.get("operation_id")
        suffix = f" operation_id={operation_id}" if operation_id else ""
        exit_code = item.get("exit_code")
        exit_text = f" exit={exit_code}" if exit_code is not None else ""
        lines.append(f"{item.get('status', 'unknown')} {command_id}{suffix}{exit_text}")
    if payload.get("truncated"):
        lines.append("… command list truncated; raise max_results or filter by operation_id.")
    return "\n".join(lines)


def _render_code_results(payload: dict[str, Any]) -> str:
    items: list[Any] = []
    for key in ("symbols", "definitions", "references"):
        value = payload.get(key)
        if isinstance(value, list):
            items = value
            break
    if not items:
        subject = payload.get("symbol")
        text = f"No code results found{f' for {subject}' if subject else ''}."
    else:
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("qualified_name") or item.get("name")
            location = f"{item.get('path', '')}:{item.get('line', '')}"
            kind = item.get("kind")
            label = f"{name} " if name else ""
            if kind:
                label += f"[{kind}] "
            preview = str(item.get("preview") or "").strip()
            lines.append(f"{label}{location}{f': {preview}' if preview else ''}")
        text = "\n".join(lines) if lines else "No code results found."
    if payload.get("truncated"):
        reason = payload.get("truncated_by") or "configured limit"
        text += f"\n… results incomplete: truncated by {reason}."
        if reason == "max_files":
            text += " Raise max_files or narrow path before concluding the symbol is absent."
    return text


def _tab_summary(tab: Any) -> str:
    if not isinstance(tab, dict):
        return ""
    tab_id = tab.get("tab_id") or tab.get("target_id")
    identity = f" id={tab_id}" if tab_id else ""
    return (
        f"tab[{tab.get('index', '?')}]{identity} "
        f"{str(tab.get('title') or '').strip()} {str(tab.get('url') or '').strip()}"
    ).strip()


def _render_browser_status(payload: dict[str, Any]) -> str:
    return (
        f"Chrome connected: {bool(payload.get('connected'))}. "
        f"Tabs: {payload.get('tabs', 0)}; contexts: {payload.get('contexts', 0)}; "
        f"version: {payload.get('browser_version', 'unknown')}."
    )


def _render_browser_tabs(payload: dict[str, Any]) -> str:
    tabs = payload.get("tabs")
    if not isinstance(tabs, list) or not tabs:
        return "No inspectable browser tabs found."
    return "\n".join(_tab_summary(tab) for tab in tabs if isinstance(tab, dict))


def _render_browser_tab(payload: dict[str, Any]) -> str:
    return _tab_summary(payload.get("tab")) or "Browser action completed."


def _render_browser_snapshot(payload: dict[str, Any]) -> str:
    sections = [_render_browser_tab(payload)]
    text = payload.get("text")
    if isinstance(text, str) and text:
        sections.append(text)
    elements = payload.get("elements")
    if isinstance(elements, list) and elements:
        rendered: list[str] = []
        for item in elements:
            if not isinstance(item, dict):
                continue
            rendered.append(
                f"[{item.get('index', '?')}] {item.get('tag') or item.get('role') or 'element'} "
                f"{str(item.get('text') or '').strip()} selector={item.get('selector', '')}"
            )
        if rendered:
            sections.append("Interactive elements:\n" + "\n".join(rendered))
    if payload.get("text_truncated"):
        sections.append("… page text truncated; raise max_chars if more context is required.")
    if payload.get("elements_truncated"):
        sections.append("… interactive elements truncated; raise max_elements for more controls.")
    return "\n".join(section for section in sections if section)


def _bounded_json(value: Any, limit: int = 8000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + "… [truncated]"


def _render_browser_evaluate(payload: dict[str, Any]) -> str:
    tab = _render_browser_tab(payload)
    return f"{tab}\nJavaScript result: {_bounded_json(payload.get('result'))}"


def _render_browser_download(payload: dict[str, Any]) -> str:
    tab = _render_browser_tab(payload)
    filename = payload.get("filename") or "download"
    size = payload.get("bytes", 0)
    download_id = payload.get("download_id", "unknown")
    digest = payload.get("sha256", "")
    return f"{tab}\nDownloaded {filename} ({size} bytes); download_id={download_id}; sha256={digest}."


def _render_browser_watch(payload: dict[str, Any]) -> str:
    watch_id = payload.get("watch_id", "unknown")
    status = payload.get("status", "unknown")
    sections = [f"Browser watch {watch_id}: {status}."]
    tab = _tab_summary(payload.get("tab"))
    if tab:
        sections.append(tab)
    events = payload.get("events")
    if isinstance(events, list):
        if events:
            sections.extend(_bounded_json(item, 1200) for item in events[:200])
        else:
            sections.append("No new watch events.")
        if payload.get("truncated") or len(events) > 200:
            sections.append("… watch events truncated; continue from next_after_seq.")
    if payload.get("dropped_since_cursor"):
        sections.append(f"Dropped before cursor: {payload['dropped_since_cursor']} event(s).")
    return "\n".join(sections)


def _render_event_list(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("events"), list) and isinstance(payload.get("resources"), list):
        prefix = _render_browser_tab(payload)
        sections = [prefix] if prefix else []
        for key in ("events", "resources"):
            entries = payload[key]
            sections.append(f"{key}: {len(entries)}")
            sections.extend(_bounded_json(item, 1200) for item in entries[:200])
            if len(entries) > 200:
                sections.append(f"… {key} truncated.")
        return "\n".join(sections)
    items: list[Any] = []
    chosen_key = ""
    for key in ("entries", "messages", "requests", "events", "resources"):
        value = payload.get(key)
        if isinstance(value, list):
            items = value
            chosen_key = key
            break
    prefix = _render_browser_tab(payload)
    if not items:
        return f"{prefix}\nNo {chosen_key or 'events'} captured." if prefix else "No events captured."
    lines = [_bounded_json(item, 1200) for item in items[:200]]
    if payload.get("truncated") or len(items) > 200:
        lines.append("… event list truncated.")
    return (prefix + "\n" if prefix else "") + "\n".join(lines)


def _render_generic_items(payload: dict[str, Any]) -> str:
    for key in ("apps", "applications", "windows", "extensions", "tabs", "elements", "items"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        if not items:
            return f"No {key} found."
        lines: list[str] = []
        for item in items[:200]:
            if not isinstance(item, dict):
                lines.append(str(item))
                continue
            identity = (
                item.get("name")
                or item.get("title")
                or item.get("bundle_id")
                or item.get("id")
                or item.get("identifier")
                or item.get("url")
            )
            details = []
            for detail_key in ("pid", "bundle_id", "role", "id", "url", "enabled", "active"):
                value = item.get(detail_key)
                if value not in (None, "", identity):
                    details.append(f"{detail_key}={value}")
            lines.append(f"{identity or _bounded_json(item, 600)}{' | ' + ' '.join(details) if details else ''}")
        if payload.get("truncated") or len(items) > 200:
            lines.append("… results truncated.")
        return "\n".join(lines)
    for key in ("message", "result", "status", "path", "app", "extension_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return f"{key}: {_bounded_json(value)}"
    return "Operation completed."


def _render_git_status(payload: dict[str, Any]) -> str:
    if not payload.get("is_repo", True):
        return "Not a Git repository."
    branch = payload.get("branch") or "detached"
    raw_entries = payload.get("entries")
    entries = raw_entries if isinstance(raw_entries, list) else []
    lines = [f"## {branch}"]
    if payload.get("head"):
        lines.append(f"HEAD: {payload['head']}")
    if payload.get("index_fingerprint"):
        lines.append(f"Index fingerprint: {payload['index_fingerprint']}")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        lines.append(
            f"{entry.get('index_status', ' ')}{entry.get('worktree_status', ' ')} {entry.get('path', '')}"
        )
    if not entries:
        lines.append("Working tree clean.")
    if payload.get("truncated"):
        lines.append("… status entries truncated; narrow path or raise max_entries.")
    return "\n".join(lines)


def _render_git_workflow(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("summary") or "Git operation completed.")]
    for key in ("head", "index_fingerprint", "commit"):
        if payload.get(key):
            lines.append(f"{key}: {payload[key]}")
    for key in ("branches", "conflicts", "paths"):
        value = payload.get(key)
        if isinstance(value, list):
            lines.append(f"{key}: {_bounded_json(value, 16000)}")
    return "\n".join(lines)


def _render_lsp(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("summary") or "Language server operation completed.")]
    for key in ("backends", "definitions", "references", "diagnostics", "changes"):
        value = payload.get(key)
        if isinstance(value, list):
            lines.append(f"{key}: {_bounded_json(value, 24000)}")
    if payload.get("file_sha256"):
        lines.append(f"file_sha256: {payload['file_sha256']}")
    if payload.get("truncated"):
        lines.append("… LSP results truncated; narrow the request or raise its limit.")
    return "\n".join(lines)


def _render_review(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("summary") or "Review operation completed.")]
    lines.append(
        f"review_id={payload.get('review_id', '')} revision={payload.get('revision', '')} "
        f"status={payload.get('status', '')} stale={bool(payload.get('stale'))}"
    )
    findings = payload.get("findings")
    if isinstance(findings, list) and findings:
        lines.append(f"findings: {_bounded_json(findings, 24000)}")
    snapshot = payload.get("snapshot")
    if isinstance(snapshot, dict):
        lines.append(f"snapshot: {_bounded_json(snapshot, 32000)}")
    return "\n".join(lines)


def _render_key(payload: dict[str, Any], key: str, empty: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value else empty


def _render_git_diff(payload: dict[str, Any]) -> str:
    text = _render_key(payload, "diff", "No diff.")
    if payload.get("truncated"):
        text += "\n… diff truncated; raise max_bytes or diff specific paths."
    return text


def _render_git_show(payload: dict[str, Any]) -> str:
    text = _render_key(payload, "content", "No output.")
    if payload.get("truncated"):
        text += "\n… output truncated; raise max_bytes or narrow paths."
    return text


def _render_git_log(payload: dict[str, Any]) -> str:
    raw_commits = payload.get("commits")
    commits: list[Any] = raw_commits if isinstance(raw_commits, list) else []
    if not commits:
        return "No commits found."
    text = "\n".join(
        f"{item.get('short_hash', '')} {item.get('subject', '')}"
        for item in commits
        if isinstance(item, dict)
    )
    if payload.get("truncated"):
        next_call = _render_next_action(payload)
        text += "\n… more commits available"
        text += f"; continue with {next_call}." if next_call else "; raise max_count or use skip."
    return text


def _render_git_blame(payload: dict[str, Any]) -> str:
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        return "No blame lines found."
    rendered: list[str] = []
    for item in lines:
        if not isinstance(item, dict):
            continue
        rendered.append(
            f"{item.get('line', '')} {item.get('commit', '')} {item.get('content', '')}"
        )
    text = "\n".join(rendered)
    if payload.get("truncated"):
        next_call = _render_next_action(payload)
        text += "\n… blame lines truncated"
        text += f"; continue with {next_call}." if next_call else "; raise max_lines or advance start_line."
    return text


def _render_exec_continuations(payload: dict[str, Any]) -> list[str]:
    raw_refs = payload.get("output_refs")
    refs: dict[str, Any] = raw_refs if isinstance(raw_refs, dict) else {}
    raw_streams = payload.get("truncated_output_streams")
    streams = (
        [stream for stream in raw_streams if stream in {"stdout", "stderr"}]
        if isinstance(raw_streams, list)
        else []
    )
    if not streams:
        for stream in ("stdout", "stderr"):
            omitted = payload.get(f"{stream}_omitted_bytes")
            if payload.get(f"{stream}_truncated") or (
                isinstance(omitted, int) and omitted > 0
            ):
                streams.append(stream)
    if not streams and payload.get("preview_truncated"):
        streams.extend(stream for stream in ("stdout", "stderr") if refs.get(stream))

    continuations: list[str] = []
    seen_refs: set[str] = set()
    for stream in streams:
        ref = refs.get(stream)
        if not isinstance(ref, str) or not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        call = _render_tool_call("read_output", {"output_ref": ref, "offset": 0})
        continuations.append(f"{stream} output truncated; continue with {call}.")
    if continuations:
        return continuations

    ref = payload.get("output_ref")
    if isinstance(ref, str) and ref:
        call = _render_tool_call("read_output", {"output_ref": ref, "offset": 0})
        return [f"Output truncated; continue with {call}."]
    return []


def _render_next_action(payload: dict[str, Any]) -> str:
    raw_action = payload.get("next_action")
    if not isinstance(raw_action, dict):
        return ""
    tool = raw_action.get("tool")
    arguments = raw_action.get("arguments")
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        return ""
    return _render_tool_call(tool, arguments)


def _render_tool_call(tool: str, arguments: dict[str, Any]) -> str:
    rendered = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in arguments.items()
    )
    return f"{tool}({rendered})"


def _bounded_model_text(value: str, tool_name: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MODEL_TEXT_SAFETY_LIMIT_BYTES:
        return value
    suffix = (
        f"\n… {tool_name} model text reached the "
        f"{MODEL_TEXT_SAFETY_LIMIT_BYTES}-byte safety ceiling; retry with narrower paths or limits."
    )
    preview_budget = max(
        0,
        MODEL_TEXT_SAFETY_LIMIT_BYTES - len(suffix.encode("utf-8")),
    )
    preview = encoded[:preview_budget].decode("utf-8", errors="ignore")
    return preview + suffix


def _render_image(payload: dict[str, Any]) -> str:
    dimensions = ""
    if payload.get("width") and payload.get("height"):
        dimensions = f", {payload['width']}×{payload['height']}"
    return f"Image: {payload.get('path', '')} ({payload.get('mime_type', 'unknown')}{dimensions})"


def _render_workspace_overview(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("summary") or "Workspace overview.")]
    for key in ("manifests", "languages", "top_level", "entrypoints"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            lines.append(f"{key}: {_bounded_json(value, 12000)}")
    instructions = payload.get("instructions")
    if isinstance(instructions, dict):
        lines.append(f"instructions: {_bounded_json(instructions, 8000)}")
    if payload.get("truncated"):
        lines.append("… workspace scan truncated; raise max_files for broader coverage.")
    return "\n".join(lines)


def _render_repo_map(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("summary") or "Repository map.")]
    files = payload.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            lines.append(str(item.get("path", "")))
            symbols = item.get("symbols")
            if isinstance(symbols, list):
                for symbol in symbols:
                    if isinstance(symbol, dict):
                        lines.append(
                            f"  {symbol.get('kind', 'symbol')} {symbol.get('qualified_name') or symbol.get('name', '')}"
                            f" :{symbol.get('line', '?')} [{symbol.get('backend', 'unknown')}]"
                        )
    if payload.get("truncated"):
        lines.append("… repository map truncated; narrow query/path or raise limits.")
    return "\n".join(lines)


def _render_project_instructions(payload: dict[str, Any]) -> str:
    instructions = payload.get("instructions")
    if not isinstance(instructions, list) or not instructions:
        return "No applicable project instructions found."
    lines: list[str] = []
    for item in instructions:
        if not isinstance(item, dict):
            continue
        suffix = " [truncated]" if item.get("truncated") else ""
        lines.append(f"## {item.get('path', '')}{suffix}\n{item.get('content', '')}")
    return "\n\n".join(lines)


def _render_skills_list(payload: dict[str, Any]) -> str:
    skills = payload.get("skills")
    if not isinstance(skills, list) or not skills:
        return "No workspace skills found."
    return "\n".join(
        f"{item.get('name', '')}: {item.get('description', '')} ({item.get('path', '')})"
        for item in skills
        if isinstance(item, dict)
    )


def _render_skill(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    return str(content) if isinstance(content, str) else str(payload.get("summary") or "Skill read.")


def _render_checks(payload: dict[str, Any]) -> str:
    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        return "No checks discovered."
    return "\n".join(
        f"{item.get('id', '')}: {item.get('command', '')} (workdir={item.get('workdir', '.')}, source={item.get('source', '')})"
        for item in checks
        if isinstance(item, dict)
    )


def _render_check_result(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("summary") or "Check evidence loaded.")]
    lines.append(
        f"check_run_id={payload.get('check_run_id', '')} status={payload.get('status', 'unknown')} "
        f"exit={payload.get('exit_code')} stale={bool(payload.get('stale'))}"
    )
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("stdout", "stderr"):
            value = result.get(key)
            if isinstance(value, str) and value:
                lines.append(f"{key}:\n{value}")
    return "\n".join(lines)


def _render_task(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("tasks"), list):
        return "\n".join(_bounded_json(item, 3000) for item in payload["tasks"]) or "No tasks found."
    return _bounded_json(
        {key: payload.get(key) for key in ("task_id", "title", "objective", "status", "revision", "details")},
        12000,
    )


def _render_task_plan(payload: dict[str, Any]) -> str:
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return str(payload.get("summary") or "Task plan is empty.")
    lines = [str(payload.get("summary") or "Task plan.")]
    lines.extend(
        f"{item.get('step_id', '?')} [{item.get('status', 'pending')}] {item.get('title', '')}"
        for item in steps
        if isinstance(item, dict)
    )
    return "\n".join(lines)


def _render_task_events(payload: dict[str, Any]) -> str:
    events = payload.get("events")
    if not isinstance(events, list):
        return _bounded_json(payload, 12000)
    if not events:
        return "No task events found."
    return "\n".join(
        f"{item.get('event_type', 'event')}: {item.get('message', '')}"
        for item in events
        if isinstance(item, dict)
    )


def _render_task_context(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("summary") or "Task context loaded.")]
    task = payload.get("task")
    if isinstance(task, dict):
        lines.append(_bounded_json(task, 6000))
    for key in ("checks", "checkpoints", "events"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            lines.append(f"{key}: {_bounded_json(value, 12000)}")
    return "\n".join(lines)


def _render_checkpoint(payload: dict[str, Any]) -> str:
    for key in ("changes", "checkpoints", "files", "restored_files"):
        value = payload.get(key)
        if isinstance(value, list):
            return f"{payload.get('summary', 'Checkpoint operation completed.')}\n{_bounded_json(value, 20000)}"
    return str(payload.get("summary") or "Checkpoint operation completed.")


_RENDERERS = {
    "server_info": _render_server_info,
    "check_exec_environment": _render_exec_environment,
    "read_file": _render_read_file,
    "list_dir": _render_list,
    "list_files": _render_list,
    "search_text": _render_search,
    "apply_patch": _render_patch,
    "exec_command": _render_exec,
    "get_command": _render_command_status,
    "list_commands": _render_command_list,
    "write_stdin": _render_exec,
    "kill_command": _render_kill,
    "read_output": _render_read_output,
    "git_status": _render_git_status,
    "git_diff": _render_git_diff,
    "git_log": _render_git_log,
    "git_show": _render_git_show,
    "git_blame": _render_git_blame,
    "git_branch_list": _render_git_workflow,
    "git_branch_create": _render_git_workflow,
    "git_conflicts": _render_git_workflow,
    "git_stage": _render_git_workflow,
    "git_unstage": _render_git_workflow,
    "git_commit": _render_git_workflow,
    "git_worktree_list": _render_git_workflow,
    "git_worktree_create": _render_git_workflow,
    "git_worktree_remove": _render_git_workflow,
    "lsp_status": _render_lsp,
    "lsp_definition": _render_lsp,
    "lsp_references": _render_lsp,
    "lsp_diagnostics": _render_lsp,
    "lsp_rename_preview": _render_lsp,
    "review_prepare": _render_review,
    "review_record": _render_review,
    "review_get": _render_review,
    "request_permissions": lambda payload: f"Permission request: {payload.get('status', 'completed')}.",
    "workspace_overview": _render_workspace_overview,
    "repo_map": _render_repo_map,
    "project_instructions": _render_project_instructions,
    "skills_list": _render_skills_list,
    "skills_read": _render_skill,
    "checks_discover": _render_checks,
    "checks_run": _render_exec,
    "checks_result": _render_check_result,
    "task_create": _render_task,
    "task_get": _render_task,
    "task_list": _render_task,
    "task_update": _render_task,
    "task_event_add": _render_task_events,
    "task_events": _render_task_events,
    "task_context": _render_task_context,
    "task_plan_get": _render_task_plan,
    "task_plan_update": _render_task_plan,
    "checkpoint_create": _render_checkpoint,
    "checkpoint_list": _render_checkpoint,
    "checkpoint_diff": _render_checkpoint,
    "checkpoint_restore": _render_checkpoint,
    "view_image": _render_image,
    "browser_status": _render_browser_status,
    "browser_tabs": _render_browser_tabs,
    "browser_active_tab": _render_browser_tab,
    "browser_snapshot": _render_browser_snapshot,
    "browser_screenshot": _render_browser_tab,
    "browser_evaluate": _render_browser_evaluate,
    "browser_click": _render_browser_tab,
    "browser_type": _render_browser_tab,
    "browser_navigate": _render_browser_tab,
    "browser_back": _render_browser_tab,
    "browser_reload": _render_browser_tab,
    "browser_hover": _render_browser_tab,
    "browser_select": _render_browser_tab,
    "browser_press": _render_browser_tab,
    "browser_upload": _render_browser_tab,
    "browser_download": _render_browser_download,
    "browser_watch_start": _render_browser_watch,
    "browser_watch_poll": _render_browser_watch,
    "browser_watch_stop": _render_browser_watch,
    "browser_wait": _render_browser_tab,
    "browser_events": _render_event_list,
    "browser_console": _render_event_list,
    "browser_network": _render_event_list,
    "browser_inspect": _render_generic_items,
    "code_symbols": _render_code_results,
    "code_definition": _render_code_results,
    "code_references": _render_code_results,
    "chrome_extension_install": _render_generic_items,
    "chrome_extension_status": _render_generic_items,
    "chrome_extensions": _render_generic_items,
    "chrome_extension_tabs": _render_generic_items,
    "chrome_extension_execute": _render_generic_items,
    "chrome_extension_send": _render_generic_items,
    "app_accessibility": _render_generic_items,
    "app_list": _render_generic_items,
    "app_launch": _render_generic_items,
    "app_activate": _render_generic_items,
    "app_windows": _render_generic_items,
    "app_snapshot": _render_generic_items,
    "app_click": _render_generic_items,
    "app_type": _render_generic_items,
    "app_press": _render_generic_items,
    "app_menu": _render_generic_items,
    "app_screenshot": _render_generic_items,
}
