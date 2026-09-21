# Local Agent Capability Gateway 规格

日期：2026-09-21。状态：Phase 0 与 Phase 1 内部只读 Provider 已实现，并通过本机真实 Codex Computer Use、CTM approval/session 和 screenshot 验收；尚未注册新的公开 Provider 工具，Native Computer 继续默认。

## 1. 目标

Coding Tools MCP 已经拥有文件、命令、Git、LSP、Workflow、多项目和独立 Computer Use v1。用户电脑上还可能安装 Codex、Claude Code、Gemini CLI、Cursor、OpenCode 等 Agent，它们各自带有本地能力、Skills、浏览器、Computer Use 或运行时。

本规格的目标不是把这些 Agent 作为第二个模型调用，而是把**已经安装的本地执行能力**抽象为模型无关的 Capability，并由当前连接 Coding Tools MCP 的 LLM 负责推理和决策。

```text
Current LLM
    |
    v
Coding Tools MCP
    |
    +-- Core runtime: files / shell / git / lsp / workflow
    +-- CTM native computer
    +-- Capability manager
         |
         +-- Codex adapter
         +-- Agent environment discovery
         +-- Context checkpoint runtime
         +-- future adapters
```

核心原则：**当前 LLM 是唯一 Agent；本机 Capability 只是手、眼睛、浏览器和执行器。**

## 2. 非目标

本设计明确不做：

- 不通过 `codex exec`、Codex prompt 或其它 Agent prompt 执行工作。
- 不把 Codex CLI 当 Sub-Agent。
- 不自动创建 Codex 模型 turn。
- 不调用 Codex 原生 `thread/compact/start` 作为“免费压缩”。
- 不暴露 raw `codex_rpc`、raw `node_repl` 或任意 Codex MCP 调用给外部 MCP Client。
- 不复制、打包或重新分发 Codex / OpenAI 私有组件。
- 不因为 Host / Full Access 而绕过 Computer approval、macOS TCC、SIP 或管理员授权。
- 不把扫描到的所有 Skills 自动注入模型上下文。
- 不把 Browser 和用户 Chrome 登录态混为同一种权限。

## 3. 已验证的 Codex Computer Use 路径

直接启动 `SkyComputerUseClient mcp` 可以完成 MCP handshake 和 `tools/list`，但真实操作会在宿主 bootstrap 阶段失败。直接连接 `computeruse.sock` 也会被本地服务的 sender authentication 拒绝。因此 Coding Tools MCP 不应逆向复制 Sky 私有 socket 协议。

本机已经真实跑通的路径是：

```text
Coding Tools MCP
    |
    v
Codex app-server
    |
    v
ephemeral thread
    |
    v
mcpServer/tool/call
    |
    v
node_repl
    |
    v
@oai/sky
    |
    v
OpenAI-signed local Computer Use service
```

真实验证包括：

- `sky.list_apps()` 返回本机 App。
- `sky.get_app_state({app:"Finder"})` 返回 Accessibility tree 和 screenshot。
- Computer Use 能触发真实的 per-app approval。
- 只调用 `mcpServer/tool/call` 时，ephemeral thread 最终仍为 `turns=[]`。

这证明 Codex app-server 可以被当成**本地 capability broker**，而不是 Agent。

## 4. No-Model-Turn 硬约束

“不调用第二个模型”必须由代码保证，不能只写在 prompt 里。

Codex broker 第一版 RPC allowlist 只有：

```text
initialize
thread/start
thread/read
mcpServer/tool/call
```

以下方法在客户端写入 pipe 之前直接拒绝：

```text
turn/start
turn/steer
thread/compact/start
任何其它未 allowlist 的 app-server RPC
```

错误分别为 `CODEX_MODEL_TURN_FORBIDDEN` 或 `CODEX_RPC_FORBIDDEN`。

兼容性 probe 完成后必须再次 `thread/read` 并断言：

```text
turns == []
```

机器可读状态只能承诺：

```text
model_invocation: forbidden
model_turns_created: 0
billing_guarantee: none
```

不得宣传“绝对零 Codex/Work 额度”，因为 CTM 只能保证自己不发模型 turn，不能定义外部产品未来对本地工具或后台活动的计量方式。

## 5. Broker 隔离模型

### 5.1 不使用用户主 Codex 配置启动 Broker

实测表明，直接从用户 `~/.codex/config.toml` 启动 app-server 会同时加载其它 Plugin/MCP，例如项目自定义 MCP。更重要的是，当前 Codex 的 plugin `enabled=false` CLI override 并不能可靠阻止 thread capability 装载，因此不能用“覆盖用户配置”作为安全边界。

正式方案使用**临时最小 `CODEX_HOME`**：

```text
Temporary CODEX_HOME
  config.toml
    mcp_servers.node_repl only
```

不复制：

- `auth.json`
- Plugins
- Marketplaces
- Skills
- Hooks
- Browser sessions
- 其它 MCP servers
- 用户 Codex session/history

### 5.2 node_repl 配置最小化

从用户 Codex 配置只读取已经安装的本地运行时路径：

- `NODE_REPL_NODE_MODULE_DIRS`
- `NODE_REPL_NODE_PATH`
- `SKY_CUA_SERVICE_PATH`
- node_repl executable

临时配置强制：

```text
CODEX_HOME = temporary home
NODE_REPL_TRUSTED_CODE_PATHS = temporary home + installed node module dir
NODE_REPL_TRUSTED_SERVICES = { sky = @oai/sky/service }
CODEX_CLI_PATH = discovered Desktop Codex executable
```

Browser trusted service 不进入 Computer-only Broker。

### 5.3 Runtime 二次检查

即使使用最小配置，Broker 仍监听：

```text
mcpServer/startupStatus/updated
```

Computer probe 第一版唯一允许启动：

```text
node_repl
```

出现其它 server 立即返回 `CODEX_BRIDGE_ISOLATION_FAILED` 并终止 Broker。

## 6. Codex 安装发现和版本兼容

Desktop capability 依赖 ChatGPT Desktop 自带的宿主运行时。发现顺序必须是：

1. 显式 `CODEX_CLI_PATH`。
2. `/Applications/ChatGPT.app/Contents/Resources/codex`。
3. 用户 `~/Applications/ChatGPT.app/...`。
4. 最后才是 PATH 中普通 `codex` CLI。

本机实测发现 PATH 中存在 `codex-cli 0.154.0`，而 ChatGPT Desktop 内置的是 `0.155.0-alpha.9.2`；优先 PATH 会选错 Provider，因此 Desktop 路径优先是必要的兼容性要求。

版本改变时执行 schema probe：

```text
codex app-server generate-json-schema --experimental
```

对全部 JSON schema 生成稳定 hash，并验证至少包含：

```text
initialize
thread/start
thread/read
mcpServer/tool/call
```

缺失任一项即 `CODEX_SCHEMA_INCOMPATIBLE`，禁止继续猜参数。

## 7. Compatibility Probe 契约

完整只读 probe：

1. 定位 Desktop Codex。
2. 读取版本。
3. 生成并 hash app-server schema。
4. 创建临时最小 `CODEX_HOME`。
5. 启动 app-server。
6. `initialize`。
7. `thread/start(ephemeral=true)`。
8. 等待 `node_repl` ready。
9. 确认没有其它 MCP server 启动。
10. 通过 `mcpServer/tool/call` 执行 `sky.list_apps()`。
11. `thread/read`。
12. 断言 `turns=[]`。
13. 关闭 app-server 并删除临时 home。

输出示例：

```json
{
  "available": true,
  "compatible": true,
  "version": "codex-cli ...",
  "schema_hash": "...",
  "started_servers": ["node_repl"],
  "app_count": 39,
  "model_invocation": "forbidden",
  "model_turns_created": 0,
  "billing_guarantee": "none"
}
```

仓库命令：

```bash
.venv/bin/python scripts/probe_codex_bridge.py
```

不 compatible 时退出码为 1，但 CTM core 不受影响。

## 8. 与现有 CTM Computer Use 的关系

`computer-use-spec.md` 定义的 Native Computer v1 保持主实现和稳定安全契约：

- per-app human approval
- bounded session
- app/process identity binding
- action receipt
- stale snapshot 防护
- app lock
- no generic keyboard/mouse fallback

Codex Computer Use 当前实际提供：

```text
list_apps
get_app_state
click
perform_secondary_action
set_value
select_text
scroll
drag
press_key
type_text
```

这些能力比 CTM v1 更强，不能直接塞入当前 `Backend` 然后把 coordinate click 偷偷解释成 AX press。

因此：

- Native CTM Computer 继续默认。
- Codex Computer 初期作为可选 Preview Provider。
- v1 只映射语义完全等价的能力。
- click/type/scroll/drag/key 等进入单独的 Computer v2 contract 和审批范围。

## 9. Approval Bridge

Codex Computer Use 会发起双向 server request，例如 app approval。App-server client 必须支持服务端向客户端发 request，不能按简单 request/reply helper 实现。

最终接入 public Computer 时，CTM 的 Workflow approval 仍是权威审批源：

```text
LLM request
 -> CTM approval
 -> Desktop user approves exact app/scope/TTL
 -> Codex internal app approval request
 -> CTM checks matching active approval/session
 -> reply accept for this session
```

没有匹配的人类审批时，内部 elicitation 不能自动 accept。

## 10. 环境与秘密隔离

Broker 子进程继承宿主环境前，必须删除 CTM transport secrets，包括：

```text
CODING_TOOLS_MCP_AUTH*
CODING_TOOLS_MCP_OAUTH*
CODING_TOOLS_MCP_SERVER_URL
```

Broker 的 `CODEX_HOME` 强制指向临时隔离目录，不能被父进程环境覆盖。

日志与 telemetry 不得记录：

- typed text
- screenshot bytes
- page body
- cookie
- password / OTP
- OAuth token
- CTM bearer token
- Skill 全文

允许记录版本、schema hash、provider、operation name、duration、result code 和匿名化 session identity。

## 11. Agent Environment Discovery

已实现一个轻量 read-only `agent_environment` discovery 层，用统一格式发现：

- Codex / Claude Code / Gemini CLI / Cursor / OpenCode 等是否安装
- CLI/version
- Skills metadata
- Plugin Skills metadata
- Worktrees
- Rules / AGENTS
- 可用 local capabilities

Discovery 默认只读取 metadata，不展开：

- auth
- token
- cookie
- browser profile
- keychain
- OAuth 文件
- secret session data

Skill 按以下类别标记：

```text
portable_instruction
mapped_capability
host_specific
model_backed
unknown
```

只在当前任务真正匹配时读取单个 Skill，不批量注入上下文。

该工具只在 `permission_mode=host` 暴露。Safe/Trusted 模式不能借 discovery 越过 workspace 扫描用户 Home。当前实现返回敏感资源的 `present` 布尔值，但不读取 `auth.json`、browser sessions、OAuth/token 内容。

## 12. Context Checkpoint Runtime

Codex 原生 compaction 已确认是 model-backed 操作，因此 CTM 不复用它作为本地压缩。

CTM 自己提供跨 Agent checkpoint：

### Deterministic state

- workspace/project/repo identity
- HEAD / git status fingerprint
- changed files
- running commands
- latest checks/tests
- active sessions
- capability versions

### Semantic summary

由当前调用 CTM 的 LLM 生成并写入：

- 当前目标
- 已完成
- 关键决定
- 未解决问题
- 下一步

CTM 不额外调用模型。

Checkpoint 已复用现有 WorkflowStore，实现为单个 `context_checkpoint(action=create|get|list)` 工具。Create 自动记录 Git 状态、running commands 和 capability fingerprint；语义 summary/decisions/unresolved/next_steps 由当前调用 CTM 的 LLM 提交。Get 会重新计算 runtime state，并用 `git_state_changed`、`running_commands_changed`、`capabilities_changed` 标记 stale。带 `task_id` 的 context checkpoint 也会进入 `task_context`，便于跨会话恢复。

## 13. Browser 与 Chrome

Browser 和 Chrome 单独建能力和发布 Gate。

Browser 面向 localhost / isolated in-app browser，可在真实 DOM、截图、点击、输入 smoke 通过后接入。

Chrome 能接触用户已有登录状态、cookies、企业应用和扩展，属于高敏感能力；默认关闭，并要求 origin approval。第一版不桥接密码、OTP、2FA、CAPTCHA、secure auth handoff 或 cookie export。

2026-09-21 的真实隔离 broker 验证进一步确认：Browser service 可以通过 `node_repl` 初始化，并可在不创建模型 turn 的情况下工作；Browser service 要求的 Codex turn metadata 必须通过合法的 `mcpServer/tool/call.params._meta["x-codex-turn-metadata"]` JSON 字符串传入。直接把 `session_id/turn_id` 放在 `_meta` 顶层无效。

但当前 Desktop runtime 的隔离 Broker 中 `iab` backend 不可用；`getForUrl(localhost)` 会选择现有 Chrome extension。为了不把 localhost 验收任务静默落到用户日常 Chrome 登录态，Phase 6 当前保持 fail-closed，不开放 Codex Browser Provider。CTM 现有 disposable Playwright localhost 验收继续作为安全默认方案。

当前状态：

```text
Browser: broker verified / safe IAB unavailable / not exposed
Chrome: candidate / high-sensitivity / disabled
```

## 14. 生命周期和并发

第一版建议：

```text
one CTM runtime -> one lazy Codex app-server broker
one computer session -> one ephemeral thread
```

Broker crash 后所有 in-flight mutating action outcome 都视为 unknown；不得自动 replay click/type/drag。

Provider 或 Codex 版本变化时旧 session 失效，新 session 必须重新 probe。禁止 snapshot 来自版本 A 而 action 使用版本 B。

## 15. 错误模型

统一错误至少包括：

```text
CODEX_NOT_INSTALLED
CODEX_APP_SERVER_UNAVAILABLE
CODEX_APP_SERVER_TIMEOUT
CODEX_APP_SERVER_ERROR
CODEX_SCHEMA_INCOMPATIBLE
CODEX_CAPABILITY_UNAVAILABLE
CODEX_BRIDGE_ISOLATION_FAILED
CODEX_MODEL_TURN_FORBIDDEN
CODEX_MODEL_TURN_GUARD_FAILED
CODEX_RPC_FORBIDDEN
CODEX_BROKER_CRASHED
```

Provider 错误只让对应 capability unavailable，不得导致 Core runtime 启动失败。

## 16. Rollout

### Phase 0 — Broker foundation（已实现）

- Desktop Codex discovery。
- app-server bidirectional JSON-RPC client。
- model-turn RPC hard block。
- schema fingerprint / compatibility check。
- 临时最小 `CODEX_HOME`。
- node_repl-only isolation。
- read-only `list_apps` real probe。
- `turns=[]` verification。
- CTM transport secret filtering。
- unit tests 和 opt-in real smoke。

### Phase 1 — Read-only Codex Computer（Preview public path 已实现）

- 建立 Provider interface。
- app list / app state / screenshot。
- 接 CTM human approval 和 session lifecycle。
- 不开放 click/type/drag。
- 每次 Provider 调用后再次 `thread/read` 验证 `turns=[]`。
- nested `mcpServer/elicitation/request` 只在 thread、app、tool 均匹配当前 CTM observe lease 时接受。
- screenshot 只接受本地 `file://` 或受限 image data URL，并限制为 5 MiB。
- Provider session 和 CTM runtime/broker 同寿命；重启后旧 session 不可继续使用。
- 公共工具复用现有 `app_list / computer_request_access / computer_session_start / computer_session_get / computer_session_stop`，通过显式 `provider=codex` 选择 Preview Provider。
- 只新增一个 `app_observe` 用于 whole-app accessibility text + optional screenshot；没有增加 `codex_*` 工具族。
- `provider` 默认仍为 `native`，不会因为安装了 Codex 自动切换 Provider。
- Codex public Preview 只在 host mode + Computer tools 开启时可用；Safe/Trusted 下 `provider=codex` fail closed。

### Phase 2 — Computer v1 compatible mapping

- 只映射与现有 AX contract 完全等价的操作。
- Native provider 继续默认。
- 当前暂不映射。受支持的 `@oai/sky` 高层 `get_app_state()` 稳定返回只有 `app + screenshot + accessibility text`，没有 CTM Computer v1 的 `window_id / snapshot_id / structured elements[] / action list`。不得通过脆弱文本解析伪造 v1 element contract。

### Phase 3 — Computer v2（实现完成，真实写操作 smoke 待人工审批验收）

- click / coordinate click / type_text / press_key / scroll / drag / select_text / secondary action。
- 独立 schema、风险等级和 approval scope。
- `provider=codex` 的 `control` 必须单独桌面人工审批，不能由 host mode 自动获得。
- 写操作统一走 `app_interact`，必须携带最近一次 `app_observe` 返回的短期 `snapshot_id` 与唯一 `operation_id`。
- 动作执行前重新 observe；状态漂移返回 `COMPUTER_SNAPSHOT_STALE`，不会继续写。
- 相同 `operation_id` + 相同参数只返回 receipt；不同参数返回 `OPERATION_CONFLICT`；不确定结果返回 `COMPUTER_ACTION_UNKNOWN`，禁止自动重试。
- 当前只完成单元/契约验证；没有通过测试代码伪造人工审批去对用户真实 App 执行写操作。

### Phase 4 — Agent Environment Discovery（已实现）

- Skills / Plugins / Worktrees / Rules metadata。
- host-only tool surface；Safe/Trusted 不暴露。
- sensitive resources 只报告存在，不读取内容。
- Codex Plugin Skills 与普通 Skills 分开标记来源。

### Phase 5 — Context Checkpoint Runtime（已实现）

- deterministic checkpoint。
- caller-supplied semantic summary。
- stale detection。
- 单个 `context_checkpoint` 工具，不增加 create/get/list 三个工具。
- 绑定 task 时自动进入 `task_context`。

### Phase 6 — Browser（Broker 已验证，发布 Gate 未通过）

- `setupBrowserRuntime()`：通过，`turns=[]`。
- `browsers.list()`：通过，使用正规 `x-codex-turn-metadata`，`turns=[]`。
- isolated `iab`：当前不可用。
- localhost 自动选择现有 Chrome extension，因此停止后续导航测试并保持 Provider 不开放。

### Phase 7 — Chrome

- 单独隐私、安全和 origin approval 评审后再开放。

## 17. Phase 0 本机验收证据

2026-09-21 在当前 macOS 环境执行真实 probe：

```text
executable: /Applications/ChatGPT.app/Contents/Resources/codex
version: codex-cli 0.155.0-alpha.9.2
schema: compatible
started_servers: [node_repl]
app_count: 39
model_turns_created: 0
compatible: true
```

同一轮测试还验证了两个应长期保留的负面案例：

1. PATH 中另一个 `codex-cli 0.154.0` 不能替代 Desktop Codex capability runtime，因此 Desktop 优先发现是必要行为。
2. 试图通过 CLI `-c` 覆盖用户 Plugin/MCP 配置并不能提供可靠隔离；最小临时 `CODEX_HOME` 才是当前可验证的安全实现。

## 18. Phase 1 本机验收证据

2026-09-21 使用临时 Preview fixture 完成两层真实 smoke。

Provider 层：

```text
app_id: com.apple.Preview
session mode: observe only
accessibility text: 2024 chars
screenshot: image/jpeg, 20439 bytes
nested app approval: 1, risk=low
mutating provider surface: none
```

CTM approval/session 层：

```text
Workflow approval: approved -> consumed
provider: codex
access: observe
state read: success
MCP image payload prepared: yes
session stop: stopped
```

过程中没有注册 click/type/drag/key 等写操作。真实 smoke 还暴露并修复了 persistent `node_repl` 在重复顶层 `const` 声明时会把 warning 前缀和 JSON 写入同一 text content 的兼容问题；Provider 现在使用独立 async scope，并对 non-fatal warning 前缀做有界 JSON 解码回退。

## 19. Phase 4 / Phase 5 本机验收证据

Agent Environment 真机验证：

```text
provider: codex
installed: true
enabled plugins: 13
plugin skills discovered: 35
worktrees: worktrees/annotation-engine-v3
rules: rules/default.rules
computer_use_installed: true
browser_plugin_cached: true
chrome_plugin_cached: true
sensitive resources present: auth/browser sessions/mcp oauth
sensitive contents read: false
```

当前 `~/.codex/skills` 中还存在两个 symlink 目录，但它们目标根目录当前都没有 `SKILL.md`，因此不会被错误计为一个可直接读取的 Skill；symlink Skill fixture 已有单测覆盖。

Context Checkpoint 验证：

```text
create -> immediate get: fresh
runtime capability fingerprint changed -> get: stale, capabilities_changed
list: checkpoint discoverable
task-bound checkpoint -> task_context: discoverable
external model invocation: none
Codex compact invocation: none
```

## 20. Phase 6 Browser 验收证据

```text
isolated broker MCP servers: node_repl only
setupBrowserRuntime(): success
browsers.list(): success
model turns after calls: 0
x-codex-turn-metadata forwarding: verified
isolated iab backend: unavailable
getForUrl(localhost): selected existing Chrome extension
navigation against daily Chrome: intentionally not performed
```

因此当前版本不把 Codex Browser 宣称为可用安全 Provider；这不是“接口没跑通”，而是 safe-backend release gate 没通过。

## 21. Definition of Done

Phase 0 完成条件：

- 单测验证 model RPC 在 spawn/write 之前被拒绝。
- 未 allowlist RPC fail closed。
- 双向 app-server server request 可以安全处理。
- 隔离 home 不包含用户 Plugins、Skills、auth 或其它 MCP。
- Browser service 不进入 Computer-only trusted services。
- CTM transport secrets 不进入 Broker。
- schema hash 可生成。
- 真机只启动 `node_repl`。
- 真机 `list_apps` 成功。
- 真机最后 `turns=[]`。
- Codex Bridge 失败不影响现有 CTM Computer/Core runtime。

Phase 1 完成条件：

- Provider 公开面只有 list/read/screenshot，没有 mutating 方法。
- app approval 复用现有 `WorkflowStore`，没有第二套授权数据库。
- approval 为一次性、精确 app scope，未批准时不可启动 session。
- Broker/Provider 创建失败时不得提前 consume approval。
- nested Codex elicitation 不能扩大 CTM 已批准 app scope。
- session TTL、stop 和 provider shutdown 都会撤销后续内部批准。
- 每次 read-only tool call 后仍验证 `turns=[]`。
- screenshot 有类型、大小和本地 URL 限制，不接受远程 URL。
- 真机 Preview observe + screenshot smoke 通过。
- 现有 Computer v1 默认 Provider 和 Native `app_snapshot/app_action/app_wait` 语义保持不变。
- Codex Preview 通过向现有 app/session 工具增加可选 `provider` 参数和新增只读 `app_observe` 暴露，不伪造 `window_id/snapshot_id/elements[]`。
- `access=control` 对 Codex Preview 在创建审批前即拒绝。

公开 MCP 路径真机 smoke：

```text
app_list(provider=codex, query=Finder): success
computer_request_access(provider=codex, access=observe): human approval created
approval: approved -> consumed
computer_session_start: codex_computer_* active
app_observe: 605 chars AX text + image/jpeg MCP image block
computer_status.providers.codex: available, preview_v2
computer_session_stop: stopped
Native helper during smoke: unavailable (Codex Preview remained usable independently)
```

后续 Phase 只有满足自己的真实 smoke、安全检查和回归测试后才能从 `experimental` 提升为 `preview`。

