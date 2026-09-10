**Coding Tools MCP：ChatGPT 网页插件稳定性审查**

审查日期：2026-09-08。原始审查基线：0.3.3；Git：`2267e09`。本报告已在同日补充 0.3.4 修复结果。范围：核心 Python runtime、HTTP/stdio 协议、命令与输出生命周期、OAuth、浏览器与代码索引工具，以及桌面客户端的启动/健康状态逻辑。

结论：0.3.3 审查确认的 5 个可复现稳定性缺陷，以及网页长会话中最关键的 OAuth 跨重启、执行去重、任务找回、只读状态查询、模型文本 renderer、稳定 browser tab ID 和 HTTP 资源边界，已在 0.3.4 中落地。当前剩余重点转为真实 ChatGPT 公网链路验收、refresh token/撤销、磁盘输出 spool、desktop authenticated health probe，以及更细的输入/结果预算。

**0.3.4 修复结果**

| 项目 | 0.3.4 状态 | 验收结果 |
| --- | --- | --- |
| Browser JavaScript deadline | 已修复 | `browser_evaluate` 使用 CDP `Runtime.evaluate` 的执行 timeout，并把执行超时映射为 `BROWSER_TIMEOUT`；定向回归通过。 |
| 后台子进程/进程组回收 | 已修复 | runtime 显式记录是否拥有独立进程组；父 shell 已退出时仍检查并回收残留进程组；关闭路径覆盖 active 与 retained command。 |
| 大输出后的 poll cursor | 已修复 | 新输出判断统一使用 `stdout_total_bytes/stderr_total_bytes` 与 absolute cursor；滚动 buffer 后不再因 `len(buffer)` 与累计 offset 混用而空等。 |
| UTF-8 输出分页 | 已修复 | `read_output` 在字符边界切页，同时保持 absolute byte offset；中文/emoji 小页回归通过。 |
| code intel 扫描上限 | 已修复 | 达到 `max_files` 返回 `truncated=true`、`truncated_by=max_files`、`scan_complete=false`；symbols/definition/references 均覆盖。 |
| OAuth DCR 跨重启 | 已修复 | 动态 client registry 按 workspace 持久化，client secret 仅保存 digest；未显式配置 signing key 时也生成并持久化用户私有 per-workspace key；原 access token 重启后回归通过。 |
| 丢响应后的副作用去重 | 已修复（exec） | `exec_command.operation_id` 同 ID + 同参数复用原任务，同 ID + 不同参数返回 `OPERATION_CONFLICT`；不使用 JSON-RPC id 作为幂等键。 |
| 任务发现/只读 polling | 已修复 | 新增 `get_command` 与 `list_commands`；可按 `command_id`/`operation_id` 找回，且不会推进 stdout/stderr 全局 cursor。 |
| 结果保留窗口 | 已增强 | completed command TTL 从 300 秒提升至 1800 秒，仍受 retained count 与总内存预算约束。 |
| 模型文本结果 | 已增强 | 为 command recovery、code intel、browser、Chrome extension、macOS app 等关键工具补专用 renderer，降低只得到 `completed` 的信息损失。 |
| Browser 跨调用定位 | 已增强 | browser tab payload 增加稳定 CDP `tab_id`；后续 tab 操作可使用 `tab_id`，同时保留 `tab_index` 兼容。 |
| HTTP 资源保护 | 已增强 | 请求体读取增加 15 秒绝对 deadline；HTTP server 增加 32 个并发请求上限，超载返回 503 + `Retry-After`。 |
| 固定工具目录 | 已更新 | catalog 从 49 增至 51：新增 `get_command`、`list_commands`；runtime contract、tools docs、SPEC 与 schema drift 已同步。 |

修复后使用 `uv run python -W default -m unittest discover -s tests/compliance -p 'test_*.py'` 运行完整 compliance 套件：**241 项通过，4 项按平台条件跳过，无功能性失败，且无 ResourceWarning**。`tests.compliance.test_schema_drift` 单独运行 **8/8 通过**。此前测试启动阶段的 urllib socket 资源警告也已通过显式连接关闭与启动 readiness probe 清理。

**验证范围与证据等级**

- 在 macOS / Python 3.13.14 下，运行 220 项现有测试，最终全部通过。178 项覆盖 MCP contract、dual-era、runtime helpers、code intel；另 42 项覆盖 security、tool golden、E2E、runtime semantics、schema drift。这不是全量 CI 或 Windows/Linux 验证。
- 后一组首次运行有 6 项失败，原因是子进程 PATH 中没有 `python`。将仓库 `.venv/bin` 加入 PATH 后，同一组 42 项全部通过。不能把最初的环境失败算作产品缺陷。
- 另运行 8 个定向探针：工具文本结果、索引扫描上限、缓冲区滚动后的轮询、UTF-8 分页、重复执行、OAuth 重建配置、后台子进程、浏览器超时。
- 命令写入、模拟重试、子进程验证均在临时工作区进行；浏览器测试使用独立临时 profile 的 headless Chrome，只打开 `about:blank`，完成后清理测试进程。
- 未在真实 ChatGPT 网页会话中走完插件导入、OAuth、公网隧道和长任务全链路。因此，不能从本轮测试推断特定账户/连接的故障率，也不假设 ChatGPT 有某个固定调用超时值。

**0.3.3 已确认的实现问题（以下为历史复现证据，0.3.4 已按上表修复）**

| 优先级 | 问题 | 复现与影响 | 建议 |
| --- | --- | --- | --- |
| P1 | `browser_evaluate.timeout_ms` 没有约束 JavaScript 执行 | 独立 Chrome 中 `1+1` 正常返回 2；随后传 `timeout_ms=1000`、`new Promise(() => {})`，5,008 ms 后仍未返回，必须外部终止调用进程。HTTP 请求线程可以一直占用；stdio 会阻塞后续请求。 | 采用操作总 deadline，加可终止的执行单元；超时后释放连接与 worker。避免仅让外层等待超时，却把底层工作继续留在后台。 |
| P1 | shell 先退出时，后台子进程逃过 watchdog 与关闭清理 | 临时目录中执行 `sleep 8 & echo $! > child.pid`，设 150 ms timeout；父 shell 已退出 0，超过 deadline 后子进程仍存在；`Runtime.close()` 后也仍存在，`timed_out=false`。 | 生命周期覆盖进程组/子进程树与管道，不以 shell 的 returncode 单独代表任务结束；对允许长期后台运行的进程设计显式、可回收的服务任务契约。 |
| P2 | 大输出滚动后，新输出判断错误，轮询等待到上限 | 用 64 字节缓冲区缩小复现边界：累计输出 100 字节并消费后再写 `NEW`；已有新输出的 250 ms poll 实际等待约 273 ms 才返回。正常配置下问题出现在滚动窗口截断后。 | 比较 `stdout_total_bytes > stdout_cursor` / stderr 对应累计值；TTY 初始输出判断也检查相同问题。推荐用 Condition/Event 唤醒，替代 20 ms 定时轮询。 |
| P2 | `read_output` 在 UTF-8 字符内部切页，造成不可逆乱码 | 输出 `中文🙂`，按 4 字节分页，得到 `中�`、`���`、`��`，拼接为 `中������`。默认 4096 字节边界也可能触发。 | 保持字节 offset 契约，但以完整字符边界返回文本并返回真实 next offset；明确过小 limit、用户指定非边界 offset、缓冲区淘汰边界的处理。 |
| P2 | 代码索引达到 `max_files` 后仍返回 `truncated=false` | 两个 Python 文件，目标符号位于未扫描文件；`code_definition(max_files=1)` 返回 `definitions=[]`、`scanned_files=1`、`truncated=false`。模型可能把“没扫描到”当作“不存在”。同类逻辑存在于 symbols/references。 | 返回 `scan_complete`、`truncated_by=max_files`、扫描/跳过计数与继续策略；遍历顺序确定化。较大文件的完整读取还应设输入字节预算。 |

对应代码位置：

- 浏览器 deadline：[browser.py:215](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:215)，连接 timeout 仅用于 `connect_over_cdp`，`page.evaluate` 没有操作取消机制。
- 后台进程：[processes.py:411](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/processes.py:411)，以及 [server.py:1476](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/server.py:1476) 的 close；watchdog 等待父进程成功后直接结束，close 只处理 active commands 并在父进程仍运行时发送终止信号。
- 轮询：[server.py:3285](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/server.py:3285)，TTY 同类判断在2749 行；累计游标定义见 [processes.py:233](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/processes.py:233)。
- UTF-8：[server.py:3218](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/server.py:3218) 至 chunk 解码处，分页先切原始 bytes，再每页独立 `errors='replace'` 解码。
- 索引：[code_intel.py:119](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/code_intel.py:119)、[code_intel.py:293](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/code_intel.py:293)。

**0.3.3 对网页长会话影响较大的设计缺口（0.3.4 已优先补齐其中的认证、幂等与任务找回）**

1. **P1：OAuth 注册不持久，重启后已有连接凭证失效。** 保持相同 signing key、密码、server URL，只重建 `OAuthConfig`，原 token 的验证从 true 变 false，原因是新的 registry 不含原 client_id。桌面端虽然保存并传回 signing key，也不能解决 registry 丢失。项目已在 limitations 中承认此限制，但在 ChatGPT 插件场景它值得提前修复：OpenAI 文档明确说明 DCR 每个连接注册一次，之后复用 client，要求连接使用期间 client 和 secret 保持有效。见 [oauth.py:143](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/oauth.py:143)、[oauth.py:217](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/oauth.py:217) 与 [OpenAI Authentication](https://developers.openai.com/plugins/build/auth)。

   建议使用权限受控的持久化客户端注册表，签名密钥继续由系统密钥链/秘密存储管理；支持 refresh token 轮换与撤销。当前仅支持 authorization_code，access token 默认 24 小时，到期需要重新授权。refresh 属于改善连续性的功能，不应伪装为现有实现。针对桌面重启、升级、隧道重建分别做恢复测试。

2. **P1：执行副作用后丢响应，没有幂等重试与任务发现机制。** 将同一 JSON-RPC 请求发送两遍，临时文件 append 结果为 `xx`，返回两个不同 command_id。JSON-RPC id 本身不是幂等键，所以这不是协议违规；它证明当前没有执行去重。若第一次响应在隧道中丢失，模型连 command_id 都拿不到，49 个工具中也没有命令列表/恢复入口。当前过期提示还会建议重新执行原工作，可能重复具有副作用的操作。见 [server.py:2619](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/server.py:2619)、[server.py:2994](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/server.py:2994)、[server.py:224](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/server.py:224)。

   建议为有副作用工具增加可选 `operation_id`，以 workspace + 已认证调用身份 + operation_id 为作用域，绑定参数摘要；同键同参数返回已有执行，同键不同参数报冲突。增加任务状态/发现接口与有限期结果账本，先记录接受状态再执行。重启后不能继续的任务应明确为 interrupted/unknown，不承诺任意进程可原地恢复。不要按全局 JSON-RPC id 缓存，不要自动重跑所有超时命令。

3. **P2：结果保留不适合较长的人机往返，输出游标又是全局共享。** 已结束命令保留 300 秒、最多 32 个、受 16 MiB 总预算影响；模型等待用户确认、阅读长文或上下文整理后，日志可能已经不可找回。`write_stdin` 消费命令全局 cursor，响应丢失/另一客户端轮询后，新一次 poll 看不到已消费输出。`read_output` 可以在仍保留且已知 ref 的情况下补读，但不能解决初始 ID 丢失。上述时限与单工作区共享信任域都是已有设计边界，不是“无会话 HTTP”本身有问题。

   建议提供配置化保留期、磁盘滚动 spool、明确的过期时间、以显式 offset 读取的只读状态工具；内存只保留小预览。需要多用户服务时，再加入 authenticated principal 的任务归属和配额，不能把客户端自报 `clientInfo` 当身份。

**模型使用质量与接入体验（原始审查项；已实现项以上方 0.3.4 状态为准）**

- **P2：49 个工具中有 31 个没有专用 `content` renderer。** 实测 `code_symbols` 的结构化结果含 `alpha`、路径和行号，但文本只有 `code_symbols: completed.`。browser、app、extension 系列也走相同 fallback（截图工具仍有 image block）。代码位置：[tool_results.py:33](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/tool_results.py:33)、[tool_results.py:398](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/tool_results.py:398)。应补充简短、包含关键值与下一步所需 ID 的文本表示，并测试 text-only / structured 两条消费路径。**不能据此认定 ChatGPT 必然看不到数据**：[OpenAI 文档](https://developers.openai.com/plugins/build/mcp-server)说明模型可使用 structuredContent 和 content。需要验证实际插件/桥接层把哪些字段送给模型。另应在 read_output 文本中保留日志缺口/淘汰信息，避免只显示剩余片段。
- **P2：浏览器定位不具备跨调用稳定性。** CDP 工具按每次连接枚举出的 `tab_index` 定位；用户或其他调用关闭前面的标签页后，同一个 index 可以指向不同页面。默认 visible tab 也不能唯一代表多窗口的前台页。建议返回稳定 target/tab ID，动作可附 expected URL 或页面版本，失效时要求重新定位。`locator(...).first` 的歧义也应通过明确匹配计数反馈。见 [browser.py:95](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:95)。
- **P2：HTTP 资源保护缺少统一边界。** handler 直接 `rfile.read(length)`，未设置 socket 读取 deadline；ThreadingHTTPServer 没有活动请求上限。exec 的 16 命令配额不约束浏览器调用、OAuth 请求或尚未读完 body 的连接。此项为静态审查风险，未做资源耗尽压力测试。建议请求读取/执行/响应分别限时、并发 semaphore、队列和 429/503 + Retry-After；不必为了支持 JSON-RPC batch 而改变现有协议行为。
- **P2：桌面“运行中”不等于 MCP 可用。** RuntimeManager.status 主要检查进程存活，cloudflared 存活与已取得 URL；没有在该状态路径执行 authenticated MCP 探测。可能出现 UI 绿色但 OAuth 已失效、隧道不通、工具线程卡住。建议区分进程、local MCP、public MCP、认证四种状态；只对明确可自动恢复的故障做有退避的重启。见 [runtime.rs:135](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/src-tauri/src/runtime.rs:135)。
- **将只读 polling 与写 stdin 分开。** 当前 write_stdin 同时负责读取进度与写入输入，整体不能诚实声明 readOnly。新增只读 get_command/poll_command 能减少纯读步骤的授权摩擦，仍保留现有 API 兼容。不要依赖 `--dangerously-fake-readonly-annotations` 来实现稳定调用；官方要求工具注解准确，客户端确认行为也不由服务器权限模式单独决定。
- **加强工具参数和恢复提示。** 统一“状态、是否可重试、原因、下一动作、作用域、过期时间”。`check_exec_environment` 应进一步报告实际可用的 python/python3、node、git、rg、浏览器与平台能力，帮助网页模型选择真实可运行的命令。本轮 PATH 问题就是验证场景。`request_permissions` 在 safe/trusted 下不具备真正的授权通道，应明确说明用户需要在哪里修改配置，避免模型在无法授权的工具间循环。
- **收敛 catalog 的认知负担。** README 和 competitive-analysis 仍称 18 个工具，运行时已经是 49 个。保持公共工具名兼容，先自动生成工具目录并返回平台/后端可用性，再考虑部署时固定的可选组件或插件工作流说明。避免每次调用随机改变工具集合。浏览器/桌面扩展还需要明确的 host capability 授权，现有 exec permission mode 并不自动限制这些独立 handler。
- **限制输入处理与输出数据成本。** 当前 read_file 即使只请求前几行，也继续扫描文件以计算 total_lines；code intel 使用 read_text 完整读文件；browser_evaluate 的 structured result 没有独立字节上限。建议各工具同时设置输入扫描预算、结果字节预算、扫描完整性标记与可靠续读路径，再用真实 Chat 任务测每完成任务的 token/请求数。

**建议实施顺序与验收**

| 批次 | 范围 | 验收方式 |
| --- | --- | --- |
| 第一批：实现缺陷 | 浏览器 deadline、后台进程清理、输出游标、UTF-8 分页、索引扫描完整性、结果 renderer | 上述复现转为回归测试；挂起调用在 deadline 后有界释放；子进程与 reader thread 被回收；有效 UTF-8 页拼接准确；未扫描完不能报告 complete。 |
| 第二批：网页连续性 | OAuth 持久化与续期、operation_id、任务发现、只读 polling、结果保留 | 模拟执行后丢响应再重试，副作用只发生一次；服务升级/重启后原 DCR client 仍可认证；结果在声明保留期内可重读，过期返回明确状态；不同调用方不会因 JSON-RPC id 相同误命中缓存。 |
| 第三批：真实 Chat 质量 | authenticated 健康探测、平台能力、工具说明、端到端评测与遥测 | 固定模型/仓库/权限/任务，在真实 ChatGPT 插件链路完成查代码→改代码→运行测试→读错误→再修复；加入网络断开、授权到期、长输出、标签页变化、用户确认等待。 |

质量指标建议区分：transport success、tool semantic success、command exit success、最终任务成功。已有 exec 返回 `ok=true` 代表工具成功执行，不代表命令 exit_code=0，不能直接把它的成功计数当作代码任务成功率。跟踪 p50/p95、空 poll 比例、命令超时/失联比例、OAuth 重连次数、丢响应后恢复率、输出缺口比例、首补丁成功率，以及每个成功任务的 token/调用数。

保留已有无会话 HTTP 和补丁提交架构；协议/命令基本测试已覆盖不少基础行为。下一轮优先把上面的边界故障纳入测试，再评估是否有必要拆分约 6,449 行的 server.py。模块拆分应服务于独立测试与统一 deadline，不宜先做大重构再寻找稳定性收益。

**原始审查运行命令（0.3.3）**

```bash
CODING_TOOLS_MCP_TELEMETRY=0 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest \
  tests.compliance.test_mcp_contract tests.compliance.test_dual_era \
  tests.compliance.test_runtime_helpers tests.compliance.test_code_intel

PATH="$PWD/.venv/bin:$PATH" CODING_TOOLS_MCP_TELEMETRY=0 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest \
  tests.compliance.test_security tests.compliance.test_tool_golden \
  tests.compliance.test_e2e tests.compliance.test_runtime_semantics \
  tests.compliance.test_schema_drift
```

本报告中的建议优先级依据网页 agent 工作流的影响范围，不代表线上已经发生对应故障。实现问题、已知设计限制与尚待公网验证的风险已分别标注。

**0.3.4 修复后验收命令**

```bash
uv run python -m unittest tests.compliance.test_schema_drift -v
uv run python -W default -m unittest discover -s tests/compliance -p 'test_*.py'
```

修复后仍未声称完成的事项：真实 ChatGPT 插件导入/OAuth/公网隧道长会话 E2E，OAuth refresh token 与撤销，completed output 的磁盘 spool，authenticated principal 级任务隔离/配额，desktop local/public/auth 四层健康探测，以及 Windows/Linux 的完整 CI 复核。
