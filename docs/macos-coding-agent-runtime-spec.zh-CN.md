# Coding Tools MCP — macOS Coding Agent Runtime 规格

日期：2026-09-21  
状态：第一轮已实现 / Active Implementation Spec  
目标版本：0.4.x 后续迭代  
平台策略：macOS-first，macOS 为唯一正式支持和验收平台

## 1. 背景

Coding Tools MCP 已经完成从通用 Desktop Automation 向 Coding Runtime 的收敛：

- 已移除 App / Computer Control；
- 已保留 Files / Shell / Git / Worktree / LSP / Checks / Task / Context Checkpoint；
- 已加入 Agent Environment metadata discovery；
- 已加入命令 activity health、Context Checkpoint drift/resume；
- 已加入 `repo_map(impact=true)` 的 Change Impact Analysis；
- 已加入 `checks_run` / `checks_result` 的 Structured Failure Diagnostics；
- 当前公共 MCP 工具数量保持 71 个。

后续目标不是增加更多 MCP 工具，而是让已有工具在 macOS 编码场景中更有语义、更稳定、更节省上下文。

本规格结合以下方向作为能力标杆，而不是依赖或复制它们：

- Serena / Language Server 类工具：语义导航和安全重构；
- XcodeBuildMCP：Xcode / Swift / macOS/iOS 构建测试能力；
- GitHub MCP Server：远程 GitHub 平台能力；
- Context7：第三方依赖的当前版本文档；
- Playwright：Web 编码验证；
- CLI-first / Skill-first 工具：减少 MCP schema 常驻上下文。

## 2. 产品定位

Coding Tools MCP 是：

> 给 Codex、ChatGPT Web 和其它 MCP 客户端提供本机 macOS 软件工程能力的稳定 Runtime。

它负责：

- 本机代码理解；
- 本机文件和命令执行；
- Git / Worktree；
- 构建、测试、静态检查；
- macOS/Xcode/Swift 工程状态；
- 任务状态和跨会话恢复；
- 本机 Agent/Skill metadata discovery；
- Release、签名、公证等本机工程流程。

它不负责：

- 通用 GUI 自动化；
- 鼠标/键盘/Accessibility 控制；
- 远程 GitHub API 的二次封装；
- 第三方 library 文档数据库；
- 通用浏览器 Agent；
- 调用第二个模型替当前模型思考。

## 3. 平台支持策略

### 3.1 正式支持

只正式支持：

- macOS；
- Apple Silicon 为主要测试目标；
- Intel Mac 若现有代码自然可运行则保留，不为其增加独立抽象；
- Python 3.11+；
- Git；
- 可选 Xcode / Command Line Tools；
- 可选 Homebrew；
- 可选语言服务器。

### 3.2 Windows / Linux

0.4.x 后续不再把 Windows/Linux 作为产品兼容目标：

- 不为 Windows 增加新兼容逻辑；
- 不为 Windows 保持独立 CI gate；
- 不因为跨平台抽象降低 macOS 能力；
- 已有天然跨平台的纯 Python/Git/Shell 代码无需主动删除；
- 文档明确标注 macOS-first。

## 4. 设计原则

### P1. 工具数量不是能力指标

默认不新增 MCP tool。新能力优先：

1. 增强现有参数；
2. 增强现有返回结构；
3. 增强内部 provider/parser；
4. 使用 Skill/CLI；
5. 只有无法自然归属时才新增 tool。

### P2. 统一入口优于 provider-specific tool

不增加：

- `swift_*`；
- `xcode_*`；
- `codex_*`；
- `github_*`；
- `playwright_*`。

现有工具应吸收能力：

- `workspace_overview`；
- `repo_map`；
- `code_definition`；
- `code_references`；
- `checks_discover`；
- `checks_run`；
- `checks_result`；
- `runtime_doctor`；
- `agent_environment`。

### P3. 语义优先，文本 fallback

有可用 LSP 时：

- definition / references 优先 LSP；
- fallback 到现有 AST/pattern/text search；
- 返回结果必须说明 backend 和 coverage；
- LSP 失败不能让基础代码导航完全失效。

### P4. CLI-first for macOS

macOS 专项能力优先复用：

- `xcodebuild`；
- `xcrun`；
- `swift`；
- `clang`；
- `codesign`；
- `xcrun notarytool`；
- `xcrun xcresulttool`；
- `simctl`（只作为工程 CLI，不做 UI 自动化）；
- Homebrew。

### P5. Raw evidence remains authoritative

Structured Diagnostics、Impact Analysis、LSP fallback 都必须：

- 标明 heuristic / backend；
- 不隐藏 raw output / raw reference；
- 不把启发式判断伪装成完整编译器事实。

## 5. Phase A — Semantic Code Intelligence Unification

### 5.1 目标

减少模型在以下工具之间自行猜测：

- `code_definition` vs `lsp_definition`；
- `code_references` vs `lsp_references`。

默认编码路径应该更简单：

```text
code_definition
  -> LSP available? use LSP
  -> otherwise code_intel fallback

code_references
  -> LSP available? use LSP
  -> otherwise code_intel fallback
```

独立 `lsp_*` 工具暂时保留，作为显式调试/精确控制接口，不做 breaking removal。

### 5.2 `code_definition` 增强

新增可选参数：

```text
prefer_lsp: boolean = true
```

行为：

- path/line/column 足够时尝试 LSP definition；
- LSP 不可用、超时、server error 时自动 fallback；
- 不吞掉安全/路径类错误；
- 返回：

```text
backend: "lsp" | "syntax" | "text"
fallback_used: boolean
fallback_reason?: string
coverage: string
```

### 5.3 `code_references` 增强

新增：

```text
prefer_lsp: boolean = true
```

规则同 definition。

结果统一为 workspace-relative path + line + column，避免调用方再适配不同 provider。

### 5.4 `lsp_rename_preview` 增强

不新增 rename apply tool。

Preview 增加：

- affected file count；
- edit count；
- current content fingerprints；
- patch-ready preview；
- unsupported document changes 明确 fail closed。

真正写入继续使用现有 `apply_patch` / guarded write primitives。

### 5.5 Swift LSP

macOS-first 后，LSP 应支持 Swift：

- 后缀 `.swift`；
- backend：`sourcekit-lsp`；
- 优先查 PATH；
- 若不在 PATH，可通过 `xcrun --find sourcekit-lsp` 发现；
- project root marker：`Package.swift`、`.xcodeproj`、`.xcworkspace`；
- 不主动启动 Xcode GUI。

## 6. Phase B — macOS / Xcode Coding Intelligence

### 6.1 `workspace_overview` 增强

识别并返回：

```text
apple:
  xcodeproj[]
  xcworkspace[]
  swift_packages[]
  xcode_available
  xcode_version
  developer_dir
  swift_available
  swift_version
  sdk_summary
```

要求：

- bounded；
- metadata-only；
- 工具缺失时返回 unavailable，不报错；
- 不启动 GUI。

### 6.2 `checks_discover` 增强

在已有 manifest-driven checks 基础上发现：

#### SwiftPM

存在 `Package.swift`：

- `swift build`；
- `swift test`。

#### Xcode

存在 `.xcodeproj` / `.xcworkspace` 且 `xcodebuild` 可用：

第一阶段只增加低复杂度的通用检查：

- `xcodebuild -list` metadata check；
- 若能无歧义识别唯一 scheme，则生成 build/test check；
- 多 scheme 时不猜默认 scheme；
- 不引入复杂 simulator/device 自动选择。

### 6.3 Structured Diagnostics 增强

现有 `check_diagnostics` 增加：

- `swiftc`；
- `clang`；
- `xcodebuild`；
- XCTest failing tests；
- codesign；
- notarytool；
- xcresulttool 的高信号失败摘要。

仍保持：

```text
raw_output_authoritative = true
```

### 6.4 `runtime_doctor` 增强

host/macOS 模式报告：

- macOS version；
- architecture；
- Xcode presence/version；
- Command Line Tools；
- selected developer dir；
- Swift；
- SourceKit-LSP；
- codesign；
- notarytool；
- Homebrew；
- Git；
- active LSP backend availability。

不读取 Apple ID、keychain secret、API key 内容。

## 7. Phase C — MCP Tool Surface Budget

### 7.1 目标

防止 MCP 工具面重新膨胀。

这是 CI / developer tooling，不新增 MCP tool。

新增脚本：

```text
scripts/check_tool_surface_budget.py
```

输出：

- registered tool count；
- directly exposed count by mode；
- deferred workflow count；
- total JSON schema bytes；
- per-tool schema bytes；
- top N largest tools；
- estimated token footprint（仅作为近似值）；
- budget pass/fail。

### 7.2 初始预算

第一版简单、可解释：

```text
registered_tools <= 75
single_tool_schema_bytes <= 4 KiB
total_schema_bytes <= 30,000 bytes（当前基线约 25.7 KiB）
```

不依赖 tokenizer package；token estimate 使用稳定近似：

```text
ceil(utf8_bytes / 4)
```

CI 真正 gate 使用 bytes/tool count，token estimate 只用于报告。

### 7.3 Make / CI

新增：

```text
make check-tool-surface
```

加入 macOS 主 CI gate。

## 8. CI 平台策略调整

### 8.1 删除 Windows gate

删除：

```text
windows-msvc-smoke
```

原因：

- 产品策略明确 macOS-first；
- 不再为 Windows 环境继承维护额外代码/测试；
- 减少 CI 和兼容负担。

### 8.2 macOS 主 Gate

完整通用 Runtime compliance 保持在 Ubuntu runner，以保证协议、文件、Git、
命令、workflow 等大量可移植测试的稳定性。另设独立 `macos-15` Runtime smoke：

- metadata-only Apple toolchain probe；
- Swift / SourceKit-LSP discovery；
- Apple workspace metadata；
- Apple structured diagnostics；
- runtime doctor Apple metadata。

Desktop build/sign/notarize/release workflow 继续以 macOS runner 为权威发布 Gate。
这样 macOS 仍是正式产品平台，同时避免把全部可移植单测绑定到 hosted-macOS
runner 的进程/PTY/网络行为。

## 9. 外部 MCP / Agent 边界

### 9.1 GitHub MCP Server

不复制。

职责：

- PR；
- Issues；
- Actions；
- GitHub Security；
- Releases；
- remote repo API。

CTM 继续负责本地 Git/worktree。

### 9.2 Context7

不复制。

第三方 library 当前文档由 Context7 或 Web 负责。

### 9.3 Playwright

不做常驻浏览器 MCP toolset。

复杂 Web 验证走：

- Playwright CLI / Skill；
- 当前 `browser_check`；
- existing shell/file/image primitives。

### 9.4 XcodeBuildMCP

不内嵌、不代理整套 server。

吸收其值得保留的能力原则：

- CLI-first；
- Xcode/Swift 工程识别；
- structured build/test diagnostics；
- Skill metadata 可由 `agent_environment` 发现。

## 10. 安全边界

### S1

Semantic LSP fallback 只能降低语义精度，不能扩大权限。

### S2

Xcode/Swift discovery 只读 metadata，不修改工程。

### S3

checks 仍走现有 command permission engine。

### S4

不读取 Keychain、Apple ID、App Store Connect API key 内容。

### S5

不因为 macOS-first 重新引入 Accessibility / Screen Recording 权限。

### S6

Simulator CLI 未来若支持，也只作为 explicit command/check，不自动操作用户 UI。

## 11. 性能约束

### Semantic routing

- LSP 单次 request 继续沿用现有 timeout；
- fallback 不应触发二次全仓扫描，除非原本 fallback 本身需要；
- multi-symbol impact 继续保持 one-pass reference scan。

### Xcode discovery

- `workspace_overview` 不运行 build；
- metadata probe 单条命令默认 <= 5 秒；
- `xcodebuild -list` 只在显式 checks discovery / Apple workspace 中运行；
- 不扫描 DerivedData。

### Diagnostics

- structured diagnostics bounded；
- 不把完整 xcresult JSON 塞进 MCP response；
- 大结果返回摘要 + raw output/artifact reference。

## 12. 测试计划

### Unit

- LSP preferred / fallback；
- Swift sourcekit-lsp discovery；
- workspace Apple metadata；
- SwiftPM check discovery；
- ambiguous Xcode scheme 不猜；
- swiftc/clang/xcodebuild diagnostics；
- tool surface budget pass/fail。

### Integration

macOS runner：

- `xcrun --find sourcekit-lsp`；
- synthetic SwiftPM fixture build/test；
- synthetic Xcode project metadata（若 CI 环境稳定）；
- `runtime_doctor` Apple environment。

### Regression

- 71-tool baseline不因实现阶段随意增长；
- existing Python/TS/Rust LSP tests；
- existing Checks / Git / Workflow tests；
- schema/docs compliance；
- full Python tests；
- Desktop tests。

## 13. 实施顺序

### Phase A1

- Swift LSP backend discovery；
- `code_definition` LSP-preferred fallback；
- `code_references` LSP-preferred fallback。

### Phase A2

- rename preview 结构增强。

### Phase B1

- Apple metadata in `workspace_overview` / `runtime_doctor`。

### Phase B2

- SwiftPM checks；
- minimal Xcode checks；
- Apple diagnostics parser。

### Phase C

- tool surface budget script；
- Make target；
- CI gate；
- 删除 Windows-only CI gate。

## 14. Definition of Done

本规格第一轮完成必须满足：

1. 不新增公共 MCP tool；
2. `code_definition` / `code_references` 可优先 LSP 并自动 fallback；
3. Swift 可通过 SourceKit-LSP 进入统一语义路径；
4. `workspace_overview` 能识别 Apple/Swift 工程 metadata；
5. `checks_discover` 能发现 SwiftPM checks；
6. structured diagnostics 能解析 Swift/Clang/Xcode 高信号错误；
7. `runtime_doctor` 能报告 macOS/Xcode/Swift 本机状态；
8. CI 有 tool surface budget；
9. Windows 专属 CI gate 被移除；
10. 所有新增判断都 bounded、可解释、fail-soft；
11. raw command/log evidence 仍然可用；
12. schema/docs/full regression 全部通过。

## 15. 明确暂缓

第一轮不做：

- Simulator 自动选择；
- iOS device 自动部署；
- LLDB MCP tool；
- xcresult 完整对象模型；
- Xcode project 自动修改；
- GitHub API；
- Context7 代理；
- Playwright toolset；
- App/GUI Control；
- Windows compatibility work。

只有在真实编码任务证明收益明显时，才进入下一轮设计。

## 16. 第一轮实施结果

截至 2026-09-21，本规格 Phase A / B / C 第一轮已落地：

- `code_definition` / `code_references` 支持显式位置下 LSP-first、运行时故障 fallback；
- Swift 接入 SourceKit-LSP，支持 PATH 与 `xcrun --find sourcekit-lsp`；
- `lsp_rename_preview` 返回 affected paths、文件 SHA-256 与 guarded patch plan；
- `workspace_overview` 返回 Apple/Xcode/Swift metadata；
- `runtime_doctor` 返回本机 macOS/Xcode/Swift/SourceKit/codesign/notarytool 状态；
- `checks_discover` 发现 SwiftPM build/test 与保守的 `xcodebuild -list -json` metadata check；
- Structured Diagnostics 支持 Swift/Clang/XCTest/Xcode/codesign/notarytool 高信号错误；
- `scripts/check_tool_surface_budget.py` 和 `make check-tool-surface` 已加入 CI；
- 完整 Runtime compliance 使用稳定的 Ubuntu runner，并有独立 macOS 15 Runtime smoke；Desktop CI/release 继续以 macOS 为权威 runner；
- Windows 专属 MSVC compliance gate 已移除；当前 Desktop release source 已通过 `releasePlatforms: ["macos"]` 选择 macOS 发布；
- 公共 MCP tool 总数仍为 71。

真实本机 smoke 已确认：Apple Silicon、Xcode、Swift、SourceKit-LSP、codesign、notarytool、xcresulttool 与 Homebrew 可被 metadata-only 探测；临时 SwiftPM 项目的统一 `code_definition` 实际通过 SourceKit-LSP 返回语义 definition。

## 17. MCP Surface 减法审计

第一轮实现后对当前 71 个 live tools 再做边界审计，结论如下：

### 保留为 Coding Runtime Core

- Files / Search / Patch / Image evidence；
- Shell / Process / command health；
- Git / Worktree；
- Code symbols / semantic LSP / SourceKit-LSP；
- Repo Map / Change Impact；
- Checks / Structured Diagnostics；
- Workspace / project instructions；
- Tasks / file checkpoints / context checkpoints / review evidence；
- Skills / Agent Environment metadata；
- Runtime doctor / permissions / approvals；
- Tool search / deferred invocation。

这些能力都直接依赖本机 workspace、运行时状态、权限模型或编码证据，放在 CTM 内部比让外部 MCP 代理更自然。

### 保留但不扩张

- `browser_check`：仅作为本地 Web 项目显式验证 CLI，不升级为常驻 Browser MCP toolset；
- Docker sandbox：只用于不可信代码的本地一次性隔离；
- Desktop / Remote MCP tunnel：作为 ChatGPT Web 连接本机 CTM 的 transport/infrastructure，不变成通用网络代理。

### 从当前产品面移除

- Cloudflare Worker + GitHub Actions cloud-sandbox control plane；
- 已经不注册的 Browser/Chrome automation schema；
- v0.4 当前合同中的旧 Browser/Chrome API 细节。

历史 v0.3 contract 与 CHANGELOG 继续保留对应历史事实。

### 明确交给并列外部 MCP / Plugin

- GitHub 平台 API → GitHub MCP；
- 第三方 library 当前文档 → Context7 / Web；
- 完整浏览器自动化 → Playwright；
- 高阶 Xcode Simulator/device/LLDB automation → XcodeBuildMCP / Skill；
- Figma、数据库、Slack/Notion/Jira、云厂商等领域能力 → 各自独立 MCP/Plugin。

CTM 不做代理 Hub。宿主模型应把 CTM 与这些外部 MCP 平级连接。

## 18. Agent Efficiency — Targeted Checks

在不增加 MCP tool、也不生成动态命令的前提下，`checks_discover` 增加轻量的
Targeted Checks 推荐层：

- 默认读取当前 Git changed paths；
- 调用方可显式传 `changed_paths`；
- 可用 `recommend=false` 完全关闭推荐；
- Python 改动优先 Python lint/typecheck/test；
- JavaScript/TypeScript 改动优先 npm checks；
- Rust / Go / Swift 分别优先 Cargo / Go / SwiftPM；
- Xcode 工程 metadata 变化才优先 `xcode:list`；
- Make checks 仅作为跨语言 fallback；
- 只改文档时不强推全量代码检查。

返回只给已有 check 增加 `recommended`、`priority`、`recommendation_score` 与
`recommendation_reasons`，并提供有序 `recommended_check_ids`。它不修改
`checks_run` 的执行合同、不绕过权限、不自动执行任何命令。

### 增量索引决策

当前真实 CTM 仓库基准（3 次调用中位数）：

- `repo_map`：约 185 ms；
- `repo_map(impact=true)`：约 395 ms。

因此第一轮明确**不引入**持久 symbol/reference index、后台 watcher 或增量缓存。
当前耗时不足以证明这些复杂度有收益。只有后续真实大型仓库基准稳定超过可接受
交互延迟，再以数据驱动方式设计缓存；不能为了理论上的 monorepo 场景提前增加
状态一致性、失效和磁盘索引维护成本。
