# Coding Tools MCP — Project-Bound Persistent Gateway 规格

日期：2026-09-22  
状态：Phase 1–4 已落地，进入完整回归与真实连接验收

## 1. 目标

Coding Tools MCP 从“一项目一 MCP Runtime / 一 URL”升级为：

```text
ChatGPT
   │
   │ 一个稳定 MCP URL
   ▼
Persistent MCP Gateway
   │
   ├── MCP Session A ──> Project A
   ├── MCP Session B ──> Project B
   └── MCP Session C ──> Project Worktree
```

Gateway 常驻。新增、删除、切换 Project 不要求重新配置 URL、重启 Tunnel 或重新连接 ChatGPT。

## 2. 不变量

1. Project Root 是默认上下文边界。
2. Permission Scope 是最大访问权限边界。
3. Full Access 不等于默认搜索整个 Mac。
4. 不存在 Desktop 全局可变 `current_project`。
5. 前台窗口不得偷偷改变远程 MCP Project。
6. Project Runtime 的 workspace 生命周期内不可变。
7. Project secret / token / environment value 不进入 project registry 明文文件。

## 3. Session 绑定

HTTP Gateway 正式支持 `Mcp-Session-Id`。

Session 只保存：

- session id；
- selected project id；
- created/last-seen timestamp。

不保存 prompt、conversation、文件正文、diff、token 或 credential。

不同 Session 的 Project selection 必须隔离。

## 4. Project Registry

Registry 是可热更新的非敏感路由表：

```json
{
  "generation": 3,
  "default_project_id": "wosaide",
  "projects": [
    {
      "id": "wosaide",
      "name": "wosaide",
      "path": "/Users/.../wosaide"
    }
  ]
}
```

Desktop 写 registry 时必须 atomic replace。Gateway 使用 generation / mtime 发现变化。

Registry 删除 Project 时：

1. 停止接收该 Project 的新路由；
2. 清理关联 Session selection；
3. 对已启动的受管 Project Runtime 执行关闭；
4. Gateway 本身保持在线。

## 5. Project control

Gateway 仅新增一个直接工具：

```text
project_context
  action = list | current | select
```

远程模型默认不拥有 add/remove/rename/permission mutation。

Project 管理由 Desktop App 负责。

## 6. 路径语义

Project 选定后：

```text
无 path       -> Project Root；list_files / search_text 可配置默认检索目录
相对 path     -> Project Root relative
绝对 path     -> Project Runtime Permission Scope 校验
```

因此 Full Access Project：

```text
search_text(query="annotation")
```

默认仍从 Project Root 搜索；桌面端可为这两个检索工具设置另一个默认目录。
这只改变未传 `path` 时的检索起点，不扩大访问权限。显式传入相对路径仍以 Project Root 为准。

## 7. Gateway / Project Runtime 分层

长期目标：

```text
Public Tunnel
     │
     ▼
Gateway Runtime
     │
     ├── Project Runtime A
     ├── Project Runtime B
     └── Project Runtime C
```

Gateway 负责：

- Auth / OAuth；
- Public Tunnel；
- MCP Session；
- Project Registry；
- Project routing；
- Project lifecycle coordination。

Project Runtime 继续负责：

- Files / Search / Patch；
- Shell / Processes；
- Git / Worktrees；
- LSP；
- Checks / Review；
- Workflow / Checkpoints；
- Project Instructions；
- permission enforcement。

不得在 Gateway 重写上述工程能力。

## 8. Desktop 数据模型

目标模型：

```text
GatewayConfig
  server_name_prefix
  local_port
  auth
  tunnel

ProjectProfile
  id
  name
  path
  permission_mode
  allowed_paths
  environment_variables (secret values remain Keychain)
```

旧 `WorkspaceProfile` 中的 Gateway 字段需要兼容迁移，不能静默丢弃配置。

## 9. Desktop 多窗口

```text
window_id -> project_id
```

窗口只是 UI 绑定，不是远程会话安全边界。

同一个 Project 多窗口默认复用同一个 Project Runtime。

Git worktree 可直接注册为独立 Project。

## 10. Tunnel / Auth

从：

```text
1 Project = 1 Tunnel + 1 OAuth
```

改成：

```text
1 Desktop Gateway = 1 Tunnel + 1 OAuth
```

Project 切换不得要求重新 OAuth。

## 11. 热更新

以下操作不重启 Gateway：

- add/remove Project；
- rename Project；
- worktree add/remove；
- Session project switch；
- Project Registry 更新。

项目权限、环境变量等若需要重启，只允许重启对应 Project Runtime。

## 12. 单 Project Runtime

未启用 Project Gateway 时，Runtime 保持单 Project：

- tool surface 不增加 `project_context`；
- 不维护 Gateway session routing；
- 所有启用的 coding primitives 直接出现在 `tools/list`。

## 13. Tool surface

Gateway 不增加第二层工具发现或 workflow。除 `project_context` 外，Project Runtime
继续直接暴露同一组 coding primitives，模型不需要先目录检索再二次调用。

## 14. 验收

必须覆盖：

1. 同一 Gateway URL 同时服务至少两个 Project。
2. 两个 MCP Session 可选择不同 Project，互不影响。
3. 同名文本的交叉搜索不会串 Project。
4. Project 删除后绑定 Session 变为未选择状态，而不是自动切到兄弟 Project。
5. Full Access Project 默认搜索仍限制在 Project Root。
6. 普通单 workspace Runtime 旧契约不退化。
7. Desktop 的 Tunnel/Auth 逐步提升为 Gateway 级配置。
8. Project secrets 不进入 registry 明文。
9. Python、Desktop TypeScript、Desktop Rust、compliance 全部通过。

## 15. 当前实施顺序

### Phase 1

- Python ProjectRegistry；
- SessionRegistry；
- GatewayRuntime；
- `Mcp-Session-Id`；
- `project_context`；
- HTTP 双 Session / 双 Project 验证。

### Phase 2

- Desktop GatewayConfig / ProjectProfile；
- 旧配置迁移；
- registry atomic writer。

### Phase 3

- Desktop Gateway Supervisor；
- 单 Tunnel / OAuth；
- Project Runtime 生命周期。

### Phase 4

- 多窗口；
- Open Project / Open Recent；
- worktree Project UX。

### Phase 5

- 全量回归与真实 ChatGPT 远程连接验收。

## 16. 2026-09-22 实施记录

当前源码已经完成：

- Python `ProjectRegistry` / `SessionRegistry` / `GatewayRuntime`；
- HTTP `Mcp-Session-Id` 创建、校验、DELETE 结束会话和 CORS 暴露；
- Gateway-only `project_context(list|current|select)`；
- 同一 Gateway URL 下双 Session / 双 Project 隔离验证；
- registry-only Gateway 到 loopback Project Runtime 的代理验证；
- Desktop `GatewayConfig` 与 Project 配置迁移；
- Project registry 原子写入和热加载；
- 一个 Gateway process / 一个 Tunnel / 多个 loopback Project Runtime；
- Project 增删和权限/环境变化仅 reconcile 对应 Project Runtime；
- Project Window：窗口 URL 固定携带 `project=<profile_id>`，同一 Project 重复打开复用已有窗口，Project 窗口不会跟随主窗口选择变化；
- Gateway/Project UI 文案、类型、日志和运行状态同步更新；
- `project_context` 加入 runtime contract、tools inventory 和 discovery 分类。

验证记录：

- Gateway + runtime helper 定向测试：102 项通过；
- Desktop TypeScript/Vitest：4 项通过，production build 通过；
- Desktop Rust：`cargo fmt --check`、Clippy `-D warnings`、48 项测试通过（3 项下载型测试忽略）；
- Ruff、mypy、tool-surface budget、`git diff --check` 通过；
- 工具注册量 72，schema 总量 26183 bytes，仍低于 75 tools / 30000 bytes 门限；
- Python 全量最终复验：365 项通过、11 项跳过；此前既有后台子进程回收时序测试曾出现一次瞬时失败，该单项独立复跑通过，随后完整复跑亦全绿。
- Desktop 最终复验：TypeScript/Vitest 4 项通过、production build 通过；Rust `cargo fmt --check`、Clippy `-D warnings`、48 项测试通过（3 项下载型测试忽略）。
- 自动化源码回归已完成；Phase 5 仅剩重新构建/启动新版 Desktop Gateway 后的真实远程 ChatGPT 连接验收。

当前正在连接本对话的 MCP 仍是旧的已启动实例；源码修改不会热替换该进程。真实 ChatGPT 验收必须使用重新构建/启动后的 Gateway，不能把当前旧连接的行为当成新版结果。
