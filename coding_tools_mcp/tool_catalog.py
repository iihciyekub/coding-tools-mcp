"""Navigation and selection guidance, shared by tool definitions and discovery.

This catalog describes tools; the runtime registry remains authoritative for
availability, schemas, permissions, and dispatch. Nothing here enables a tool.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCategory:
    title: str
    use_when: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ToolGuide:
    category: str
    use_when: str
    aliases: tuple[str, ...]


CATEGORIES: dict[str, ToolCategory] = {
    "discovery": ToolCategory("Tool discovery / 工具目录", "Find an unfamiliar capability; skip discovery for tools whose schema you already have.", ("工具", "目录", "发现", "catalog")),
    "files": ToolCategory("Files and edits / 文件与修改", "Locate, read, search, and patch source files; batch reads when several paths are known.", ("文件", "读取", "修改", "搜索", "edit")),
    "commands": ToolCategory("Commands and output / 命令与输出", "Run known commands and continue existing command handles instead of launching duplicates.", ("命令", "终端", "输出", "进程", "shell", "terminal")),
    "git": ToolCategory("Git and worktrees / 版本与工作树", "Inspect changes or perform explicit Git operations; refresh state fingerprints before guarded writes.", ("版本", "分支", "提交", "工作树", "commit", "branch")),
    "code": ToolCategory("Code navigation / 代码定位", "Use lightweight symbol lookup first; choose LSP for semantic identity or position-based analysis.", ("代码", "符号", "定义", "引用", "语义", "lsp", "symbol")),
    "project": ToolCategory("Project context / 项目上下文", "Read applicable instructions and obtain a bounded map of an unfamiliar repository.", ("项目", "仓库", "规则", "技能", "repository", "instructions", "skills")),
    "checks": ToolCategory("Checks and evidence / 检查与证据", "Discover unfamiliar verification commands or retain check evidence; run an already-known command directly when persistence is unnecessary.", ("测试", "检查", "验证", "test", "verify")),
    "tasks": ToolCategory("Persistent tasks / 持久化任务", "Save or resume work across sessions; small one-shot edits do not need task records or plans.", ("任务", "进度", "计划", "接续", "task", "plan", "resume")),
    "review": ToolCategory("Review records / 审查记录", "Prepare and preserve review evidence and findings; these tools do not perform an AI review themselves.", ("审查", "评审", "审阅", "review")),
    "recovery": ToolCategory("Checkpoints / 检查点与恢复", "Snapshot selected text files when rollback is needed; preview changes before restoring.", ("检查点", "恢复", "回滚", "快照", "checkpoint", "restore", "rollback")),
    "access": ToolCategory("Permissions / 权限与审批", "Handle a reported permission requirement and inspect its approval; discovery never grants access.", ("权限", "审批", "授权", "permission", "approval")),
    "runtime": ToolCategory("Runtime diagnostics / 运行环境", "Inspect server settings or diagnose environment problems; these checks are not required before every edit.", ("环境", "诊断", "运行时", "配置", "environment", "doctor", "hooks")),
}


# Explicit membership avoids misclassifying similarly named tools (for example,
# shell_snapshot versus checkpoint_create, or code_symbols versus repo_map).
_GROUPS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "discovery": (
        ("tool_search", "Browse categories first only when the required capability is unknown.", ("工具目录", "查找工具", "发现工具", "browse tools")),
        ("tool_invoke", "Use the discovered schema for a deferred tool; call directly exposed tools by their own names.", ("调用工具", "高级工具", "invoke tool")),
    ),
    "files": (
        ("read_file", "Read one known file or continue a bounded slice.", ("读取文件", "查看文件", "read file")),
        ("read_files", "Read several known files together under one total budget.", ("批量读取", "读取多个文件", "batch read", "multiple files")),
        ("list_dir", "Inspect immediate directory structure; use list_files for glob-based file lookup.", ("目录结构", "列出目录", "list directory")),
        ("list_files", "Find paths by filename or glob; use search_text for file contents.", ("查找文件", "文件名", "文件列表", "find files", "glob")),
        ("search_text", "Find literal text, an error message, or a regex; this is not semantic reference resolution.", ("搜索文本", "搜索代码", "查找字符串", "正则", "grep", "search text")),
        ("apply_patch", "Make explicit source edits using unique context; inspect affected files before patching.", ("修改代码", "修改文件", "补丁", "编辑", "edit source", "patch")),
        ("view_image", "Inspect a known workspace image when visual evidence is needed.", ("查看图片", "图片", "image")),
    ),
    "commands": (
        ("exec_command", "Run a known command or test; retain command_id and use operation_id for recoverable retries.", ("执行命令", "运行命令", "执行测试命令", "run command", "shell")),
        ("get_command", "Recover status by command_id or operation_id before retrying an uncertain execution.", ("命令状态", "恢复命令", "查询命令", "recover command", "command status")),
        ("list_commands", "Find an existing command handle after reconnecting.", ("命令列表", "找回命令", "正在运行", "list commands")),
        ("write_stdin", "Poll output or send input to the returned command_id; do not restart the command to wait for it.", ("等待命令", "轮询", "标准输入", "继续命令", "poll", "stdin")),
        ("kill_command", "Stop a specific runtime-owned command when cancellation is intended.", ("停止命令", "终止进程", "取消命令", "cancel command")),
        ("read_output", "Page retained command output using its stream reference and absolute byte offsets.", ("读取输出", "命令日志", "历史输出", "read output")),
    ),
    "git": (
        ("git_status", "Inspect changed paths and obtain current HEAD/index fingerprints before guarded Git writes.", ("改动状态", "暂存状态", "git状态", "git status")),
        ("git_diff", "Inspect staged or unstaged changes before review or commit.", ("查看差异", "代码差异", "diff")),
        ("git_log", "Find commits in bounded history.", ("提交历史", "版本历史", "history")),
        ("git_show", "Read a known revision, historical file, or commit patch.", ("查看提交", "历史文件", "show commit")),
        ("git_blame", "Find which commit last changed selected lines.", ("追溯代码", "行归属", "blame")),
        ("git_branch_list", "Inspect local branches before choosing a branch operation.", ("分支列表", "查看分支", "list branches")),
        ("git_branch_create", "Create a branch with fresh expected_head and expected_index_fingerprint from git_status.", ("创建分支", "新建分支", "create branch")),
        ("git_conflicts", "Inspect unmerged paths and index stages before resolving conflicts.", ("合并冲突", "查看冲突", "merge conflicts")),
        ("git_stage", "Stage explicit reviewed paths with fresh Git state fingerprints.", ("暂存文件", "暂存修改", "stage files", "git add")),
        ("git_unstage", "Remove explicit paths from the index with fresh Git state fingerprints.", ("取消暂存", "撤销暂存", "unstage")),
        ("git_commit", "Commit exactly the declared staged paths; refresh Git state after staging.", ("提交代码", "提交修改", "创建提交", "commit changes")),
        ("git_worktree_list", "Inspect worktrees and identify entries managed by this runtime.", ("工作树列表", "查看工作树", "list worktrees")),
        ("git_worktree_create", "Create an isolated managed worktree using fresh Git state fingerprints.", ("创建工作树", "隔离开发", "create worktree")),
        ("git_worktree_remove", "Remove a clean runtime-managed worktree after finishing its work; its branch is preserved.", ("删除工作树", "清理工作树", "remove worktree")),
    ),
    "code": (
        ("code_symbols", "Scan lightweight symbol definitions without starting a language server.", ("符号列表", "函数列表", "代码结构", "list symbols")),
        ("code_definition", "Find candidate definitions by symbol name; use lsp_definition for semantic resolution at a position.", ("查找定义", "函数定义", "find definition")),
        ("code_references", "Find identifier-name occurrences; use lsp_references when same-name symbols must be distinguished.", ("查找引用", "标识符引用", "find references")),
        ("lsp_status", "Check language-server availability when semantic tools are needed or fail.", ("语言服务器", "lsp状态", "language server status")),
        ("lsp_definition", "Resolve the semantic definition at a source position using an available language server.", ("语义定义", "精确定义", "semantic definition")),
        ("lsp_references", "Resolve references to the same semantic symbol at a source position.", ("语义引用", "精确引用", "semantic references")),
        ("lsp_diagnostics", "Read language-server diagnostics for a file; use checks for project verification.", ("代码诊断", "类型错误", "类型检查", "diagnostics", "type errors")),
        ("lsp_rename_preview", "Preview semantic rename edits and source hashes; this does not write files.", ("重命名符号", "语义重命名", "重命名预览", "rename symbol")),
    ),
    "project": (
        ("workspace_overview", "Orient in an unfamiliar project before choosing narrower paths.", ("项目概览", "工作区概览", "了解项目", "workspace overview")),
        ("repo_map", "Locate relevant areas in a bounded file/symbol map; narrow the query if coverage is incomplete.", ("仓库地图", "项目地图", "仓库结构", "repo map")),
        ("project_instructions", "Read root and nested instructions applicable to the path you will edit.", ("项目规则", "项目指令", "嵌套规则", "agents.md", "project instructions")),
        ("skills_list", "List workspace skill summaries before choosing one to read.", ("技能列表", "发现技能", "list skills")),
        ("skills_read", "Read one selected workspace skill; reading it does not execute its scripts.", ("读取技能", "查看技能", "read skill")),
        ("agent_environment", "Discover metadata for installed local agent runtimes and their reusable skills/capabilities without reading secrets.", ("本机agent", "本地agent", "codex技能", "agent environment", "local agents", "installed agents")),
    ),
    "checks": (
        ("checks_discover", "Find project-defined test, lint, or build commands when the verification entry point is unknown.", ("发现测试", "测试入口", "检查入口", "discover tests", "find checks")),
        ("checks_run", "Run a discovered check when persisted evidence is useful; otherwise exec_command can run a known command.", ("运行测试", "执行检查", "运行检查", "run tests", "run checks")),
        ("checks_result", "Read persisted verification evidence and check whether later edits made it stale.", ("测试结果", "检查结果", "验证证据", "test results")),
    ),
    "tasks": (
        ("task_create", "Create durable task state only when tracking or cross-session recovery is useful.", ("创建任务", "新建任务", "create task")),
        ("task_get", "Read one known task record; use task_context to resume its surrounding work.", ("任务详情", "查看任务", "get task")),
        ("task_list", "Find a task ID or inspect task status before resuming.", ("任务列表", "查找任务", "list tasks")),
        ("task_update", "Change a task record using its current revision.", ("更新任务", "任务状态", "update task")),
        ("task_event_add", "Persist a useful decision, progress update, or evidence link.", ("记录进度", "记录决策", "添加事件", "record progress")),
        ("task_events", "Read task history when its sequence of decisions matters.", ("任务历史", "任务事件", "task history")),
        ("task_context", "Resume a known task with recent events, checks, and checkpoints in one read.", ("恢复任务", "继续任务", "接续任务", "任务上下文", "resume task", "restore task")),
        ("task_plan_get", "Read the persisted plan and revision for a tracked task.", ("查看计划", "读取计划", "get plan")),
        ("task_plan_update", "Replace a tracked task's ordered plan with current revision checking.", ("更新计划", "修改计划", "update plan")),
    ),
    "review": (
        ("review_prepare", "Capture a review snapshot when durable review evidence is needed.", ("准备审查", "准备评审", "prepare review")),
        ("review_record", "Save findings after analysis; recording findings does not generate them.", ("记录审查", "保存评审", "记录问题", "record review")),
        ("review_get", "Read saved findings and check whether the reviewed code has changed.", ("查看审查", "审查结果", "评审结果", "get review")),
    ),
    "recovery": (
        ("checkpoint_create", "Snapshot an explicit bounded set of text files when rollback is needed.", ("创建检查点", "保存快照", "create checkpoint")),
        ("checkpoint_list", "Find a saved checkpoint before previewing its differences.", ("检查点列表", "快照列表", "list checkpoints")),
        ("checkpoint_diff", "Preview a checkpoint against current files and obtain a fresh restore token.", ("预览恢复", "检查点差异", "恢复预览", "preview restore")),
        ("checkpoint_restore", "Apply an intended restore using the fresh token from checkpoint_diff; re-preview if files changed.", ("恢复检查点", "回滚文件", "恢复文件", "restore checkpoint")),
        ("context_checkpoint", "Persist or resume compact cross-agent task context without calling another model; get reports whether runtime state has gone stale.", ("上下文检查点", "会话压缩", "任务续接", "context checkpoint", "session compact", "resume context")),
    ),
    "access": (
        ("request_permissions", "Request the exact operation reported as needing permission; wait for its decision before retrying.", ("请求权限", "申请授权", "request permission")),
        ("approval_get", "Read one approval decision; this tool cannot grant it.", ("查看审批", "审批状态", "get approval")),
        ("approval_list", "Find pending or completed approval records; this tool cannot grant them.", ("审批列表", "待审批", "list approvals")),
    ),
    "runtime": (
        ("server_info", "Inspect workspace, policy, and runtime metadata when configuration matters.", ("服务器信息", "工作区配置", "server info")),
        ("check_exec_environment", "Inspect lightweight execution-policy status; use runtime_doctor for actionable toolchain diagnosis.", ("执行环境", "沙箱状态", "exec environment")),
        ("runtime_doctor", "Diagnose missing commands, aliases, or environment issues instead of repeated trial-and-error execution.", ("环境诊断", "环境检查", "缺少命令", "runtime doctor")),
        ("hooks_status", "Inspect enabled hooks when hook behavior or a blocking hook needs explanation.", ("钩子状态", "hook配置", "hooks status")),
        ("shell_snapshot", "Capture a reusable command environment or explicitly refresh it after environment changes.", ("环境快照", "刷新环境", "shell snapshot")),
    ),
}


TOOL_GUIDES = {
    name: ToolGuide(category, use_when, aliases)
    for category, entries in _GROUPS.items()
    for name, use_when, aliases in entries
}


TOOL_USAGE_INSTRUCTIONS = (
    "Use directly listed tools for routine read/search/patch/command/diff work. "
    "Read several known files with read_files. Use returned command_id values to continue commands. "
    "For unfamiliar capabilities, tool_search({}) shows categories; "
    "tool_search({\"category\":\"code\"}) shows summaries; "
    "tool_search({\"query\":\"code_definition\",\"include_schema\":true}) retrieves one tool's parameters. "
    "You may skip browsing and search an English or Chinese intent directly. "
    "Only enabled capabilities are discoverable. Reuse schemas already retrieved in this connection. "
    "Use persistent tasks, plans, reviews, and checkpoints only when the work needs them. "
    "Follow applicable project instructions before editing."
)


def normalize_query(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.lower().replace("_", " ")))


def discovery_score(query: str, name: str, title: str, description: str) -> float:
    """Rank explicit names/intent aliases before broad category or word matches.

    Chinese aliases use substring matching because whitespace tokenization does
    not split Chinese sentences. English aliases match complete word sequences.
    This is a local lexical index, not a semantic model or external service.
    """
    guide = TOOL_GUIDES[name]
    name_text = normalize_query(name)
    title_text = normalize_query(title)
    if query in {name_text, title_text}:
        return 1000.0
    aliases = [normalize_query(alias) for alias in guide.aliases]
    if query in aliases:
        return 800.0

    def phrase_in(phrase: str, text: str) -> bool:
        if re.search(r"[\u3400-\u9fff]", phrase):
            return phrase in text
        return f" {phrase} " in f" {text} "

    score = 0.0
    for alias in aliases:
        if phrase_in(alias, query):
            score = max(score, 300.0 + min(len(alias), 60))
        elif len(query) >= 2 and phrase_in(query, alias):
            score = max(score, 80.0)
    if phrase_in(query, name_text):
        score = max(score, 120.0)
    tokens = set(query.split()) - {"a", "an", "the", "to", "for", "of", "and", "how", "i", "do", "can", "please"}
    name_tokens = set(name_text.split())
    description_tokens = set(normalize_query(f"{description} {guide.use_when}").split())
    score += min(60, 16 * len(tokens & name_tokens))
    score += min(15, 3 * len(tokens & description_tokens))
    category = CATEGORIES[guide.category]
    if any(phrase_in(normalize_query(alias), query) for alias in (*category.aliases, guide.category)):
        score += 6
    similarity = max(
        difflib.SequenceMatcher(None, query, name_text).ratio(),
        difflib.SequenceMatcher(None, query, title_text).ratio(),
    )
    if similarity >= 0.6:
        score += similarity * 10
    return score
