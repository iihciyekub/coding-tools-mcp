# 本机 Agent Skill 能力桥接设计

状态：目录桥接第一阶段已实现（2026-09-23）；原生 ChatGPT Skill 打包、专有插件动作适配和网页端验收仍待按具体 Skill／插件完成。

## 要解决的问题

用户已在运行网关的主机上为 Codex、Claude Code、Cursor、Gemini CLI 等 Agent 安装 Skill，并通过 Coding Tools MCP 的远程地址使用 ChatGPT 网页版时，希望 ChatGPT 能知道有哪些能力、按需读取相关 Skill，并准确判断插件的实际功能是否可用。远程 ChatGPT 只能访问网关主机上获授权的文件；它不能因此看到浏览器所在设备上的其他本地文件。

当前 MCP 的直接工具集见 [工具与 Schema](tools-and-schemas.md)。它能读写授权范围内的文件、运行命令并读取项目规则；在持久 Gateway 显式配置本机能力目录后，另提供只读 Skill／插件目录工具。`AGENTS.md`／`CLAUDE.md` 项目规则会随 MCP 初始化提供；这些规则和各 Agent 安装的 Skill／插件是不同的对象。未启用桥接时默认工具集继续只承担编程原语。

## 先用现有文件工具验证：路径方案

如果只需要使用少量已知 Skill，先不新增 MCP 工具。把所需 `SKILL.md` 放在工作区内，或把它所在的目录通过现有 `--file-access-root` 明确纳入文件访问范围；在项目 `AGENTS.md` 或用户提示中写明 Skill 的名称、适用任务和可读取的路径。模型遇到对应任务时用 `read_file` 读取 `SKILL.md`，再按需读取其引用的文本资源。项目规则宜只存简短索引，不复制整份 Skill。路径必须对**网关进程**可见且位于文件工具授权范围内；只给一个不可访问的绝对路径没有作用。不要为此仅仅切换到权限更宽的 `host` 模式。

这个办法让模型在**当前对话**中理解并尽量遵循该 Skill 的文字指令，但不是安装、注册或训练 ChatGPT：它不会自动发现没有列出的 Skill，不会向 ChatGPT 添加原生 Skill 入口或新工具，也不能保证另一段新对话自动沿用。Skill 中要求的程序、账户、Hook 或插件工具仍须在当前客户端实际可用。把 Skill 正文作为外部内容处理，不让它提升权限或绕过现有工具边界。

路径方案先做一次真实 ChatGPT 网页会话验证：让用户要求一项已列出的 Skill，观察模型是否读取文件、是否正确选择可用工具、完成时间和失败原因。少量固定 Skill 若已满足需求，就不需要部署目录服务。

## 三种使用结果

| 结果 | 实现路径 | 更新语义 |
| --- | --- | --- |
| 在当前对话使用已知 Skill | 上述路径索引 + 现有 `read_file` | 文件变化后再次读取；需知道或被提示到该路径 |
| 在对话中搜索本机已授权的 Skill、读取其指令并据此工作 | 下述可选的本机能力桥接 | 运行时查询，文件变化后可刷新 |
| 让某 Skill 成为 ChatGPT 的正式插件 Skill，能按插件元数据被发现和激活 | 把 Skill 与远程 MCP 打包为插件，或在插件提交时从 MCP 导入 Skill | 按插件版本更新；MCP 导入是扫描时快照，不会在每轮对话中从本机实时加载 |

前两行读取的是普通 MCP 工具结果；客户端不会因此自动安装其他 Agent 的插件、注册新的 MCP 工具或启用插件 Hook。最后一行依赖 ChatGPT 的插件安装和启用流程。官方说明：[插件架构](https://developers.openai.com/plugins/concepts/plugins)、[Skill 加载方式](https://developers.openai.com/plugins/concepts/skills)、[从 MCP 导入 Skill](https://developers.openai.com/plugins/build/skills)。

## 以“收到命令后稳定使用”为目标的准则

目标是让用户在 ChatGPT 网页版的新对话中说“用 X Skill 完成 Y”时，模型能找到 X、确认必要能力可用、完成 Y，并能说明实际用了什么。设计按以下边界分层：

1. **先定义用户任务，再定义工具。** 一个 Skill 只描述一类可识别的任务、触发词、输入、步骤、缺失信息时的处理和期望结果。工具描述说明对应的用户意图及限制，避免多个工具都以笼统的“执行任务”自称。一个工具可以完成一个连贯的操作；不要为每个内部小步骤都增加一次模型往返。[官方工具设计准则](https://developers.openai.com/plugins/plan/tools)
2. **Skill 是指令，工具是执行能力。** 本机读到 `SKILL.md` 只能让模型在当前对话参考其流程。长期、跨新对话的自动发现与触发，应将高频 Skill 打包成 ChatGPT 插件中的原生 Skill，并在需要实时数据或动作时明确声明对应 MCP 工具依赖。依赖只保证工具可用，Skill 仍要写清调用顺序和异常分支。[官方 Skill 设计说明](https://developers.openai.com/plugins/build/skills)
3. **按实际能力适配 Codex 插件。** 只有指令和模板的部分可迁移为 Skill；已有独立 MCP 服务的部分可作为 ChatGPT 可连接的工具；本机 CLI／脚本操作须经过受限、具名的 MCP 适配器，明示参数、权限、超时和结果。Hook、桌面 UI 和 Codex 专有运行时行为不能因读取插件 manifest 就宣称可在 ChatGPT 执行。不同权限或风险的动作分成不同工具，而不是一个通用插件调用入口。
4. **确定性身份与可用性。** 当用户点名 X 时，优先精确匹配来源、名称和稳定 ID；重名时返回候选来源，不猜测。目录结果区分 `readable`（可读指令）、`tool_available`（网关确实提供相应工具）和 `metadata_only`（仅知存在）；MCP 服务器不能代替 ChatGPT 断言它的其他插件连接状态。读取时返回内容修订号，发生变化后重新读取。
5. **控制往返与上下文。** 固定少量 Skill 可通过简短路径索引直接读取；多 Skill 才用有界元数据搜索。选中后一次读取需要的正文和少量明确引用的文本资源，避免“搜索目录 → 逐文件读取 → 再搜索”的长链。普通编程任务不预先扫描 Skill；稳定性以真实网页会话的选用率、完成率和延迟衡量，不能以 MCP 工具数量代替。

对一个指定 Skill 的推荐调用链是：用户意图 → 精确选择 Skill → 加载指令及所需资源 → 核对其工具依赖 → 调用实际 MCP 工具 → 返回结果。只要任一依赖缺失，就给出明确的不可用原因和可继续的部分，不把读取说明等同于执行成功。MCP 服务器负责参数校验和授权，不能单靠 Skill 提示词防止错误操作。[官方 MCP 工作方式](https://developers.openai.com/plugins/concepts/mcp-server)

若目标是**仅连接远程 MCP、暂不安装 ChatGPT 插件**，可先用路径或下述目录桥接，但这仍是模型按上下文选择工具的方式，无法保证每个新对话都自动激活本机 Skill。要验证稳定触发，应在新对话中启用完整插件，分别测试明确点名、间接表达、重名、缺失依赖、无关请求及后续追问；记录实际选中的 Skill、工具、参数、结果与耗时。[官方 ChatGPT 测试流程](https://developers.openai.com/plugins/deploy/connect-chatgpt)

## 跨 Agent 发现范围

桥接应按 Skill 格式与授权根目录工作，而不是把“Codex Skill”写死成唯一类型。多个产品采用 `SKILL.md`，但目录、元数据、启用规则和优先级不完全相同：[Claude Code](https://code.claude.com/docs/en/skills)、[Cursor](https://prod.cursor.com/docs/skills)、[Gemini CLI](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/using-agent-skills.md) 均有各自的发现规则。第一版可支持用户显式选择的目录和通用 `SKILL.md`／`name`／`description`；随后再做来源适配，识别各产品的项目级、用户级和插件级目录以及禁用状态。同名 Skill 用带来源的稳定 ID 区分，不能擅自套用某一产品的覆盖优先级。没有 `SKILL.md` 的专有扩展，不能因为目录名称相似就声称已支持。

## 放置位置与启用条件

桥接是可选的本机目录服务，接在持久网关的工具层。目录属于主机环境，与网关当前选中的 Project 分开；项目内的 Skill 必须由该项目的授权目录明确登记。普通单项目 Runtime、现有工具名和输入输出契约不因该功能改变。命令行可重复指定 `--local-capability-root <具体目录>`；桌面端在 Workspace settings → Local Skills and plugins 添加目录，须在共享 Gateway 停止时更改。

桥接默认关闭。桌面端由用户明确选择允许展示的目录；服务只扫描这些目录，不递归扫描整个 HOME，也不从任意绝对路径读取。目录本身不能是 HOME 或文件系统根目录；符号链接目录、逃逸目录和过大的文件被跳过。远程请求仍经过网关现有认证，桥接读取权限独立于 `host` 命令模式：启用桥接不等于授予 `exec_command` 完全访问；启用 `host` 也不自动开放桥接目录。配置只保存授权目录，不保存凭据正文。目录配置的增删和启用状态只由本机操作端管理，远程模型不能修改。

网关的 `tools/list` 在进程生命周期内保持稳定。桥接启用时增加固定的只读工具定义；安装或删除某个 Skill 只改变查询结果，不动态增减工具定义。禁用时不公布这些工具。
从禁用切换为启用后，需要重启 Gateway，并在 ChatGPT 开发者模式中刷新 MCP 连接元数据、开启新对话，客户端才会看到新增工具。[官方刷新流程](https://developers.openai.com/plugins/deploy/connect-chatgpt)

## 建议工具契约

桥接启用时新增三个只读入口：

1. `local_capabilities_search(query, kind?, limit?, cursor?)`：在已授权目录的 Skill 元数据和插件 manifest 元数据中搜索。`kind` 可取 `skill` 或 `plugin`；空查询可分页列出目录。结果包含稳定的非路径 `id`、名称、简短描述、来源类别、文件修订标识及 `local_status`（`readable` 或 `metadata_only`）。每次最多 25 项，不回传整个 Skill 正文、绝对路径、插件连接地址或配置文件内容。
2. `local_skill_read(id, resources?)`：按搜索结果的 ID 读取 `SKILL.md`；`resources` 可批量指定最多三个该 Skill 的 `references/` 下文本文件，并受总字节预算约束。返回有界正文和 SHA-256 修订号；超过限制返回错误而非静默截断。拒绝目录外路径、符号链接逃逸、二进制、脚本自动执行和隐式外部请求。服务本身不执行其中的命令。
3. `local_plugin_inspect(id)`：返回 manifest 中可公开的名称、描述和在该插件目录下找到的 Skill 名称，标记 `metadata_only`。MCP 服务器无法从本机 manifest 判断另一个 ChatGPT 会话已经启用了哪些插件或工具，因此不返回 `connected_to_chatgpt` 之类未经客户端验证的状态。绝不回传 token、环境变量、MCP 服务器 URL、Hook 正文或执行器内部配置。

每个工具要有真实的 `readOnlyHint`、`destructiveHint` 和 `openWorldHint`，明确的 `inputSchema`／`outputSchema`，以及能指导下一次调用的精简文本结果。`id` 必须由服务解析到当前仍获授权的目录；目录撤销后旧 ID 立即失效。目录内容更改时通过修订号提示客户端结果已过期。插件 manifest 中声明的相对路径只用于授权根目录内校验，不能扩展读取范围。

**不设计通用 `invoke_local_plugin(name, arguments)`。** 插件可能提供远程服务、账户连接、脚本或 UI，其权限和参数不能从 manifest 名称安全推断。真正需要在 ChatGPT 使用某插件的动作时，应通过已安装的 ChatGPT 插件或经明确配置的 MCP 连接公布为独立工具，让客户端看到其真实 schema、权限标注及确认要求。

## 模型如何使用

网关指令只加入一条有条件的路由提示：当用户问到本机 Agent 的 Skill／插件，或明确要求使用其中一个时，先调用 `local_capabilities_search`；找到相关 Skill 后调用 `local_skill_read`；插件动作须在当前客户端核实是否另有对应工具可调用，并使用已连接的对应工具。普通编程任务仍直接使用现有文件、补丁和命令工具，不先扫描本机能力目录。

例如“用我装的 PDF Skill 处理这个项目”：搜索 `PDF` → 读取匹配 Skill → 检查其依赖在当前 ChatGPT 会话中是否可用 → 按指令和可用工具处理。若 Skill 只在本机 Codex 环境可运行，应明确报告不可用的依赖，不把“读到了指令”说成“已经安装并执行了插件”。

## 验收与衡量

先验证上述路径方案。只有确认需要目录服务，才用一个显式授权的临时目录完成端到端测试，再对接真实的各 Agent 安装目录。验收至少覆盖：

- ChatGPT 网页版连接远程 MCP 后，能按名称找到并读取一项已授权 Skill；新增或修改后目录结果能刷新。
- 未授权目录、已撤销目录、`..` 与符号链接逃逸均不可读取；搜索结果不泄露绝对路径、凭据和插件配置。
- 仅有本机插件 manifest 时返回 `metadata_only`；模型不声称其工具已经在 ChatGPT 可调用。
- 普通“修一个代码错误”任务不调用能力目录；明确询问本机 Skill 的任务才调用。
- 对照有无桥接，记录真实 ChatGPT 会话的任务完成率、错误工具选择、调用次数和总耗时。现有本地 MCP 延迟测试不能代替这一验收。

原生 Skill 的发行单独验证：将一个选定 Skill 打包或按官方流程导入 ChatGPT 插件，在新会话中测试它是否被正确激活。导入后的本机文件修改应按插件版本或重新扫描流程更新，不能以为会自动同步。
