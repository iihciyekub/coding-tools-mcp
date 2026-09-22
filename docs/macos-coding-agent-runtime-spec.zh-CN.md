# macOS Coding Runtime 精简规格

日期：2026-09-22
状态：当前实现基线

## 1. 定位

Coding Tools MCP 是一个 **macOS-first coding runtime**。MCP 是协议入口，不是 Agent 工作流引擎。

Runtime 负责给模型提供可靠的“眼睛和手”：

- 文件读取、搜索与原子 Patch；
- 有界命令执行和长进程生命周期；
- Git 只读证据；
- 代码符号、定义、引用和诊断；
- Project Gateway 与权限边界；
- macOS 开发工具链状态。

Runtime **不负责**：计划、Task、Review、Checkpoint、Skills、Agent memory、Agent 编排、自动修 Bug/自动重构等模型层工作流。

## 2. 核心原则

### 2.1 Primitive first

能由 LLM 组合现有 primitive 完成的工作，不新增 MCP Tool。

新增 Tool 必须至少满足一项：

1. 不增加它，模型无法可靠取得该能力；
2. 它提供明显的安全边界；
3. 它提供原生命令难以稳定解析的结构化结果；
4. 它能显著减少高频串行 round-trip，并且不替模型做方案决策。

### 2.2 合并机械步骤，不合并认知步骤

允许：`read_files` 批量读取多个已知文件。
不允许：`fix_bug`、`refactor_project`、`review_code` 这类把模型推理封装进 Runtime 的工具。

### 2.3 macOS native first

已有成熟 macOS/Unix 开发能力时优先复用：

- Swift 语义：SourceKit-LSP；
- Xcode 构建/测试：`xcodebuild`；
- Swift Package：`swift build` / `swift test`；
- Git 写入、branch、worktree：原生 `git`；
- 签名/公证：`codesign` / `notarytool`；
- Homebrew：`brew`；
- 其他 CLI：统一通过 `exec_command`。

Spotlight (`mdfind`)、Quick Look、LaunchServices、AppleScript、`xctrace` 等能力只有在真实 benchmark 证明能明显改善任务成功率或效率时才加入 Adapter；不能因为 macOS 有 API 就增加 MCP Tool。

## 3. 当前 Tool Surface

当前注册工具：**29**。CI 硬限制：

- registered tools ≤ 32；
- total input schema ≤ 16,000 bytes；
- single schema ≤ 2,048 bytes。

普通单 Project Runtime 直接暴露 **28** 个工具：文件读取/搜索/补丁、命令生命周期、
Git 证据、代码导航/诊断、项目概览/规则、检查发现、权限请求和 runtime doctor。
不存在 `tool_search` / `tool_invoke` 二次发现层。Persistent Project Gateway 只额外暴露
`project_context`。

## 4. 已删除的过度抽象

以下能力不再属于 MCP Runtime：

- Task / Plan / Task Events；
- Review records；
- Checkpoint / Context Checkpoint；
- `checks_run` / `checks_result` 持久化执行层；
- Git branch/stage/commit/worktree wrappers；
- Repo Map / change-impact heuristic；
- semantic rename preview wrapper；
- workspace Skills 索引；
- local Agent environment 枚举；
- shell snapshot / lightweight exec-environment Tool；
- Protocol Tasks extension。

对应职责分别交回调用 Agent、`exec_command`、原生 Git、SourceKit-LSP 和项目自身脚本。

## 5. Project Gateway

长期结构：

```text
ChatGPT / Codex / Claude
          │
          ▼
   Persistent MCP Gateway
          │
     MCP Session
          │
          ▼
   Selected Project
          │
          ▼
 Immutable Project Runtime
```

不变量：

1. Project Root 是默认上下文边界；
2. Permission Scope 是最大权限边界；
3. Full/Host access 不等于默认搜索整个 Mac；
4. 不存在 Desktop 全局可变 `current_project`；
5. 前台窗口不能偷偷改变远程 MCP Session 的 Project；
6. Session 只保存路由状态，不保存 prompt、conversation、文件正文或 token。

## 6. Code Intelligence

高频入口只有：

- `code_symbols`；
- `code_definition`；
- `code_references`；
- `code_diagnostics`。

带 source position 的 definition/reference 优先使用语言服务器语义，失败时安全降级到有界扫描。Swift 优先 SourceKit-LSP。

不再为每个 LSP method 建立一个 MCP Tool。

## 7. Git

MCP 保留只读、高信息密度的证据工具：

- status；
- diff；
- log；
- show；
- blame。

写操作直接使用 `exec_command` 调用原生 Git。模型需要时先读取 `git_status` / `git_diff`，执行原生命令，再重新读取证据。

Runtime 不再维护自己的 branch/worktree/checkpoint 生命周期。

## 8. Checks

`checks_discover` 只负责从已有 manifest/config 中发现项目定义的测试、lint、typecheck、build 命令并给出确定性的推荐理由。

真正执行统一走 `exec_command`。Runtime 不再持久化 check result，也不再制造另一套 command lifecycle。

## 9. Result Contract

每个 Tool 的模型可见结果必须包含下一步调用所需的关键信息。

例如：

- command: `command_id`, `output_ref`, `next_action`；
- project: `project_id`, root, runtime state；
- discovery: selected tool schema + invoke route；
- error: code, category, retryable, required action。

不能假定宿主一定把完整 `structuredContent` 暴露给模型。

## 10. Benchmark

不再以“工具能调用成功”作为主要能力证明。建立同模型 A/B 评测：

- Minimal primitives；
- 当前 Runtime；
- 候选新 Adapter/Tool。

至少记录：

- task success rate；
- first-pass success；
- tool-call 数；
- discovery calls；
- duplicate calls；
- model↔MCP round trips；
- end-to-end latency；
- wrong-project / boundary violations；
- invalid retries；
- user interventions。

只有能改善这些指标的高级能力才进入 Runtime。

## 11. 当前工程规则

1. 不恢复已删除 workflow API；
2. 不为兼容旧版保留 shadow implementation；
3. 不新增“一个动作一个 wrapper”的 macOS Tool；
4. 优先改善 primitive 的可靠性、结果质量和批量效率；
5. Project Gateway、权限边界和 Result Contract 的正确性优先于功能数量；
6. Tool Surface Budget 是 CI gate，不是建议值。
