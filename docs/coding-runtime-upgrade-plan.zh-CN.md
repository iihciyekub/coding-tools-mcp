# Coding Tools MCP 开发能力升级方案

状态：M1、M2 与 M3 的主要闭环已经实现。项目概览、规则解析、workspace Skills、带代码指纹的检查证据、持久任务上下文与原子 Plan、文件 Checkpoint、桌面状态与审批面板、带 HEAD/index 并发校验的 Git 写流程、受管 worktree、可选 Python/TypeScript LSP 查询与 rename preview、可持久化 Review、完整浏览器基础动作、受管下载、有界单调用事件采样，以及 Runtime 生命周期内可跨调用轮询的浏览器 watch 已经实现；可选 Controller 和跨 Runtime 恢复的浏览器会话属于 M4/后续范围。

核查日期：2026-09-11。源码基线：`1aa3ff2485300585d10ccebe701feb4020dd9504`；Python core `0.3.8`，Desktop `0.3.19`。本方案结合用户提供的“分析MCP脚手架能力”对话预览、当前源码和一手资料整理。外部产品资料会变化，正式接入时应固定依赖版本并重新验证。

## 1. 建议采用的方向

把项目升级为“可被 ChatGPT 等宿主使用的开发执行与工作流运行时”，优先完成：

**理解项目 → 定位代码 → 生成可核查修改 → 验证 → 审查 → 保存进度与恢复。**

分成三个可独立交付的层次：

1. **Core：可靠开发工具。** 保留文件、Patch、命令、Git 读取、浏览器与 macOS 控制，补充项目概览、语义查询、验证和可恢复修改。
2. **Workflow：持久任务记录。** 提供计划、证据、审查记录、审批请求和恢复摘要，不调用模型也能工作。
3. **可选 Controller：实际运行 Agent。** 以后按需接入外部 Agent 后端，才承担模型循环、后台运行、重试和子 Agent 调度。

前两层最值得先做。它们可以改善 ChatGPT 的工具条件、任务衔接和验证质量，但不会使 ChatGPT 必然持续调用工具，也不能决定其隐藏上下文、思考过程或何时停止。加上 Controller 后，后台工作由独立执行器完成，应明确展示执行器身份和成本，不能描述成接管当前 ChatGPT 对话。

Codex App Server 官方提供会话、审批、事件流及审查等集成接口，可作为未来 Controller 的候选后端；是否适合本产品仍需单独原型验证。[官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)

## 2. 当前能力与上一轮判断的修正

| 范围 | 当前核查结果 | 对方案的影响 |
| --- | --- | --- |
| 版本与目录 | Core 0.3.8，Desktop 0.3.19；注册表声明 104 个工具，默认暴露 51 个，工作流扩展增加 53 个 | 分别管理 core、桌面版本与兼容范围，避免统称 App 0.3.8 |
| 文件与命令 | 已有原子 Patch、基线校验、PTY、输出分页、命令查询及 `operation_id` 去重 | 复用现有执行基础；不要新造第二套命令管理器 |
| 项目规则 | 根/嵌套规则可按路径解析，工作区 Skills 可索引和读取 | 后续可增加刷新通知；技能读取继续保持工作区边界且不执行脚本 |
| 代码语义 | 原有轻量索引保留，并新增 Python、TypeScript/JavaScript LSP 定义、引用、诊断与 rename preview | LSP 缺失时明确退化；rename 仍需由调用者审查后应用 |
| Git | 只读查询、分支、暂存、提交、冲突和受管 worktree 均有结构化接口 | 写操作绑定 HEAD/index，worktree 位于私有状态目录；不自动 push 或强制删除 |
| 权限 | safe/trusted 可创建精确、限时、单次审批，桌面负责批准或拒绝，执行前原子消费 | 保持客户端不能自批；dangerous/host 仍是操作者启动时的显式策略 |
| 浏览器 | CDP/Playwright、Chrome 扩展桥、导航/表单动作、受管上传下载、有界事件采样和 Runtime 内持久 watch 已存在 | watch 有序号、丢失计数和显式 stop；不宣称跨 Runtime 重启恢复或原生 Downloads UI 事件可靠 |
| 桌面端 | Tauri + React + Rust 管理 workspace、运行时、隧道、依赖、任务状态、审批与 worktree 注册 | 当前不是内置模型聊天工作台；Controller 仍应保持可选与独立生命周期 |
| 恢复 | 客户端重连可通过句柄找回同一运行时内的命令 | 不等于 Python 进程重启后仍保有命令，更不等于 App 退出后任务持续执行 |
| 协议与状态 | 一个 workspace 一个信任域，无传输会话 | 持久业务任务可以新增，但不能暗中恢复旧 session 或声称已有多租户隔离 |
| 工具目录 | 规范明确固定目录，无 profiles、无动态列表 | 工具分组及新工具暴露需要显式契约迁移，不能当作透明补丁 |

源码依据：[注册表、Runtime 与权限](../coding_tools_mcp/server.py)、[代码查询](../coding_tools_mcp/code_intel.py)、[项目规则](../coding_tools_mcp/project_context.py)、[Patch 实现](../coding_tools_mcp/patching.py)、[浏览器](../coding_tools_mcp/browser.py)、[桌面说明](../apps/desktop-client/README.md)。现行边界以 [SPEC](../SPEC.md)、[v0.3 契约](runtime-contract-v0.3.md) 和 [限制](limitations.md) 为准。

还需修正三种预期：

- **LSP 是重要增强，但不是所有任务的最大收益项。** 单文件问题可能用搜索和 lint 更快。语言服务存在启动、内存、配置与同步成本，应测量后按项目启用。OpenCode 的官方说明也明确讨论这些取舍。[OpenCode LSP](https://opencode.ai/docs/lsp/)
- **工具搜索不等于宿主动态加载。** MCP 有工具列表变更通知，但宿主是否刷新、如何呈现及是否支持工具搜索，需要实测；一个自定义 `tool_search` 返回 JSON，并不能强制 ChatGPT 将其变成可调用工具。[MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- **MCP Tasks 不应称为“2026 新能力”。** 2025-11-25 规范已经引入 Tasks，该页面标记为实验性。协议任务包装与产品开发任务记录是不同概念，应使用适配器连接。[MCP Tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)

## 3. 同类项目中值得借鉴的部分

以下为架构参考，未对这些项目运行相同任务的性能比较；不据此宣称速度或完成率领先。

| 参考项目 | 已核实的相关方向 | 本项目的取舍 |
| --- | --- | --- |
| [Serena](https://github.com/oraios/serena) | 面向 Agent 的符号级查询、编辑等能力，提供 LSP 与 IDE 后端路线 | 借鉴语言服务适配与语义定位；第一批只支持少量语言，不复制整个 IDE |
| [Aider Repo Map](https://aider.chat/docs/repomap.html) | 在上下文预算内选择重要符号和依赖相关内容 | 建轻量项目地图，并返回选取依据、范围与截断信息 |
| [GitHub MCP Server](https://github.com/github/github-mcp-server#tool-configuration) | 可配置 toolsets 与单独工具，并考虑旧名称兼容 | 先做启动时固定的能力集合；动态发现作为后续客户端适配 |
| [OpenCode LSP](https://opencode.ai/docs/lsp/) | 语言服务诊断反馈，以及资源、同步成本的明确取舍 | 保留 CLI 验证路径；LSP 不成为安装或正常使用的强制依赖 |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | 浏览器专用动作，包括文件上传与弹窗处理 | 借鉴动作覆盖和目标定位；保持本项目的相对路径与权限边界 |
| [Codex App Server](https://learn.chatgpt.com/docs/app-server) | 可嵌入客户端的 Agent 协议，含 thread/turn、审批与 review | 后期评估独立适配；Core 不绑定其账号、模型或私有桌面状态 |

建议自行维护的核心价值是：现有执行契约、统一错误与输出、修改事务、验证证据以及桌面操作体验。语言服务和未来 Agent 后端优先采用成熟实现。直接复用第三方源码前，另外核对许可证、分发方式和依赖维护成本。

## 4. 能力清单与优先级

P0 表示首个增强版本必须解决；P1 表示形成完整开发闭环；P2 为后续可选能力。表内为工作包，具体函数名在接口设计阶段冻结。

| 优先级 | 工作包 | 第一版交付范围 | 用户能感知的收益 |
| --- | --- | --- | --- |
| P0 | 契约与模块注册 | 单一注册表、静态能力集合、能力状态、兼容模式 | 更新可控，缺依赖时有明确解释 |
| P0 | 项目概览与 Repo Map | 子项目、清单、入口、规则路径、符号摘要、预算控制 | 更快找到相关模块，减少无目的读取 |
| P0 | 规则与 Skills | 按路径解析规则，索引/读取本地 Skills，版本与来源 | 更稳定地采用项目要求和既有开发步骤 |
| P0 | Checkpoint 最小闭环 | 显式路径快照、差异预览、冲突检测恢复 | 修改出错时可按范围恢复 |
| P0 | 验证命令 | 发现 test/lint/typecheck/build，执行选定检查并保存结果 | 减少猜错命令、错目录与漏验 |
| P0 | 最小任务与证据存储 | 目标、状态、检查结果、checkpoint 关联、恢复摘要 | 换连接后知道做过什么、下一步是什么 |
| P1 | LSP 查询 | Python、TypeScript/JavaScript 的 definitions、references、diagnostics | 更准确地定位符号及检查修改影响 |
| P1 | 语义修改 | rename 生成修改预览，统一进入 Patch 校验与提交 | 降低同名误改和跨文件遗漏 |
| P1 | Git 工作流 | 分支/worktree、按范围暂存、commit、冲突检查 | 更清楚地隔离和交付改动 |
| P1 | 完整任务/Plan/Review | 计划更新、事件游标、审查快照与发现、恢复上下文 | 任务过程和验收依据可以检查 |
| P1 | 审批与桌面展示 | 持久请求、一次性授权、过期/拒绝、任务/差异面板 | 在需要授权时确认具体动作，已有授权内顺畅执行 |
| P1 | 浏览器动作 | navigate/back/reload、wait、select、hover、press、upload、dialog、download | 支持表单、文件和弹窗组成的长流程 |
| P2 | 扩展语言与 DAP | Rust/Go 等后端，单一调试适配器试点 | 支持更多工程和运行时排错 |
| P2 | 协议 Tasks 适配 | 依宿主能力包装长操作，保留原有轮询接口 | 支持宿主可识别的异步操作 |
| P2 | 可选 Controller | 单 Agent 后端先行，再考虑多 Agent | 独立执行器可运行更长任务 |
| P2 | 跨平台桌面自动化 | Windows UIA / Linux AT-SPI，另评估 Windows ConPTY | 扩大平台覆盖，避免把 macOS 能力泛化 |

## 5. 推荐架构与代码归属

```text
ChatGPT / 其他 MCP 宿主             Desktop 控制面板
             │                           │
             └────── MCP / 受限控制接口 ──┘
                              │
                    注册表 + 参数/权限校验
                              │
          ┌───────────────────┼──────────────────┐
       基础执行             项目能力          工作流服务
   文件/Patch/命令/Git    Map/Skills/LSP     Task/Review/Approval
          └───────────────────┼──────────────────┘
                  有界输出 + 证据与事件存储

       可选 Agent Controller（独立生命周期，后期接入）
                  │ 调用相同受检验能力
                  └───────────┘
```

在 `coding_tools_mcp/` 内按能力增量增加 `tool_registry.py`、`workspace_insight.py`、`skills.py`、`verification.py`、`git_ops.py`、`checkpoints.py`、`lsp/`、`workflow/`。这些是建议路径，不要求先进行全仓搬迁。`server.py` 逐步转为组合入口；保留现有公开 import 与 CLI。

桌面 UI 和受限 Rust 控制接口归 `apps/desktop-client/`；Python 服务拥有任务/审批状态，桌面通过接口读写，避免两套数据库。Controller 若进入开发，建议放 `integrations/agent-controller/`，核心不反向依赖它。对应测试归 `tests/`，可复用评估归 `benchmarks/`，结果归 `reports/`。

不为这次能力升级改动隧道、npm launcher 或 Cloudflare 控制契约，除非具体接入验证发现必要的接口变化。

### 5.1 统一结果与状态

新工具继续沿用当前结果封装：简短 `content`、可机器读取的 `structuredContent`、稳定错误码和明确下一步。新增结果按需携带：

- `workspace_id`、`task_id`、`operation_id`：区分工作区、产品任务和一次可去重操作。
- `revision`、`snapshot_id`、`file_hashes`：校验状态和证据是否仍对应当前代码。
- `backend`、`capabilities`、`coverage`、`stale`：说明结果来自 LSP、AST、文本或命令解析，是否完整和新鲜。
- `artifact_id`、`next_cursor`、`truncated`：大输出有界且可分页，不把完整源码与日志塞进初始化。

这些字段先在新增接口定义，不能未经兼容测试改动旧工具必填字段、位置含义或错误语义。状态数据不是新的 MCP 传输 session，也不是个人聊天记忆。

推荐最小持久层为一个 workspace 对应的 SQLite 数据库和受限 artifact 目录，位于稳定的应用数据目录，不能放进现有启动即清理的临时目录。确定 workspace 迁移/重命名规则、schema 迁移、容量/TTL、备份和过期句柄返回语义。不要由模型直接写数据库。

一个 workspace 继续是共享信任域。若将来需要不同客户或团队成员互相隔离，必须另做身份、ACL、配额设计；增加 `task_id` 不提供这种隔离。

## 6. 各工作包需要补全的设计

### 6.1 项目理解：先便宜地知道该读哪里

建议工具：`workspace_overview`、`repo_map`、`project_instructions`。

第一版扫描 Git 跟踪文件和有界的未跟踪候选，尊重忽略目录；识别 package/pyproject/Cargo/go.mod 等清单、子项目边界和规则文件。Map 用路径、公开符号、导入关系与任务关键词排序，输出可核查的文件/符号位置。估算 token 时注明估算方法，并设字节硬上限。

Repo Map 不依赖 LSP 才能启动，不默认加向量数据库或外部 embedding。动态导入、生成代码与无法解析的关系标记为未知，不能返回“完整调用图”。文件变更、分支切换和外部编辑触发失效；返回索引版本及范围。

验收：在本仓库中能分出 core、桌面、隧道、Worker 等边界；用户只问桌面问题时优先给出桌面入口与适用规则；大仓库超预算时仍返回有用结果和明确截断信息。

### 6.2 Skills：规则供给与脚本执行分开

建议工具：`skills_list`、`skills_read`。第一版只索引本地 `SKILL.md` 的名称、描述、作用域、来源、内容摘要哈希和依赖声明；宿主选择后读取全文及被引用资源。渐进加载参考官方 Skills 设计。[官方 Skills 文档](https://learn.chatgpt.com/docs/build-skills)

默认只读 workspace 中配置的路径；用户全局技能库需要由操作者额外配置可读根，不能借技能引用逃逸工作区。相对资源路径、软链接、同名技能、无效 front matter、超长内容都要有确定结果。

适用的子目录规则优先于父目录规则；规则文件和 Skill 都不得提升运行时权限。读取 Skill 不执行其脚本，脚本仍走既有命令策略。首批可附 bugfix、refactor、verify、review 的指令型模板，但不承诺宿主一定遵循。远程安装和插件市场留到后续。

### 6.3 验证：将结果绑定到具体改动

建议工具：`checks_discover`、`checks_run`、`checks_result`。先覆盖 Python 的 unittest/pytest/ruff/typecheck 和 Node package scripts；Rust/Go 后续按真实项目增加。

发现阶段只解析清单和已配置命令，不运行安装或脚本。执行时展示实际命令、workdir、范围、超时和来源，并复用现有 `exec_command`、命令句柄及输出存储。项目提供的 test/build 脚本可能执行任意代码，必须走相同策略。

结构化结果区分 passed、failed、skipped、blocked、cancelled、unknown；保留退出码和原始日志位置。适配器解析失败不得伪造通过。保存开始/结束代码指纹；检查过程中源码变化时标记证据可能过期。影响范围不确定时给出建议和依据，不能声称已验证未执行的检查。

验收：能正确处理测试失败、没有测试、缺工具链、超时及被中止；同一个 `operation_id` 重试不重复启动检查。检查结束后改文件，旧结果不会仍显示为当前验证通过。

### 6.4 Checkpoint：恢复用户原有工作是首要正确性要求

建议工具：`checkpoint_create`、`checkpoint_list`、`checkpoint_diff`、`checkpoint_restore`。

第一版采用显式文件范围的内容快照，覆盖文本内容、存在/删除状态和模式；记录当时 HEAD 与范围边界，不修改用户分支或 index。大型二进制、符号链接、submodule、LFS 或超预算内容在创建时明确排除/拒绝，不能把部分快照叫“全仓恢复点”。

恢复分为预览和执行：预览返回将修改/创建/删除的文件、预期当前哈希和恢复目标；执行再次校验。checkpoint 后出现的用户改动必须报告冲突，不能无条件覆盖。操作不得通过默认 `reset --hard`、`clean -fd` 或覆盖 stash 实现。

文本恢复复用 Patch 的基线、提交与回滚机制；无法回滚时必须保留恢复材料并显式失败。该机制保证受支持范围的源码恢复，不撤销数据库写入、浏览器提交、依赖安装等外部副作用，也不恢复暂存区；UI 必须标出这个范围。

验收：任务开始前已有未提交修改；任务运行后又有用户编辑；只恢复选定路径，用户后来编辑会触发冲突，范围外文件及 index 保持一致。删除、新建、Unicode 和多文件写入失败需覆盖。

### 6.5 LSP：先查询，再引入写操作

新工具候选：`lsp_status`、`lsp_definition`、`lsp_references`、`lsp_diagnostics`、`lsp_rename_preview`。保留旧 `code_*` 工具的名称查询行为，不悄悄改成需要光标位置的新协议。

第一阶段支持 Python 与 TypeScript/JavaScript，按 workspace/subproject 管理服务进程，记录可执行文件版本、配置根和支持能力。现有 AST/文本工具保持可用；缺少 LSP 时返回未安装/不支持及可选替代方案，不能把文本命中标成语义引用。

必要工程内容：进程启动/退出、初始化超时、文件同步、外部编辑检测、诊断新鲜度、服务崩溃恢复、资源上限、取消与 monorepo 根识别。定义公共位置编码并正确转换 LSP 协商的编码，测试中文、emoji、CRLF，避免字符列偏移。

rename 先返回带文件哈希的修改提案，经统一校验再由 `apply_patch` 提交。对文件创建/移动等资源操作、越界 URI 和不支持的 WorkspaceEdit 显式拒绝或分阶段支持；不允许后端命令绕过写入检查。LSP 启动与依赖安装也必须遵守执行策略，不能成为新的 shell 旁路。

验收：同名但不同作用域变量不会一起重命名；跨文件别名/引用正确；预览后用户编辑导致提交拒绝；LSP 退出时原有工具仍可工作。预览成功不能作为重构已经成功的证据，仍须检查和测试。

### 6.6 Git：隔离、暂存、提交分别定义

建议补充 `git_branch_list/create`、`git_worktree_list/create/remove`、`git_stage`、`git_unstage`、`git_commit`、`git_conflicts`。先完成选定路径暂存与准确 diff，再考虑 hunk 暂存；第一版不自动解决 merge/rebase 冲突、不自动 push 或发布。

每次写操作记录 repo/worktree、预期 HEAD/index、路径范围和 operation_id；禁止默认 `git add -A`。提交只包含明确的已审查范围，考虑已有用户暂存内容、hooks、签名失败和操作超时。Git hooks/filter 等执行行为也需要沿用执行策略，不能把 Git 写操作当作无副作用元数据更新。

**Worktree 边界必须单独解决：**当前一个 Runtime 固定一个 workspace。建议由受限管理接口在操作者配置的目录创建 worktree，并为其启动独立 Runtime/注册独立 workspace；core 不能因创建 worktree 就获得任意兄弟目录访问权。返回 worktree 与连接/切换信息。linked worktree 的 Git common directory 访问要显式校验与授权。

如果目标 ChatGPT 连接不能便捷切换多个 workspace，应先交付 checkpoint 和当前 workspace 的 Git 操作，worktree 标记为桌面可用，不能宣称聊天端已形成一键闭环。移除 worktree 必须检测未提交改动及相关运行命令，保持非强制默认。

### 6.7 Task、Plan 与恢复：保存可验证状态

P0 先做 `task_create/get/list/update` 与 `task_context`，记录目标、范围、当前状态、关联 checkpoint/check run、待做事项。P1 再加 `task_events` 和计划步骤的原子更新；不另建一套与 task 重复的 Goal 存储。

建议状态为 `pending → running → waiting_input / blocked / completed / failed / cancelled`，显式定义允许的迁移；状态更新带 revision，防止多个客户端覆盖。完成记录必须附验收结果，缺少必要验证时只能记录原因或等待，不能因文字写“完成”就使所有检查变绿。

`task_context` 返回最新有效摘要、改动范围、证据链接、未决问题和建议下一步。它不负责压缩 ChatGPT 内部上下文；用户重连后，由宿主读取并决定继续。

运行时重启时：持久任务/证据可读；无法确认仍存活的命令标记 interrupted 或 unknown，不能假装仍在运行，也不能自动重放 commit/upload 等有副作用动作。后台运行跨 App 退出需要独立 supervisor，留给 Controller 阶段。

### 6.8 Review：材料、发现和模型执行分清

建议工具：`review_prepare`、`review_record`、`review_get`。准备阶段固定 base/head 或工作树内容快照，集合 diff、相关规则、验证记录和未覆盖范围。记录阶段接受带文件、行号、理由、优先级、来源和状态的结构化发现。

Core 不调用模型时，由当前 ChatGPT 或人工阅读后记录发现。不能把“准备好了 diff”称为“已经完成独立 AI 审查”；也不能把 lint 无报错等同于逻辑正确。未来可选后端才提供真正的异步 reviewer 执行接口。

代码变化后旧 review 标记 stale；发现行号绑定快照而非永远绑定当前文件。修复发现后重新验证，记录 superseded/resolved 关系，避免丢失原始审查依据。

### 6.9 Approval：只在授权不足时进入决策链路

已有权限内的正常读写与已授权工作直接执行。对策略需要额外授权的动作，返回具体请求：workspace、动作与参数摘要、目标范围、有效期和预览摘要哈希。建议状态为 pending、approved、denied、expired、cancelled，执行时一次性消费授权，并校验参数/文件状态未变化。

MCP 模型可以创建/读取审批请求；批准/拒绝只能通过可信操作者通道，例如受认证的桌面 Rust 接口，不能给相同远程模型一个可自批的通用 `approve` 工具。桌面与 CLI 都需要可用的操作者路径，超时或无人处理不能自动批准。

不把一项授权转换为整个 workspace 的 dangerous/host 模式。保留旧 `request_permissions` 兼容语义，第一版通过新增审批接口演进；是否弃用旧接口另行版本化。审批不是沙箱，多客户端仍共享 workspace 信任域。

### 6.10 Browser 与桌面：补操作链，也补状态链

浏览器新增动作按 CDP 与 Chrome Bridge 分别声明支持矩阵；不支持时返回明确错误，不能默认在两种后端悄悄切换目标页。操作绑定稳定 tab 标识与最新快照，目标不存在或引用过期时要求重新定位。

当前 CDP 连接按调用创建，弹窗、下载和 chooser 等跨调用事件不能仅靠增加函数封装完成。需要评估有界持久 Browser Manager，或在一次调用中完成监听与触发；处理超时、断连和浏览器关闭，避免误关闭用户 Chrome。

上传只接受允许根内的显式文件，下载限定受管目录并处理重名/大小/超时。上传文件和点击最终提交应分别记录；需要额外授权时绑定具体文件、页面与目标动作。弹窗、上传、下载既有浏览器约束，也有真实外部副作用，不属于 Git 回滚范围。

桌面按交付阶段增加四块视图：能力状态与依赖准备、任务与验证结果、checkpoint/diff/review、待审批动作。沿用现有受限 Rust API 与 Keychain 机制，不给 webview 通用 shell。依赖包采用固定版本及可验证来源，离线/缺依赖仍能使用基础工具。

## 7. 工具目录与兼容迁移

目前固定工具目录是明确产品契约，新增工具、Skills 与工作流也涉及 [SPEC](../SPEC.md) 中的边界。本文只提出变化，不提前把现行文档改写成已支持。

建议迁移顺序：

1. **先内部模块化。** 保持旧目录、输入输出和协议行为不变，建立注册表与 schema/文档生成一致性检查。
2. **新增显式契约版本。** 可用下一 minor 作为增强版候选，但最终版本按发布政策确定；同时提供 legacy 目录配置，默认兼容路径保持原工具集合。新能力仅在明确选择的增强配置中暴露。
3. **增强配置启动后目录固定。** 以 project、semantic、workflow、browser 等能力组选择工具；组配置与 permission mode 完全独立，权限仍由服务端检查。缺依赖的已暴露工具返回 unavailable，不随意抖动列表。
4. **宿主实测后再做发现优化。** `tools_search` 可提供建议与 schema 引用；只有验证宿主支持重新发现，才启用相应动态协议路径。对不支持的宿主继续用固定集合或重新连接。

不要先做任意 `call_tool(name, arguments)` 代理，把大量隐藏动作压进一个不透明 schema；这会使读写属性、参数检查和授权更难观察。工具数不是目标，用真实任务完成率和上下文开销决定集合大小。

保持当前两类协议、现有 `command_id` 生命周期与无会话架构；协议 Tasks 后续独立协商、独立测试。HTTP 隧道重连、宿主工具缓存与通知交付需列入验收，不能从本地 SDK 通过推断 ChatGPT 一定可用。

正式实现时一起更新契约、schema、工具说明、初始化指令和迁移文档。特别是新增 Git/checkpoint 写操作后，“apply_patch 是唯一直接写工具”的表述要改成准确的边界：文本源码编辑统一通过 Patch 引擎；Git、快照恢复及工作流状态具有独立受检验写语义。

## 8. 实施阶段与发布门槛

下列为顺序与粗估，按一位熟悉项目的全职开发者、现有工具链可用计算；不是排期承诺。集成调试与真实宿主验证可能显著改变工期，完成 M0 后重估。

| 阶段 | 交付内容 | 前置条件 | 退出标准 | 粗估 |
| --- | --- | --- | --- | --- |
| M0：设计与基线 | 最小接口 RFC、兼容矩阵、注册表整理、评估任务基线 | 当前代码可检查 | legacy 行为不变；ChatGPT/stdio/HTTP 能力差异有记录 | 1–2 周 |
| M1：首个可用增强版 | Overview/Map、规则/Skills、范围 checkpoint、checks、最小 task store | M0 | 一个实际 bugfix 流程可定位、改动、验证并冲突安全地恢复；桌面能查看结果 | 2–4 周 |
| M2：语义与 Git | 两类语言服务、rename preview、Git 写流程、worktree 接入试点 | M1 的修改/证据基础 | 跨文件重构、并发编辑冲突、缺后端退化和 worktree 边界通过 | 3–6 周 |
| M3：完整工作流 | Plan/Review、审批闭环、浏览器事件与动作、桌面视图完善 | M1，语义关联依赖 M2 | 重连/重启语义明确；实际表单流程与审批过期/拒绝可验证 | 3–5 周 |
| M4：可选扩展 | 单 Agent Controller、协议 Tasks、更多语言/DAP/其他平台 | 前述闭环有实际收益证据 | 单独 RFC、费用/身份/隔离/取消验证；之后才评估多 Agent | 单独估算 |

M0–M3 顺序投入约 9–17 个开发周；这是范围估算。可在 M1 先发布使用，无需等待所有 P1/P2。第二批功能应由第一批真实任务数据决定，不按工具数量推进。

第一轮建议按以下小任务拆分，避免一个超大 PR：

| 任务 | 主要归属 | 完成定义 |
| --- | --- | --- |
| T01：兼容与暴露 RFC | docs、registry、contract tests | 固定 legacy 输入输出、增强配置、不可用能力语义 |
| T02：项目概览与规则解析 | workspace insight、project context | 子项目范围、适用规则、预算、过期检测有样例 |
| T03：Skills 索引读取 | skills | 本地发现、来源、相对引用和越界处理完整 |
| T04：任务/证据最小存储 | workflow store | revision、分页、TTL、重启读取与 unknown 状态可验证 |
| T05：检查发现与执行 | verification、现有 command manager | 真实 Python/Node 检查结果可追溯，不复制进程管理 |
| T06：checkpoint 创建/预览/恢复 | checkpoints、patching | 用户原有改动及后续编辑不被静默覆盖 |
| T07：首版桌面状态与评估 | desktop、benchmarks | 能力/任务/验证结果可读，旧连接配置仍可用 |

这些任务号只是本文拆分建议，尚未在 GitHub 建 issue 或分派开发。

## 9. 如何证明更新有用

分开验证“工具确定性”和“模型任务表现”。现有 deterministic dogfood、reference patch 或工具单测都不能替代真实模型完成任务的评估。

工具层从相关单测开始，继续执行当前契约、schema drift、安全、双协议和端到端门槛，命令以 [CI 与测试指南](ci-and-tests.md) 为准。桌面变化执行 `make desktop-check`；发布前验证依赖准备、旧配置迁移和重新连接。

建议固定以下任务夹具与验收：

| 场景 | 必须观察到的正确行为 |
| --- | --- |
| 本 monorepo 定位一个桌面 bug | 找到正确 subtree 和嵌套规则，避免把 core 规则误用于所有组件 |
| Python / TypeScript 跨文件重命名 | 区分同名符号；预览后发生编辑会拒绝过期修改 |
| 测试失败后修复 | 记录真实失败与修复后的执行结果，检查范围/版本明确 |
| 原有脏工作区与 checkpoint 恢复 | 原有改动被捕获；后续用户编辑得到冲突提示；范围外与 index 不受损 |
| 断网/HTTP 重连/Runtime 重启 | 重连找回原句柄；重启保留业务记录并诚实标记失联进程 |
| 两个客户端同时更新任务/文件 | revision 或文件基线冲突可见，不静默后写覆盖 |
| 过期或参数变化的审批 | 原授权不再适用；拒绝不能被重试变成批准 |
| 浏览器上传、弹窗、下载 | 绑定实际 tab/文件/目标，文件在受管范围，事件未丢失 |
| 缺少 LSP、语言配置失败 | 说明缺失，基础 read/search/patch/checks 仍可使用 |
| 工具缓存不刷新的宿主 | 有固定目录兼容路径，不出现“已发现但不可调用”的假成功 |

Agent 层使用同一模型/配置、相同提示、仓库快照、机器与权限，对比 0.3.7 基线和增强版。冷启动、热缓存分开测；每项至少重复三次作初步趋势观察，结论不稳时再扩大样本。

主要指标：任务完成率、用户介入次数、首次有效修改时间、无关文件读取量、工具调用/轮询次数、请求/返回字节、token（宿主可提供时）、p50/p95 延迟、错误恢复成功率、LSP 常驻内存。预算与候选性能门槛在 M0 固定，避免事后挑选有利指标。

发布硬门槛是兼容与正确性：上述恢复/审批/过期证据夹具不允许数据损失或越权；既有关键任务无已知回归。只有模型评估提供证据后，才使用“更少调用”“更快完成”等效果表述，不预先承诺与 Codex 等价或提升某个百分比。

## 10. 范围取舍与下一步决策

建议采用：**Core + 可选 Workflow 的路线，先 M0/M1，再根据实测进入 M2/M3。** 将“更像工作模式”落实为更可靠的定位、修改、检查、恢复和可见进度。

暂缓：自建模型路由、完整聊天系统、自动上下文压缩、通用记忆库、向量数据库、插件市场、多人多租户、一次覆盖所有语言与系统，以及多 Agent 自动编排。它们不是首个开发闭环的必要条件。

进入开发前需要形成的具体决策为：增强版默认暴露策略、首批语言服务及安装方式、checkpoint 支持边界、worktree 连接方式、状态保留策略、桌面/CLI 审批通道。M0 用原型与宿主测试把这些选择固化；只有涉及产品范围或外部服务成本的取舍再提交用户决策。

当前实现已达到 M1–M3 的主要增强版闭环：默认 CLI 行为保持 51 个工具；使用 `--enable-workflow-tools` 可启用 53 个新增工具，桌面 App 启动的运行时默认启用，并显示最近任务、检查、恢复点、审查和待审批请求。检查证据会绑定执行前后代码指纹，任务上下文与有序 Plan 可在重启后恢复，Checkpoint 仍要求预览令牌且并发修改会冲突。Git 写流程要求调用者提交 `git_status` 返回的 HEAD/index 指纹，暂存与提交只接受显式路径，提交路径必须与真实暂存集合完全相同。LSP 支持按需连接 Python、TypeScript/JavaScript 与 Rust（rust-analyzer）服务，Rust 使用最近的 `Cargo.toml` 作为子项目根；返回 UTF-16 位置、后端来源和源码哈希，rename 只产生预览，不直接写文件。Review 保存 diff、规则和任务证据，并在代码改变后标记 stale。审批请求绑定完整工具参数、权限和过期时间，只能由本机桌面端决定，并在一次匹配执行前原子消费。受管 worktree 创建在私有状态目录，删除前强制检查干净状态并保留分支。浏览器方面，单调用采样已验证 console、page error、request failure、dialog 与 popup；`browser_download` 通过当前 tab 的 CDP 网络上下文分块读取到 runtime 私有目录，并可通过 `download_id` 直接交给 `browser_upload`；`browser_watch_start/poll/stop` 由独立线程持有 CDP 连接，使用有界事件缓冲、递增序号和丢失计数跨 MCP 调用观察事件。watch 在 Runtime 关闭时回收，不跨 Runtime 重启持久化。M4 已完成协议 Tasks 首版与 Rust LSP 这一小切片；Controller、clangd/DAP 和其他平台仍未实现。
