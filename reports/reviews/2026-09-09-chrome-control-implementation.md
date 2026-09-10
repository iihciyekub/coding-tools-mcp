**Chrome 控制与轻量桌面包：实施记录，2026-09-09。**

本轮已落实“优先使用系统环境，否则下载到独立用户环境”的交付方式。桌面版本为 0.3.16，核心为 0.3.5。现有 Chrome/Playwright 功能与公共工具入口继续保留；大型第三方运行依赖转到 App 外部。没有将扩展重写为完整浏览器后端，也没有发布到远程或替换 `/Applications` 中的旧版应用。

| 产物 | 原 v0.3.15 | 本地 v0.3.16 |
| --- | ---: | ---: |
| App 常规文件逻辑体积 | 176.47 MiB | 13.49 MiB |
| DMG | 65.92 MiB | 4.40 MiB |

App 缩小约 92.36%。随包的项目代码与锁定依赖清单只有 147,924 bytes；不包含 Python 解释器、Playwright、Node 或 Chromium。依赖仍是运行功能所需，会按需下载并缓存，因此这里的减幅是 App 交付体积，不是安装全部外部依赖后的总磁盘占用。首次试打包的未封装签名 DMG 约 4.03 MiB；表中是完成本地 ad-hoc 签名并重新封装、挂载验证后的最终测试包。

**运行方式已落地。** 优先使用显式运行命令、相同核心版本的系统 CLI、具备匹配核心与依赖的 Python。否则在应用本地数据目录创建版本及内容摘要隔离的持久环境。uv 优先复用系统 Python，缺少时可以下载解释器；没有 uv 时，已有 Python 3.11+ 可通过 venv/pip 准备环境。两者都没有时，应用提供 Resources → Install uv 指引。依赖下载使用锁定版本和哈希校验，系统 Python 包不会被修改。

首次准备拥有独立五分钟截止、错误日志、进程取消和 OS 文件锁；成功通过健康检查后才标记可复用。下载期间不持有界面状态锁，支持查看状态和取消启动。取消后不会延迟启动服务器。已准备的环境在没有 uv、没有新的网络下载时可复用。旧版本环境保留，避免让既有 Native Messaging 注册失效；更换解释器后需重新运行扩展安装工具。详细用户说明集中在 [桌面 README](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/README.md)。

**浏览器修复已落地。** 同名按钮使用唯一 CSS 路径；密码值默认不进入快照；增加表单可访问名称、ARIA/focusable 控件、disabled/checked 状态及元素截断提示。快照文本不再静默只保留前 100 个元素；网络文本分别显示事件与资源；inspect 返回正确匹配数。截图传递操作 timeout，Playwright 1.60+ 附着时不覆盖 Chrome 的默认 focus/media/download 行为。

Native Messaging 能使用外部虚拟环境的邻接入口，模块式安装也可生成固定解释器的轻量启动器。修复超时连接迟到回复引发整个 host 退出的问题，加入 pending 清理和消息大小检查。扩展对同一标签的执行进行排队，JavaScript 执行具有截止时间。

**验证结果。**

- Python 全套发现测试运行 280 项：无失败，7 项按平台或配置跳过。最后的浏览器修改另跑 9 项定向测试，包括 3 项隔离 Chrome 实机测试，全部通过。
- Rust 20 项常规测试通过，包含准备取消和停止启动意图；2 项外部环境集成测试另行显式运行并通过。
- 外部环境验证覆盖 uv 准备、移除 uv 后复用、已安装 CLI 复用，以及只有兼容 Python、没有 uv 的 venv/pip 路径。
- 使用最终 App 内的资源建立临时环境，完成真实 MCP initialize/tools/list 握手，确认 51 个工具含 browser_snapshot 和 chrome_extension_install。
- 2 项扩展 JavaScript 测试验证同页串行、超时后后续请求仍可执行。
- 前端类型检查、3 项前端测试与构建通过；Rust fmt/clippy、相关 Python ruff/mypy、版本元数据和差异空白检查通过。
- App 已构建；DMG 校验通过；只读挂载后的 App 通过 codesign --verify --deep --strict。此为本地 ad-hoc 测试签名，未做 Developer ID 发行签名或公证。

本机实测平台是 macOS arm64。Windows/Linux 外部环境验证已加入 CI，但本轮没有远程运行这些平台。当前仍保持每次浏览器调用短连接；iframe/shadow 的全面语义采集、扩展统一后端、多 Chrome profile 路由属于后续架构工作。本轮解决的是交付体积、外部环境准备及已确认的一组可靠性问题。

最终安装包：[Coding Tools MCP 0.3.16](</Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/src-tauri/target/release/bundle/dmg/Coding Tools MCP_0.3.16_aarch64.dmg>)。详细体积数据见 [测量记录](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/reports/reviews/2026-09-09-thin-app-measurements.json)。
