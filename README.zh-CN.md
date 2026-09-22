# Coding Tools MCP

[English](README.md) | **简体中文**

> 让任何 AI 聊天应用或 agent，都能安全地上手你的代码仓库。

[![PyPI](https://img.shields.io/pypi/v/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![npm](https://img.shields.io/npm/v/coding-tools-mcp)](https://www.npmjs.com/package/coding-tools-mcp)
[![Python](https://img.shields.io/pypi/pyversions/coding-tools-mcp)](https://pypi.org/project/coding-tools-mcp/)
[![compliance](https://github.com/xyTom/coding-tools-mcp/actions/workflows/compliance.yml/badge.svg)](https://github.com/xyTom/coding-tools-mcp/actions/workflows/compliance.yml)
[![release](https://github.com/xyTom/coding-tools-mcp/actions/workflows/release.yml/badge.svg)](https://github.com/xyTom/coding-tools-mcp/actions/workflows/release.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Coding Tools MCP 是一个**macOS 优先、模型中立的编程运行时**，通过
[Model Context Protocol](https://modelcontextprotocol.io) 对外提供服务：
文件读取与搜索、结构化多文件补丁、命令执行、交互式命令、git 操作——
一个服务器，任何 MCP 客户端都能驱动。Claude Desktop、Claude Code、Codex、
Cursor、Cline、VS Code、Windsurf、Gemini CLI，或你自己写的 agent，拿到的
都是同一套稳定的默认工具目录：默认限定在单一工作区内，由显式权限模式
层层把关。

[![观看演示](https://img.youtube.com/vi/N9lQaXt1eqQ/maxresdefault.jpg)](https://youtu.be/N9lQaXt1eqQ?si=LyEwvzzQF6QjUxR0)

## 为什么用它

- **让聊天应用变成编程 agent。** Claude Desktop——或任何 MCP 聊天客户端——
  用你已有的订阅直接获得真实的仓库访问能力，无需额外产品。
- **安全是产品本身，不是附加项。** 每个服务器进程绑定一个工作区根目录；
  绝对路径、`..` 穿越、符号链接逃逸一律拒绝；权限模式对网络访问、shell
  展开、内联脚本和破坏性命令逐项把关；Linux 上还有
  [Landlock](docs/security-boundary.md) 提供内核级文件系统隔离。显式启用
  `host` 模式后，命令执行会退出这层边界，以满足完整本机开发需要。
- **运行时不替模型做 Agent 工作。** 它只提供编程原语；计划、审查状态、任务
  跟踪、Skills 与 Agent 编排都留给调用它的模型或客户端。
- **为上下文窗口精打细算。** 工具结果按设计做摘要、分页与封顶；在确定性
  dogfood 工作负载上，序列化结果字节数相比上一版本下降 37%，任务完成率不变。

## 快速开始

用你手头已有的工具链启动（服务器本体是 PyPI 上的 Python ≥ 3.11 包；
npm 包是一个轻量启动器，会通过 `uv` 或 `pipx` 拉起服务器）：

```bash
uvx coding-tools-mcp --stdio --workspace /path/to/repo   # Python 工具链
npx coding-tools-mcp --stdio --workspace /path/to/repo   # Node 工具链
```

接入 Claude Desktop、Claude Code、Codex、Cursor、VS Code、Windsurf、
Gemini CLI 或 Cline——各家的 JSON 配置完全相同（偏好 Node 的话把 `uvx`
换成 `npx` 即可）：

```json
{
  "mcpServers": {
    "coding-tools": {
      "command": "uvx",
      "args": ["coding-tools-mcp", "--stdio", "--workspace", "/path/to/repo"]
    }
  }
}
```

然后对你的客户端说一句：*"跑一下测试，把第一个失败修了。"*

完整工具集保持精简并直接暴露。`runtime_doctor`、项目概览/规则、检查发现、
代码诊断和 Git 历史都无需二次工具发现。运行时不再内置另一套 workflow 引擎。

想用 HTTP？去掉 `--stdio`，服务器就在
`http://127.0.0.1:8765/mcp` 上讲 Streamable HTTP。两代协议在两种 transport
上同时提供：支持 MCP `2026-07-28`，同时继续支持握手时代的
`2025-11-25` 与 `2025-06-18`。Persistent Project Gateway 只用 MCP session
绑定明确的 Project；普通单 workspace runtime 不维护传输会话。
一行安装脚本、各客户端的完整接入指南和排障见
[docs/quickstart.md](docs/quickstart.md) 与
[docs/mcp-client-config.md](docs/mcp-client-config.md)。

## 七个值得一试的玩法

**1. 让 Claude Desktop 成为你的编程 agent。**
上面那份配置就是全部——你已经在付费的聊天窗口，现在能读代码、打补丁、跑测试、看 diff。

**2. 随时随地连回自己的电脑写代码。**

```bash
CODING_TOOLS_MCP_AUTH_MODE=bearer ./integrations/tunnels/tunnel.sh cloudflared /path/to/repo
```

回环地址绑定 + 带认证的 HTTPS 隧道（`cloudflared`、`ngrok` 或 Microsoft Dev
Tunnel）。手机上打开 claude.ai，指向 `https://<tunnel-host>/mcp`，就能驱动家里的
工作站。ChatGPT 与 Grok 通过各自的连接器设置同样接入。内置 Bearer token 与
OAuth 2.1 + PKCE（含 RFC 7591 动态注册）。
→ [docs/remote-mcp.md](docs/remote-mcp.md)

**3. 在一次性 Docker 沙箱里放心跑可疑代码。**

```bash
docker build -t coding-tools-mcp-sandbox:local .
docker run --rm --init -it -p 8765:8765 -v "$PWD:/workspace" coding-tools-mcp-sandbox:local
```

容器化的服务器，工具链和缓存都预配好——放心把 agent 指向一个来路不明的
PR，用完即毁。→ [docs/docker.md](docs/docker.md)

**4. 用图形界面操作。**

```bash
cd apps/desktop-client
npm install
npm run tauri dev
```

全新的 Tauri 桌面端使用系统 WebView，当前以 macOS 为正式支持与持续测试平台：
按工作区管理配置、一键启停服务器与隧道、凭证写入系统钥匙串、剪贴板
助手和实时健康检查。右上角可直接复制网页版 ChatGPT 所需的公网 MCP 地址和
授权口令；公网隧道尚未就绪时不会误复制本地 `127.0.0.1` 地址。支持英文与简体中文。
→ [桌面客户端](apps/desktop-client/README.md)

**6. 保持一个活着的交互式命令。**`exec_command` 在真实 PTY 下启动 REPL 或
调试器；`write_stdin` 跨轮次喂输入；`read_output` 分页读取长输出；
`kill_command` 干净收尾。长时进程是一等公民，配有命令看门狗与有界缓冲。

**7. 给自研 agent 装上生产级的"手"。**用 Anthropic SDK 或任何框架搭 agent
循环？别再手写文件和执行工具——对着这个服务器讲 MCP，整个安全边界直接
继承。→ [docs/embedding.md](docs/embedding.md)

## 工具目录

默认提供一套刻意保持精简且如实标注的目录。`apply_patch` 是唯一的直接文本/
源码编辑原语：分阶段、基线校验、跨文件原子提交并支持回滚。Git 写入、branch、
worktree、构建系统、Xcode、SwiftPM、Homebrew 等工作流直接通过 `exec_command`
调用系统原生命令，不再各自包装成 MCP 工具。

所有启用能力都直接出现在 `tools/list`；完整工具清单与选用提示统一维护在
[工具与 Schema](docs/tools-and-schemas.md)，不再内置 MCP 二次工具发现流程。

`runtime_doctor` 会进行非破坏性的 macOS 运行时体检，返回 Xcode、Swift、
SourceKit-LSP、codesign、notarytool、Homebrew、Git、LSP、沙箱状态与网络策略。
无需为普通修改建立任务、计划、审查和检查点。

网络命令策略可独立选择 `--network-policy deny|allowlist|unrestricted`。
allowlist 模式下可重复使用 `--network-allow-domain github.com`，子域可写成
`*.example.com`。不在列表中的目标，以及无法从命令行静态确定目标的联网命令，
都需要显式授权。这里是命令策略层的门控，并不是操作系统级出站防火墙；旧的
`--allow-network` 仍兼容，并等价于 `--network-policy unrestricted`。

仓库根部的 `AGENTS.md`/`CLAUDE.md` 会自动载入，并随 `initialize` 的
`instructions` 下发；不握手的客户端则通过 `server/discover` 拿到同一份内容。
工具的 `content` 是给 agent 看的精炼文本，`structuredContent` 则是完整稳定的
机器结果。Schema 与结果封装：
[docs/tools-and-schemas.md](docs/tools-and-schemas.md) ·
[docs/runtime-contract-v0.4.md](docs/runtime-contract-v0.4.md)

## 安全边界

| 模式 | 适用场景 | 放行范围 |
| --- | --- | --- |
| `safe`（默认） | 日常 agent 工作 | 文件工具与常规命令；疑似联网命令、shell 展开、内联脚本、破坏性命令均需显式授权 |
| `trusted` | 本地开发 | 放开网络、shell 展开与内联脚本；保留敏感值过滤与破坏性命令检查 |
| `dangerous` | 仅限隔离容器/虚拟机 | 关闭普通 `exec_command` 权限门；工作区路径边界依然生效，显式设置的 `deny`/`allowlist` 网络策略仍然生效 |
| `host` | 显式的本机完全开发 | 关闭普通命令权限门与 Landlock，继承服务进程真实的 HOME、临时目录、SSH agent、Git 凭据和完整环境；显式设置的 `deny`/`allowlist` 网络策略仍然生效 |

递归列举与搜索默认排除 `.git`、`node_modules`、构建产物、虚拟环境和常见
缓存。命令在工作区限定的 cwd 下运行，带超时与输出上限；除显式启用的
`host` 模式外，环境会经过清洗。
支持 Landlock 的 Linux 主机获得内核级文件系统隔离；其他平台会收到明确
警告——这仍不是完整的操作系统级沙箱，真正不可信的工作请用 Docker 镜像或
虚拟机。详见：
[SECURITY.md](SECURITY.md) · [docs/security-boundary.md](docs/security-boundary.md) ·
[docs/permission-modes.md](docs/permission-modes.md)

## 遥测

服务器会发送匿名使用遥测（每工具的成功率/延迟计数与版本/平台维度——
绝不包含路径、参数、命令或文件内容），用于确定修复优先级。设置
`CODING_TOOLS_MCP_TELEMETRY=off` 或 `DO_NOT_TRACK=1` 可关闭；CI 环境自动
关闭。`CODING_TOOLS_MCP_TELEMETRY=debug` 会把每个事件打印到 stderr 而不
发送。完整事件清单与承诺见 [docs/telemetry.md](docs/telemetry.md)。

## 证据、Dogfood 与 SWE-bench

每个版本都经由 tag 触发的流水线发布：合规套件、真实工作负载基准和
SWE-bench 评测与 registry 发布运行在同一个 commit 上——PyPI 与 npm 均走
trusted publishing，npm 带 provenance。Dogfood 效率指标可复现
（`make dogfood-smoke`），报告存于 `reports/`。本仓库不宣称任何模型生成的
SWE-bench 榜单成绩——[docs/swe-bench.md](docs/swe-bench.md) 写明了测了什么、
没测什么。更多：[COMPLIANCE.md](COMPLIANCE.md) ·
[BENCHMARK.md](BENCHMARK.md) · [docs/dogfood.md](docs/dogfood.md)

## 文档

| | |
| --- | --- |
| 文档导航 | [按主题浏览文档](docs/README.md) |
| 上手 | [快速开始](docs/quickstart.md) · [客户端配置](docs/mcp-client-config.md) · [排障](docs/troubleshooting.md) |
| 远程与沙箱 | [Remote MCP](docs/remote-mcp.md) · [Docker 沙箱](docs/docker.md) |
| 工具与契约 | [工具与 Schema](docs/tools-and-schemas.md) · [运行时契约](docs/runtime-contract-v0.4.md) · [权限模式](docs/permission-modes.md) |
| 命令执行 | [Exec 配方](docs/exec-command-recipes.md) · [Exec 排障](docs/troubleshooting-exec.md) |
| 集成 | [嵌入指南](docs/embedding.md) · [npm 启动器](packages/npm-launcher/README.md) |
| 安全与质量 | [安全策略](SECURITY.md) · [安全边界](docs/security-boundary.md) · [CI 与测试](docs/ci-and-tests.md) · [已知限制](docs/limitations.md) · [竞品分析](docs/competitive-analysis.md) |

## 开发

```bash
python -m pip install -e ".[dev]"
make ci        # lint、类型检查、测试、协议/集成套件与各项门禁
```

完整门禁矩阵见 [docs/ci-and-tests.md](docs/ci-and-tests.md)。

## 许可证

本项目基于 [Apache License 2.0](LICENSE) 发布。

如果你使用了本项目的代码、文档、实质性实现细节或衍生成果，请保留版权
声明、许可证声明与 [NOTICE](NOTICE) 文件，并清晰注明出处。

Project: Coding Tools MCP  
Author: Coding Tools MCP Contributors  
Source: https://github.com/xyTom/coding-tools-mcp

引用元数据见 [CITATION.cff](CITATION.cff)。
