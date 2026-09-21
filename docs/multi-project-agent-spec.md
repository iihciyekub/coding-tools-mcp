# 单服务、多项目 Coding Agent：实施规格

日期：2026-09-21。起始提交：`4cc7951`。状态：核心源码与受限浏览器验证已实现，本机验收通过；未替换已安装 App，首次系统授权及发布验收仍独立待办。实际结果见第 12 节。

## 1. 产品目标

用户只启动一个 Coding Tools MCP 服务，使用现有聊天客户端，在工作区下的多个独立项目之间进行分析、编辑、测试、Git 审查和提交。项目可以是普通目录、独立 Git 仓库、嵌套仓库或 Git worktree。MCP 的价值是减少 agent 的错误和人工接力，不是扩大工具数量。

主闭环：明确项目 -> 读取适用规则 -> 定位代码 -> 修改 -> 在该项目运行检查 -> 读取对应证据 -> 审查正确仓库。

## 2. 本次交付与非目标

本次实施核心多项目正确性，并补充可独立验证的运行证据能力：

- 统一现有 Git 工具的仓库定位、路径转换和写入状态校验。
- 按目标目录实时读取项目指令，不依赖启动扫描是否完整。
- 项目概览与检查指纹先限定目录，再消耗扫描预算。
- 命令结果、恢复列表标明工作目录，支持按目录筛选。
- Python / TypeScript / Rust LSP 按项目选择根目录；诊断标明新鲜度。
- 对已实现的 App 观察/动作链路执行可用的自动化验收，区分真实系统权限和模拟后端证据。
- 提供可选的本地 Web 场景验证命令，复用现有 exec/read/image 工具，不增加 MCP 工具。
- 统一 Full Access 语义：host 模式下文件工具与命令都可使用主机文件系统；工作区只作为项目上下文、Git、LSP、检查与审查的稳定锚点，不再维护“常用目录”白名单。

不在此次核心改动中默认开启持续录屏、访问个人 Chrome 会话或自动发布版本。本次浏览器交付限定为第 10 节的本地场景验证命令，不是持久、交互式 Chrome MCP 控制。Full Access 的 host 文件权限属于用户明确选择的访问模式，不改变 Standard Access 的文件边界。

不新增全局 `current_project`、项目注册数据库、调度平台、向量数据库、多租户系统或另一套任务存储。不为普通分析强制创建任务和 worktree。不把截图或系统调用成功作为任务完成证据。

## 3. 已确认问题

1. `git_status` 从子目录读取状态，却在工作区根目录计算索引指纹。
2. `git_diff`、`git_log`、`git_show`、`git_blame` 和 Git 写操作混用工作区根目录与目标项目；子仓库可能报错或得到非 Git 空差异。
3. `project_instructions` 依赖启动时的嵌套指令清单；全局扫描被截断后，真实存在的规则可能遗漏。
4. 检查指纹先枚举全工作区，再过滤目标，其他项目消耗预算。
5. Python / TypeScript LSP 使用父工作区根目录；旧诊断可能在文件变化后被立即返回。
6. 命令恢复结果没有明确工作目录。

## 4. 路径和仓库契约

### 4.1 工作区与项目

工作区是服务的稳定操作边界，不等于某个 Git 仓库。内部新增不可变的仓库上下文：实际工作树根目录、Git 目录、共享 Git 目录及工作区根目录。每次操作独立解析，不改变进程 cwd，不修改 `Runtime.workspace`，不把客户端上一次选择作为默认值。

Git 使用 `rev-parse` 识别仓库/worktree，不假定 `.git` 必须是目录。仓库根目录必须位于配置的工作区中；不因为 host 文件访问范围较大而悄悄扩大 Git 的项目范围。现有受管 worktree 的创建/删除继续限定在受管存储内；任意外部路径不获隐式授权。

### 4.2 输入兼容

现有 Git 工具增加可选 `repo_path`，选择工作区中的目标仓库或其中的目录。`path` / `paths` 继续表示工作区相对路径，绝不因为加入 `repo_path` 就改成仓库相对路径。

例：工作区为 `/work`，仓库为 `/work/project-a`：

```json
{"repo_path":"project-a","paths":["project-a/src/app.py"]}
```

未给 `repo_path` 时：从明确的 `path` / `paths` 推导唯一所属仓库；没有路径则沿用工作区根目录。多个路径属于不同仓库时拒绝，提示拆成不同调用。显式 `repo_path` 与路径所属仓库不符时拒绝，不能静默忽略或去另一仓库执行。

删除文件仍可作为 diff / stage 路径；解析从其最近存在的父目录开始。文件参数以 literal pathspec 传给 Git，防止文件名中的通配符或 pathspec magic 扩大范围。

### 4.3 输出与回退

Git 结果返回 `repo_root`（绝对路径）和 `path_base`。原生 Git 输出中的文件名保持仓库相对路径，显式标明基准，模型不能直接把它当工作区相对路径。传给写工具时仍使用工作区相对路径。分页 `next_action` 必须保留 `repo_path`。

默认非 Git 工作区原有的补丁差异回退保留兼容，但明确返回 `is_repo=false`、`diff_source=patch_baseline`；显式指定仓库失败不得回退。真正 Git 差异返回 `diff_source=git`。空差异不能掩盖选错仓库。

### 4.4 写操作和并发

HEAD、索引指纹、暂存集合和写操作必须来自同一工作树。短暂的状态检查与写入在同一仓库协调范围内执行；不锁住多轮 agent 任务。不同仓库可独立进行。Git 自身的锁仍生效，不能宣称可阻止外部进程在任意时间写入。

拒绝跨仓库暂存和提交，提交继续要求暂存集合精确匹配声明路径。受管 worktree 按所属仓库区分目录，防止两个仓库使用相同 worktree_id 时串用；删除前检查真实 Git 归属和干净状态。

同一工作副本不推荐并行写入；需要并行开发时复用现有 worktree。分析不自动建 worktree。

## 5. 项目规则和扫描

`project_instructions(path)` 从工作区到目标目录逐级查找已支持的规则文件名，每次读取当前磁盘内容。保留父级规则，按浅到深排序，按文件身份去重大小写别名；拒绝越界符号链接。单文件及总输出均有预算，截断必须明确报告。启动时的全局发现仅用于提示，不能作为目标规则查找的唯一数据源。

`workspace_overview` 增加可选 `path`，默认 `.` 保持兼容；列表中的文件路径仍以工作区为基准。明确目标后的概览、检查指纹只扫描目标树，遵守 ignore 规则，不遍历 `node_modules` / `.venv` 等无关树后才丢弃结果。

指纹包含目标标识；排序稳定；删改、新文件会改变指纹；越界符号链接不读取其内容；预算耗尽、嵌套仓库未完整覆盖等情形不能报告完整证据。查询某项目时，兄弟项目变化不使其证据失效。

## 6. 运行环境和命令归属

`exec_command` 继续显式接受 `workdir`，不运行 shell profile 或自动执行项目环境脚本。`exec_command` / `get_command` / `list_commands` / 后续轮询返回实际的 `workdir`；命令内容和环境秘密不因归属信息而额外泄露。

`list_commands` 增加可选工作目录筛选，按规范化后的实际目录匹配。`operation_id` 仍复用现有去重机制；跨目录重复使用同一 ID 且参数不同应保持冲突，不猜测用户意图。

Python / TypeScript LSP 在目标向上查找语言配置，最迟在最近仓库边界停止，避免借用兄弟项目配置。Rust 保留 Cargo 根识别。按 `(language, project_root)` 复用后端；不为每次查询启动新进程。

仅改变根目录不保证所有后端自动选对解释器；项目已有语言服务器配置优先，不自动安装依赖或全局修改 PATH。缺失工具应明确报告，不把其他项目的环境伪装成当前项目环境。

## 7. 诊断新鲜度

文档版本使用按文档单调递增的整数。文件变化后旧诊断不能被当作最新结果；等待按 URI 和版本隔离。显式返回文档版本、诊断版本和 `freshness`。

- `fresh`：语言服务器明确给出了与请求文档相同版本的诊断。
- `unversioned`：收到了未附版本的诊断，无法严格证明属于当前版本。
- `pending`：当前版本尚无可确认结果。
- `stale`：存在旧版本证据或查询期间文件又发生变化。

无最新诊断不等于没有错误。保留现有诊断列表字段，新增元数据并在模型可读文本中说明未确认状态。等待超时返回有界结果，不无限重试。

## 8. 代码组织与兼容

共享仓库定位放在核心 Python 子树，桌面端不复制实现。现有工具数、CLI 名称、权限模式和默认发现模式保持不变；只扩展必要参数和结果字段。沿用现有错误外壳、命令管理、工作流存储和 LSP 管理器。

协议契约、工具说明和测试随实现更新。新增错误码必须纳入运行时契约。不得用批量格式化掩盖功能 diff，也不重写不相关桌面文件。

## 9. 截图和 App 验证：第 2 阶段

复用现有 `app_snapshot`、`app_action`、`app_wait` 和授权会话。先完成目标窗口观察、可访问控件操作、再观察的闭环。

屏幕捕获权限不等于持续录像。默认不开麦克风、不录音、不连续上传桌面。静态布局问题用按需窗口截图；只有实际遇到动画/拖动问题才考虑有界过程证据。

当前动作仅包含可访问元素的 press / set_value。不能声称已经覆盖画布笔画、全局快捷键和任意拖动。不得操作 MCP 自身授权 UI 来扩大权限。

验收分开记录：模拟后端 HTTP 协议测试、原生测试夹具、签名安装后的首次系统授权、实际聊天客户端图像消费。未执行项明确保留未验收，不以其中一种结果代替其他结果。

## 10. 浏览器：第 3 阶段，限制范围

目标仅是 Web 编码验证，不是管理用户全部浏览器活动。本轮选择可选 Playwright Python 驱动，通过 `python -m coding_tools_mcp.browser_check` 执行有限 JSON 场景。agent 使用已有 `exec_command` 运行、`read_file` 读取结果、`view_image` 查看截图；不增加 MCP 注册工具、另一个常驻服务或个人浏览器连接。

已实现的最小能力：明确的本地 HTTP URL，页面可访问性结构，基于 role/name、label 或 test_id 的 click/fill/press/wait，按需截图，控制台和页面错误、失败请求以及 HTTP 4xx/5xx 证据。每次新建无持久配置的 headless Chromium context，不读取日常登录标签页；依赖安装必须显式执行，缺失时返回指引而不自动安装。

网络限制为用户指定的 HTTP loopback origin，端口也是身份的一部分；其他项目的本地端口同样拒绝。WebSocket 使用对应 ws origin，service worker 关闭。真实测试发现普通路由无法约束原生重定向链，因此首版使用 `route.fetch(max_redirects=0)` 检查响应，拒绝所有 HTTP 重定向（包含同源重定向）；用户应提供最终开发地址。跨 origin 的 API/CDN 可通过开发代理接入，不静默放宽访问。此处是浏览器请求约束，不是操作系统网络沙箱。

可选依赖最低版本为 Playwright 1.58。为了当前 Chromium 的本地 WebSocket，runner 仅在临时浏览器上下文对明确指定的 origin 设置 `local-network-access`；HTTP/WebSocket 路由仍拒绝其他端口和主机。没有修改操作系统授权、个人 Chrome 设置，也没有关闭浏览器的全局安全检查。真实测试同时验证同源 WebSocket 转发和跨项目 WebSocket 拒绝。

每次运行生成独立结果目录，包含有界报告、页面结构和可用的最终截图。报告标明目标 URL、实际 workdir、步骤结果、清理结果与证据路径。请求头、Cookie 和响应正文不采集；URL 凭据、查询和 fragment 从报告移除；这不是全面敏感信息防泄漏，截图和页面文字仍可能含主动提供的测试数据。完整输入上限、事件上限、超时、错误码和例子统一维护在 [browser-verification.md](browser-verification.md)。

验收已覆盖两个一次性本地开发服务、保存成功、HTTP 500 失败证据、跨项目端口阻断、重定向阻断、总超时，以及截图通过现有 MCP image 工具回到本次对话。没有场景时仅表示有限页面观察，不代表业务流程通过。成功点击不等于保存成功，场景应等待可观察的业务结果。

本轮不暴露跨调用页面句柄，因此不宣称实现持久页面恢复、交互式 Chrome 管理、用户日常标签页访问、任意 JS/坐标操作等能力。遗留 browser/chrome Schema 仍不代表实现。Web 测试不能代替 Tauri/WKWebView 的原生验收；后续只有真实任务证明场景式验证不足，才考虑按需 browser gateway。

## 11. 验收矩阵

| 编号 | 场景 | 必须满足 |
| --- | --- | --- |
| G1 | 父目录非 Git，A / B 为独立仓库 | status/diff/log/show/blame 返回各自内容和根目录 |
| G2 | 父目录也是仓库，包含独立子仓库 | 目标不会回落到父仓库 |
| G3 | A、B 包含同名文件、相同 HEAD 或相同索引内容 | 不以内容相同推断仓库相同 |
| G4 | 在 A 暂存、取消暂存、提交 | B 的 HEAD / index / 工作树不变 |
| G5 | 显式仓库与文件冲突或多仓库 paths | 拒绝；不部分写入 |
| G6 | linked worktree、相同 worktree_id、脏 worktree | 归属准确；不删除错误或不干净的工作树 |
| G7 | 中文、空格、箭头、换行、通配符样式文件名 | literal 路径正确；status 采用 NUL 分隔解析 |
| G8 | 非 Git 默认回退与显式错误仓库 | 返回状态清楚，不报告假 Git 空差异 |
| P1 | 启动规则清单为空/被截断，目标规则存在 | 实时返回正确规则 |
| P2 | 服务启动后新增或修改规则 | 无需重启即可读取；兄弟项目规则不混入 |
| P3 | 目标小项目、兄弟大目录、嵌套仓库 | 目标扫描预算独立；完整性如实报告 |
| C1 | 两项目交错运行和重连 | 输出引用稳定；workdir 明确；筛选准确 |
| L1 | Python / TypeScript / Rust 不同项目 | 不借用父工作区或兄弟项目配置 |
| L2 | 文件变化、旧版诊断迟到、其他 URI 通知 | 不误标 fresh，不被无关事件提前唤醒 |
| R1 | review_prepare 目标项目 | diff/status/rules/fingerprint 同范围 |
| B1 | 原有协议和功能回归 | 默认目录数量不变，既有测试不退化 |
| B2 | 有界本地 Web 场景 | 成功/失败均有证据；不得访问另一个 origin 或跟随重定向 |
| B3 | 一次性原生 App 夹具 | 窗口截图、赋值、点击、后台状态、回执重放和停止均验证 |

所有破坏性验收只在临时夹具中进行，不对用户其他项目提交、删除或重置。真实项目只做只读验收。

## 12. 发布与执行记录

不自动 commit / push / 打 tag / 替换正在使用的已安装 App。源码验证使用新建的测试 Runtime，避免把已启动旧进程的工具表误认为新版行为。最终明确说明当前连接是否仍需要重启才能加载源码。

- 起始状态：`iiaide`，`4cc7951`，工作区干净。
- 实现状态：多项目核心、受限 Web 验证与本地验收完成；同步出现的核心实现变更已保留并共同验证，未另写一套重复的仓库解析器。
- 本轮未 commit、push、打 tag、重启当前服务或替换已安装 App；没有对用户其他项目执行写入、提交或清理。

### 12.1 本机执行结果（macOS，2026-09-21）

| 检查 | 实际结果 |
| --- | --- |
| 多项目定向测试 `tests.test_multi_project` | 26 项通过；与原有 workflow 合计 43 项通过，7.390 秒 |
| 浏览器验证 `tests.test_browser_check`，启用真实 Chromium | 11 项通过，3.944 秒，其中 7 项使用真实 Chromium、4 项输入/边界测试 |
| 完整 Python discovery，包含上述真实浏览器测试 | 354 项，350 通过、4 项跳过，无失败，51.302 秒 |
| Full Access 文件语义 | host 模式下 read/list/search/apply_patch 可直接操作工作区外绝对路径，明确选择隐藏配置目录（如 `.codex`）后路径证据保持绝对；dangerous 模式仍拒绝工作区外文件 |
| Ruff | 项目既有 lint 配置通过；修改的原生 Python 夹具单独通过 |
| mypy | 项目既有配置通过，检查 31 个源文件；不等同于启用了全部严格检查 |
| dispatch inputs | Worker / workflow_dispatch / workflow_call 均为 9 项，门禁通过 |
| `git diff --check` | 通过 |
| `uv lock --check --offline` | 通过，锁文件与可选依赖约束一致 |
| Desktop TypeScript | 6 项测试通过；production Vite build 通过 |
| Desktop Rust | `cargo fmt --check`、Clippy 通过；46 项通过、3 项下载型测试忽略 |
| 原生 App helper 实际夹具 | 截图、set_value、press、后台状态、回执重放与 stop 通过 |
| 实际项目只读验收 | coding-tools-mcp 与 wosaide 返回各自 repo_root、分支和 AGENTS.md；coding-tools-mcp diff_source 为 git 且包含实际修改 |
| MCP 目录 | 注册表仍为 79 项；本轮没有新增 MCP 工具 |

最新完整复验日志：`/tmp/ctm-full-access-full-20260921.log`（354 项完整 Python 测试）。此前多项目/浏览器/App 验收日志继续保留为 `/tmp/ctm-final-core-f6a92.log`、`/tmp/ctm-browser-acceptance-v2-f6a92.log`、`/tmp/ctm-native-smoke-f6a92.log`。这些是本机临时日志，不随包分发。最终测试包含了补充的 CRLF 文档新鲜度、宿主 Git 环境变量防串仓库、模型可读文本中的工作目录/规则截断提示，以及 Full Access 文件工具 host-filesystem 边界。

另一次独立完整复验记录于 `/tmp/ctm-final-stable-snapshot-20260921.log`：353 项、4 跳过、无失败，51.719 秒。此次在测试前后计算核心 Python 模块与测试源码的聚合 SHA-256，两次均为 `876907cefef495f021c4b2c72e96ce550ef7766671df15896e67bf66f3ac19b5`，确认执行期间该源码集合未变化；不是仓库提交 ID，也不是运行时的项目状态指纹。

完整测试使用项目 `.venv`，规范化 PATH 与测试服务名，避免继承桌面连接名称影响契约断言：

```bash
PATH="$PWD/.venv/bin:$PATH" \
CODING_TOOLS_MCP_SERVER_NAME=coding-tools-mcp \
CODING_TOOLS_MCP_TELEMETRY=off \
CODING_TOOLS_MCP_BROWSER_SMOKE=1 \
PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'

make lint typecheck check-dispatch-inputs PYTHON=.venv/bin/python
.venv/bin/python -m ruff check --ignore E501 apps/desktop-client/tests/computer_smoke.py
.venv/bin/python apps/desktop-client/tests/computer_smoke.py
```

本轮在项目 `.venv` 显式安装 Playwright 1.63.0 及依赖，并在 Playwright 缓存安装其测试 Chromium；没有修改个人 Chrome profile。依赖选择记录在 `pyproject.toml` 的可选 browser extra 和 `uv.lock`，核心运行时不要求安装浏览器依赖。

原生测试初次失败暴露的是夹具身份与就绪时序：从桌面服务启动的无 bundle 夹具继承了管理 App 的 `__CFBundleIdentifier`，触发正确的受保护应用拒绝。测试现改为独立临时 `.app` 身份，移除夹具子进程继承值，且验证 bundle ID。AX 与 ScreenCaptureKit 列表短暂不同步时，测试仅在有限启动窗口重新观察自己的窗口。不修改产品受保护应用名单、截图匹配规则或授权逻辑。

一次保留的浏览器证据位于 `$HOME/Library/Caches/coding-tools-mcp/browser-acceptance/browser-check-vyqznt6e/`。`screenshot.png` 已通过现有 MCP `view_image` 实际返回本次对话：输入框显示 `MCP acceptance`，页面显示 `Saved`。该记录证明浏览器图像传递，不替代直接 computer 工具的完整聊天客户端验收。

### 12.2 明确保留的验收边界

本次真实 App 夹具使用已有 macOS Accessibility / Screen Recording 权限；没有重新测试签名安装后首次拒绝/授权/升级归属。Windows/Linux 没有在本机执行平台回归。Tauri/WKWebView 的完整产品 UI 和任意绘图/拖动/全局快捷键不属于本轮完成声明。

当前在线连接仍是先前已启动的安装运行时。使用本轮核心修复需重新构建/更新该 App 实际使用的运行时，再重启相应服务并刷新客户端工具定义；只重启旧安装包不保证加载工作区源码。本轮没有擅自断开正在使用的连接。

## 13. 外部参考

- Git rev-parse（仓库与 worktree 定位）：https://git-scm.com/docs/git-rev-parse
- LSP 3.17（文档版本及 publishDiagnostics）：https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/
- Chrome DevTools MCP（成熟浏览器驱动候选，不表示本项目已集成）：https://github.com/ChromeDevTools/chrome-devtools-mcp
- Playwright Python（本轮场景验证采用的驱动）：https://playwright.dev/python/docs/library
- Playwright BrowserContext（隔离与路由）：https://playwright.dev/python/docs/api/class-browsercontext
- Playwright WebSocketRoute：https://playwright.dev/python/docs/api/class-websocketroute

参考核对日期：2026-09-21。具体交付事实以本仓库实现及本节执行记录为准。
