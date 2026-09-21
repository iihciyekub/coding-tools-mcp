# Coding Tools MCP — Local Agent Coding Runtime Gateway

日期：2026-09-22。状态：0.4.2 当前架构。

## 1. 目标

Coding Tools MCP 的目标是给当前调用 MCP 的模型提供稳定、模型无关的软件工程能力，而不是成为桌面自动化或第二套 Agent。

重点能力：

- 文件读取、搜索、补丁与图片查看；
- Shell、Git、Worktree、LSP、Checks、Review；
- 持久 Task 与 Context Checkpoint；
- 本机 Agent 环境与 Skills/Plugins/Rules/Worktrees 的 metadata discovery；
- 构建、测试、发布与证据收集。

## 2. 明确不做

0.4.1 起，Coding Tools MCP 不提供通用 App / Computer Control：

- 不控制 macOS 应用窗口；
- 不提供鼠标、键盘、拖动、滚动或屏幕操作 MCP 工具；
- 不打包 Accessibility / Screen Recording helper；
- 不桥接 Codex Computer Use；
- 不通过 `codex exec` 或其它 Agent CLI 启动第二个模型完成当前任务。

这类能力维护成本高、平台耦合强，对编码任务的平均收益低于文件、命令、Git、LSP、测试和上下文恢复能力。

## 3. Agent Environment Discovery

`agent_environment` 仅在 Full Access / host mode 下暴露，负责发现本机 Agent 环境的 metadata：

- Codex；
- Claude Code；
- Gemini CLI；
- Cursor；
- OpenCode；
- Skills；
- 已启用 Plugin 名称；
- Plugin Skills；
- Worktrees；
- Rules；
- Browser/Chrome Plugin 是否存在或启用。

敏感资源只报告存在性，不读取内容：

- auth；
- token；
- cookie；
- OAuth；
- browser session；
- credential files。

发现到 Skill 时只返回 metadata。当前模型按需选择并读取单个 Skill，禁止把全部 Skills 自动注入上下文。

## 4. Context Checkpoint

`context_checkpoint(action=create|get|list)` 提供模型无关的跨会话恢复能力。

服务器自动记录：

- workspace；
- Git HEAD / index / changed paths fingerprint；
- 当前运行命令；
- Runtime capability fingerprint。

当前调用模型提交：

- summary；
- decisions；
- unresolved；
- next_steps。

CTM 不为 checkpoint 启动额外模型，也不调用 Codex `thread/compact/start`。

读取 checkpoint 时重新计算 deterministic fingerprint，并显式返回 `stale` 与 `stale_reasons`。

## 5. Browser 原则

浏览器能力只服务 Web 编码验证，不做用户日常浏览器遥控。

当前推荐路径是 `python -m coding_tools_mcp.browser_check` + 现有 MCP 文件/命令/图片工具：

- 只针对用户明确给出的本地开发 URL；
- 使用临时、无持久登录态的浏览器上下文；
- 不连接用户日常 Chrome Profile；
- 不读取 cookies / passwords / 登录页面数据；
- 不新增常驻 Browser MCP 工具，除非未来有明确、可验证的编码收益。

## 6. Provider 与产品无关

公共 MCP contract 使用通用软件工程概念，不增加：

- `codex_*`；
- `claude_*`；
- `gemini_*`；
- vendor-specific GUI tools。

第三方 Agent 环境只是 metadata source。真正的文件、命令、Git、LSP、Workflow 能力由 CTM 自己实现和约束。

## 7. 安全边界

Full Access 表示：

- host filesystem；
- host shell environment；
- SSH / Git / package managers / installed developer tools。

它不表示 CTM 会读取凭据内容或主动操作 GUI。

`agent_environment` 必须保持 metadata-only；Context Checkpoint 不保存文件正文、token、cookie 或屏幕内容。

## 8. 工具面原则

不是 MCP 工具越多越好。

新增能力必须至少满足一项：

1. 显著提高编码任务完成率；
2. 显著提高验证质量；
3. 让模型完成过去无法完成的软件工程任务；
4. 能稳定抽象，不依赖脆弱 GUI 状态。

优先复用现有工具参数与 Workflow Store，避免为每个 Agent/Provider 增加一组新工具。

## 9. 0.4.2 架构

```text
Current LLM
    ↓
Coding Tools MCP
    ├── Files / Search / Patch / Images
    ├── Shell / Processes
    ├── Git / Worktrees
    ├── Code intelligence / LSP
    ├── Checks / Reviews / Evidence
    ├── Tasks / Plans
    ├── Context Checkpoint
    └── Agent Environment Discovery
```

不包含 Desktop Computer Automation。

## 10. Definition of Done

0.4.2 的 Local Agent Coding Runtime Gateway 满足：

- `agent_environment` 仅 host mode 可见；
- discovery 不返回敏感文件内容；
- Context Checkpoint 不调用额外模型；
- checkpoint 能检测 Git/命令/capability stale；
- Desktop 不打包 Computer helper；
- Runtime 不暴露 App/Computer tools 或 CLI flags；
- 文档、schema、工具数量和 release metadata 与 live registry 一致；
- Python、TypeScript、Rust 和 compliance 全量测试通过。
