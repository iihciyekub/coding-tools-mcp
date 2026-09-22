"""Small, static selection guidance for model-visible coding primitives.

The runtime registry owns availability, schemas, permissions, and dispatch. This
module only adds concise selection hints to tool descriptions; it deliberately
does not implement a second discovery or routing layer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolGuide:
    category: str
    use_when: str


_GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "files": (
        ("read_file", "Read one known file or continue a bounded slice."),
        ("read_files", "Read several known files together under one total budget."),
        ("list_dir", "Inspect immediate directory structure; use list_files for glob-based lookup."),
        ("list_files", "Find paths by filename or glob; use search_text for file contents."),
        ("search_text", "Find literal text, an error message, or a regex; this is not semantic reference resolution."),
        ("apply_patch", "Make explicit source edits using unique context; inspect affected files before patching."),
        ("view_image", "Inspect a known workspace image when visual evidence is needed."),
    ),
    "commands": (
        ("exec_command", "Run a known command or test; retain command_id and use operation_id for recoverable retries."),
        ("get_command", "Recover status by command_id or operation_id before retrying uncertain execution."),
        ("list_commands", "Find existing command handles after reconnecting."),
        ("write_stdin", "Poll output or send input to a running command; do not relaunch it just to wait."),
        ("kill_command", "Stop a specific runtime-owned command when cancellation is intended."),
        ("read_output", "Page retained command output using its stream reference and absolute byte offsets."),
    ),
    "git": (
        ("git_status", "Inspect changed paths and repository state before edits or Git commands."),
        ("git_diff", "Inspect staged or unstaged changes before review or commit."),
        ("git_log", "Read bounded commit history."),
        ("git_show", "Read a known revision, historical file, or commit patch."),
        ("git_blame", "Find which commit last changed selected lines."),
    ),
    "code": (
        ("code_symbols", "Scan lightweight symbol definitions without starting a language server."),
        ("code_definition", "Find a definition; source positions prefer LSP/SourceKit semantics and fall back safely."),
        ("code_references", "Find references; source positions prefer LSP/SourceKit semantics and fall back safely."),
        ("code_diagnostics", "Read language-server diagnostics for one file; use exec_command for project-wide verification."),
    ),
    "project": (
        ("project_context", "List projects, inspect the session-bound project, or explicitly switch this MCP session."),
        ("workspace_overview", "Orient in an unfamiliar project, including bounded Apple/Xcode/Swift metadata."),
        ("project_instructions", "Read root and nested instructions applicable to the path you will edit."),
    ),
    "checks": (
        ("checks_discover", "Find existing project-defined checks; execute the chosen command with exec_command."),
    ),
    "access": (
        ("request_permissions", "Request the exact operation reported as needing permission; never broad implicit access."),
    ),
    "runtime": (
        ("server_info", "Inspect workspace, policy, and runtime metadata when configuration matters."),
        ("runtime_doctor", "Diagnose macOS/Xcode/Swift/SourceKit/toolchain environment problems."),
    ),
}


TOOL_GUIDES = {
    name: ToolGuide(category, use_when)
    for category, entries in _GROUPS.items()
    for name, use_when in entries
}


TOOL_USAGE_INSTRUCTIONS = (
    "Use the listed tools directly; there is no secondary tool-discovery workflow. "
    "Read several known files with read_files and continue commands using their command_id. "
    "Use exec_command for native macOS developer tools and ordinary Git writes instead of looking for wrapper tools. "
    "Use the model/client conversation for planning, review, and task state; this runtime only exposes coding primitives and bounded project helpers. "
    "Follow applicable project instructions before editing."
)
