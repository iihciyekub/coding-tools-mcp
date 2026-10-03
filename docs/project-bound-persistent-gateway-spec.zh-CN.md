# Coding Tools MCP — Project-Bound Persistent Gateway 规格

日期：2026-10-03
状态：Workspace 授权与 Project 索引分离；真实 Desktop 连接验收仍需单独进行

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
8. Workspace Root 是持久授权单位，Project 是其下的工作目录索引，继承 Workspace 权限。
9. 索引扫描不构成目录白名单；新建普通目录无需 Git、重新注册或重启 MCP。

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
  "default_project_id": "research",
  "workspaces": [
    {
      "id": "research",
      "name": "Research",
      "path": "/Users/.../ii-research",
      "endpoint": "http://127.0.0.1:54321/mcp",
      "runtime_build_id": "0.4.5-0123456789abcdefabcd"
    }
  ],
  "projects": []
}
```

新 registry 的 `default_project_id` 应指向 Workspace Root 的 id（上例为 `research`）。
只有一个 Workspace 且未指定默认项时，Gateway 自动以该根目录作为默认工作目标。
`workspaces` 保存授权根目录及其受管执行器 endpoint，权限和环境变量来自持久
Workspace profile，通过执行器启动参数与进程环境传入，不复制到 Project 索引。
Desktop 为每个 Workspace 启动一个受管本地 Gateway，Project Runtime 在该进程内
按需创建，并保持各自的 Git、LSP、命令和工作流状态。外层 Gateway 代理调用时携带
Project 的绝对路径，让本地执行器保留正确的相对路径基准。

启动扫描始终保留 Workspace Root，同时索引第一层普通目录和深层带 Git 或常见
项目清单的目录；扫描每个 Workspace 最多检查 2000 个条目，最深检查 8 层，跳过
依赖、构建目录和符号链接。后续请求至多每 2 秒刷新扫描；任意已有子目录可在调用时
按绝对路径或 Workspace 相对路径立即解析，不受扫描深度、刷新间隔或 Git 标记限制。
重叠 Workspace 由路径最深的根目录负责；同名且不唯一时返回候选项，避免静默串项目。
符号链接按实际路径判断所属 Workspace。Project 规则从 Workspace Root 沿路径继承。

旧的仅含 `projects` 的 registry 继续支持显式项目路由。Desktop 现有 `profiles.json`
中的 Workspace profile 直接作为授权根目录使用，保留 id、路径、访问模式和 Keychain
配置，无需重选目录。生成的 `projects` 根目录条目用于兼容旧 registry 消费者；子项目
索引由 Python Runtime 管理，不作为新的持久授权配置。

Desktop 写 registry 时必须 atomic replace。Gateway 使用 generation / mtime 发现变化。
Registry 是某个 Desktop App instance / runtime build 的运行时状态，必须位于该 bundle
自己的 app-local data 目录，并继续按 `runtime_build_id` 分目录；preview/dev 与 production、
旧版与新版不得共享同一个 registry 文件或 endpoint 集合。

`runtime_build_id` 来自 Desktop bundle 内置 Runtime 的内容哈希环境键。Gateway 第一次
代理某 Project Runtime 前必须通过 `server_info.runtime_build_id` 校验实际执行器；不一致时
返回 `PROJECT_RUNTIME_VERSION_MISMATCH`，不得继续把业务 Tool 调用发送给旧 Runtime。

Desktop 的现有 snapshot reconcile 同时承担轻量自愈：如果某个 Workspace 执行器子进程
已经退出，则用当前 bundle 的同一 `runtime_build_id` 重新启动该执行器，并原子
更新 registry endpoint；不额外创建常驻 health-monitor 服务。

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
  project = <name | directory | root path | id>   # action=select
```

模型不需要先复制内部 `project_id`；优先直接使用人类可读的 Project 名称或目录名。

远程模型默认不拥有 add/remove/rename/permission mutation。

Project 管理由 Desktop App 负责。

若 Desktop 保存的是一个“项目容器目录”而该目录自身不是 Git repository，则 Gateway
启动前应把其直属 Git roots 展开为独立 Project，并使用父配置 + canonical path 派生稳定
Project ID。用户无需手工把同一父目录下的每个仓库重新添加一次。若父目录本身是 Git
repository，则它继续作为单一 Project。若某个自动发现的子 Project 同时被用户显式保存
为独立 ProjectProfile，则显式配置优先，保留其独立权限和环境设置。

## 6. 路径语义

Project 选定后：

- 所有普通相对路径默认相对于该 Project Root；
- 父容器目录不再继续参与文件、Git、LSP、检查或命令路径解析；
- 若 Gateway 根目录只是多个 Git Project 的容器，新 Session 不会自动绑定父容器；只有一个子 Project 时可自动绑定，多个时需要选择一次。

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

Gateway Runtime 与全部 Project Runtime 必须来自同一个 Desktop bundle runtime build。
Desktop 允许借用系统 Python/uv 来创建私有环境，但不得仅凭相同 semantic version 直接
复用系统已安装的 `coding-tools-mcp` 包；实际执行代码必须来自当前 App bundle 内置 wheel。

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

WorkspaceProfile (持久配置)
  id
  name
  path (Workspace Root)
  permission_mode
  allowed_paths
  environment_variables (secret values remain Keychain)
```

ProjectProfile 是工作索引；旧 Desktop snapshot 中的权限字段继续作为 Workspace
有效权限的投影返回。旧 `WorkspaceProfile` 中的 Gateway 字段需要兼容迁移，不能静默丢弃配置。

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

Desktop App 升级或 bundle runtime build 变化时不属于热更新：旧 Gateway/Project Runtime
必须停止，并使用新的 bundle hash 对应私有环境重新启动后再发布 endpoint。

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
5. 默认搜索遵循 Workspace 的有效搜索目录设置，保留现有 Desktop 默认值；显式相对路径以所选 Project Root 为准。
6. 普通单 workspace Runtime 旧契约不退化。
7. Desktop 的 Tunnel/Auth 逐步提升为 Gateway 级配置。
8. Project secrets 不进入 registry 明文。
9. Python、Desktop TypeScript、Desktop Rust、compliance 全部通过。
10. Gateway / Project Runtime `runtime_build_id` 一致；故意制造版本漂移时调用被阻断。
11. Workspace Root 始终可用；扫描子项目不清除单 Workspace 的默认根目录。
12. 新建普通目录能立即被选择和使用；读取、修改、命令执行继承 Workspace 权限。
13. 子项目继承父级规则；重叠 Workspace、同名目录及符号链接不会串到错误执行器。

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
