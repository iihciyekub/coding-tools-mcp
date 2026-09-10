**Coding Tools MCP：Chrome 控制体积与实现审查，2026-09-09。**

后续状态：本轮已实施外部运行环境与部分可靠性修复，见[实施记录](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/reports/reviews/2026-09-09-chrome-control-implementation.md)。以下保留审查时的基线事实。

控制、操作和理解已有 Chrome，不必然需要目前的应用体积。当前体积主要来自“Python Playwright + 随包 Node driver + 自包含 Python runtime”的交付选择。对本项目的需求，建议逐步采用“现有 Chrome 扩展作为默认控制后端，Playwright 作为可选高级自动化后端”，保留公共 MCP 接口。不能直接删除 Playwright：现有扩展尚未承接完整浏览器功能。

本轮检查当前工作区代码（Git HEAD 为 `2267e09`，另有未提交修改；core 0.3.4、desktop 0.3.15），核对本地 macOS 产物，运行定向单元测试、隔离 Chrome 合成页面测试及桥接/文本结果探针。没有修改既有源码、安装配置或浏览器用户数据，没有重新构建发布包。已有产物测量与当前源码审查分别列出，不能把静态体积差额当作优化完成后的发布体积。

**实际体积来自哪里。** 统计对象是本地 2026-09-08 19:34 生成的 [v0.3.15 App](</Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/src-tauri/target/release/bundle/macos/Coding Tools MCP.app>)。逐个常规文件累加逻辑字节，排除符号链接重复计数；MiB = 1,048,576 bytes。它与 `/Applications/Coding Tools MCP.app` 的文件数、总量及 Node 哈希一致。该口径不是 Finder/du 的磁盘占用，也不包括构建缓存和其他版本安装包。

| 构成 | 逻辑字节 | MiB | 对体积的含义 |
| --- | ---: | ---: | --- |
| 整个 App | 185,043,512 | 176.47 | 当前安装内容总量 |
| Playwright 目录 | 136,266,292 | 129.95 | 占 App 73.64% |
| 其中 Node 可执行文件 | 120,253,952 | 114.68 | 包含在上一行内；单独占 App 64.99% |
| Python 动态库 | 17,341,120 | 16.54 | 自包含 Python 的主要成本 |
| Tauri 桌面主程序 | 13,868,992 | 13.23 | 不是主导成本 |
| cryptography 的 Rust 扩展 | 11,742,160 | 11.20 | 可审核的生产依赖精简候选 |
| macOS Native Messaging launcher | 156 | 可忽略 | 已复用同一 Python runtime |

没有发现随包 Chromium 浏览器本体。Node 为单一 arm64 可执行文件，不能靠去掉另一套架构减半。Chrome 扩展的 JS 和 manifest 合计仅 3,438 bytes，但这个数字只代表当前简单扩展代码，不代表完整功能替代品的最终大小。

| 本地安装包 | 字节 | MiB |
| --- | ---: | ---: |
| v0.3.13 DMG | 4,468,019 | 4.26 |
| v0.3.14 DMG | 63,746,432 | 60.79 |
| v0.3.15 DMG | 69,122,976 | 65.92 |

前两个位于 `target/release/bundle/dmg`；[v0.3.15 DMG](</Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/src-tauri/target/release/bundle/macos/Coding Tools MCP_0.3.15_aarch64.dmg>) 位于 `target/release/bundle/macos`。这些是本机已有文件，不是对所有线上发布版本的独立核验。

体积变化叠加了两件事。`2267e09 add control chrome` 的父提交 `da5abbe` 中，Tauri 构建只构建前端、没有资源目录声明；Chrome 提交同时引入了内置 Python runtime。因此旧的约 4 MiB 是依赖外部运行环境的薄壳，不能把全部增量都归因于 Chrome 控制。当前 [打包脚本第 50 行](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/build-bundled-runtime.mjs:50) 显式收集完整 Playwright，[pyproject.toml 第 10 行](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/pyproject.toml:10) 也将其作为所有安装方式的必需依赖。

**目前有两套独立的 Chrome 通路。**

| 工具入口 | 实际通路 | 当前能力和限制 |
| --- | --- | --- |
| `browser_*` | Python → Playwright → 随包 Node → CDP → 已启动 Chrome | 提供快照、截图、点击、输入、JS、网络和控制台；需要 Chrome 调试端口 |
| `chrome_extension_*` | Python → Unix socket → Native Messaging host → 扩展 → Chrome API/CDP | 当前只有状态、扩展列表、标签列表、JS 求值、扩展消息；不需要调试端口；桥接安装/请求目前仅支持 macOS |

分派可见 [server.py:4127](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/server.py:4127) 和 [server.py:4172](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/server.py:4172)。二者没有统一后端层。前者的 tab ID 是 CDP target 字符串，后者是 Chrome 整数 tab ID，不能直接互用；求值函数调用和不可序列化值处理也有差异。扩展源码的 [action 分派](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_extension/service_worker.js:13) 明确显示，安装扩展并不会让 `browser_*` 自动改走轻量路径。

这里的“理解”来自 Chrome 提供页面结构、状态和截图，再由调用 MCP 的模型推理。项目不需要为了这项能力额外内置一个大模型或浏览器引擎。重要的是提供准确、完整且有边界的信息。Playwright MCP 本身也采用结构化可访问性快照，并提供连接现有浏览器的扩展模式，可作为交互设计参考；这并不意味着直接引入它会减少 Node 交付成本。[Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)

**已经复现、应优先修复的问题。** P1 表示可能导致错误操作、非预期敏感数据返回或连接故障；P2 表示明显影响功能质量、可靠性或资源成本。下面的问题不需要等待架构迁移才能修。

| 优先级 | 发现及验证 | 修复方向 |
| --- | --- | --- |
| P1 | 快照为同一父节点下两个 `button name=action` 生成相同 selector；按 Second 按钮返回的 selector 点击，实际触发 First。原因是有 name 时不再加位置约束，而点击始终取 `.first`。[定位生成](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:208)、[点击](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:353) | 生成唯一元素引用，操作前验证对应页面和元素；保留现有 selector 兼容入口，新增严格匹配/歧义错误，不再让快照引用无声命中其他元素。 |
| P1 | `input[type=password]` 的合成密码明文进入 snapshot.elements.text。[browser.py:229](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:229) 直接读取所有 input.value。 | 密码字段保留类型、名称、是否有值，默认隐藏值；名称与值分字段。普通快照不应自动返回密码。 |
| P1 | 扩展客户端超时关闭 socket 后，迟到回复引发 BrokenPipeError；异常传播至 host 主循环，使整个 host 退出。已用内存 socketpair 复现。[chrome_native_host.py:118](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_native_host.py:118)、[主循环](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_native_host.py:130) | 将单个连接失败限制在单个请求；加入 pending 到期清理和端到端 deadline。扩展的 `timeout_ms` 当前仅限制客户端等待，没有传给 Runtime.evaluate。[chrome_bridge.py:203](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_bridge.py:203) |
| P2 | label 关联的输入框名称为空；合成页面中的 iframe 按钮、开放 shadow root 按钮和自定义 role=checkbox 都没有出现在交互元素列表。[browser.py:219](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:219) | 提供 role、accessible name、checked/disabled/expanded 等状态，支持 frame/shadow、正文层次和稳定引用。 |
| P2 | 设置 max_elements=1 后没有元素截断标记；只返回 text_truncated=false。文本 renderer 又固定只输出前 100 个元素，低于默认采集的 150 个。[browser.py:221](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:221)、[tool_results.py:356](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/tool_results.py:356) | 统一文本/结构化输出预算；返回元素总数、是否截断和继续读取方式。 |
| P2 | network 的 events=[]、resources 有一条记录时，模型文本仍只有“No events captured.”。构造数据验证通过；结构化内容仍保留资源。[tool_results.py:388](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/tool_results.py:388) | 分别展示 events 和 resources，不以发现一个空数组就结束渲染。 |
| P3 | 多个 button 匹配时 inspect.matched 返回 1，因为先取 `.first` 再 count。[browser.py:522](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:522) | 对完整 locator 计数，再选择实际检查的元素。 |

**连接复用有直接性能收益。** 当前每个工具调用都启动 Playwright/Node、附着 Chrome，再停止 driver，见 [browser.py:58](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:58)。在临时独立 Chrome 152.0.7977.83、Playwright 1.62.0 的合成小页面上，对同一个 snapshot 函数分别运行 7 次，结果如下。复用版本只在实验进程内替换连接管理，没有修改源码。

| 模式 | 7 次耗时，ms | 中位数 |
| --- | --- | ---: |
| 现有每次启动/连接 | 151.46、153.23、151.00、156.38、180.32、153.61、151.85 | 153.23 ms |
| 已建立连接复用 | 8.97、3.47、3.10、3.05、3.00、5.32、4.63 | 3.47 ms |

该结果隔离了小页面上的连接开销，不包括网络页面加载、MCP 公网传输或模型推理，不能外推整体任务提速倍数，也没有测进程峰值内存。实现上应让专用 browser worker 拥有连接，附加断线恢复、空闲释放及每页操作队列；不能把同步 Playwright 对象直接在多个 HTTP 请求线程之间共享。

**其他可靠性问题有明确代码证据，但尚未全部实机复现。**

- 当前 `connect_over_cdp` 没有 `no_defaults=True`。本机 Playwright 1.62 默认代码会启用 focus emulation；官方也说明默认附着会影响 focus、媒体及下载行为。随后用 visibilityState 猜 active tab 不够可靠。应采用不覆盖日常浏览状态的连接方式，并从扩展查询 focused window + active tab。`no_defaults` 在 1.60 新增，而项目依赖下限为 1.55，必须配套版本能力判断或提高最低版本。没有做用户前台 Chrome 焦点实验，不能把这个风险写成已复现的前台选错页。[Playwright CDP API](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp)
- `browser_evaluate` 当前已增加 CDP 执行 timeout，不应重复报告历史缺陷。其他路径仍不一致：截图没有传入请求 timeout，snapshot/title/visibility 求值也没有统一剩余时间预算；截图还有像素和总字节预算缺口。[browser.py:252](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:252)、[求值修复](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:282)
- 快照先读取全页 innerText，再在 Python 截断；交互元素先对全部候选计算样式和几何，再 slice。max_chars/max_elements 控制输出，不充分控制采集成本。宜在页面端按 scope/viewport/数量停止，支持分页。[browser.py:219](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:219)
- Console/network 仅监听本次连接后的短时间窗口。连续“点击→查网络”可能漏掉点击时发生的事件；network 的 PerformanceResourceTiming 只能补部分资源信息。应在连接存续期间维护有界事件缓冲，按 cursor/since 读取。[browser.py:412](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:412)、[network](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:474)
- Native host 使用固定 `chrome-native.sock`，每个实例启动和退出都会 unlink。多个 Chrome profile 同时加载扩展时，实例可能覆盖/删除彼此的连接入口。应提供实例 ID、profile/session 注册及拥有者清理规则。[chrome_native_host.py:24](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_native_host.py:24)、[启动](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_native_host.py:87)
- 扩展每次 execute 都 attach/detach，同一页请求又可以并发执行，没有 tab 队列、租约或 onDetach 恢复。扩展成为主后端之前必须补这些生命周期能力。[service_worker.js:46](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_extension/service_worker.js:46)、[消息处理](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_extension/service_worker.js:77)
- Native Messaging 消息大小应分方向处理：Chrome 限制 host→扩展单条 1 MB、扩展→host 64 MiB；本项目还设置了 4 MiB socket 请求、16 MiB host 读取和 8 MiB socket 响应限制。host 写入缺少相应边界检查。截图回传不是 1 MB 的方向，但未来全页截图仍需考虑本项目 8 MiB 响应限额和 Base64 膨胀。[host framing](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_native_host.py:28)、[Chrome Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)

本项目已经有 loopback CDP 限制、扩展 allowed_origins 和 0600 socket 权限，这些边界值得保留。扩展模式增加的是对用户当前登录态浏览器的控制能力，应明确选中的 profile/tab 和已授权客户端，而不是把它当作仅限仓库目录的操作。现有 socket 是同一 OS 用户级信任边界，不能把它夸大为任意远程网站均可直接连接的漏洞。若只做网页控制，`management` 权限服务的扩展枚举功能可以改成独立可选能力。[endpoint](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/browser.py:33)、[manifest](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_extension/manifest.json:7)

**打包还有几个独立优化与验证缺口。** macOS Native Messaging 已采用 156 字节 wrapper，复用已打包的 runtime；不存在需要再修一次的双 Python 问题。[build-bundled-runtime.mjs:59](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/build-bundled-runtime.mjs:59)

1. 生产构建从开发环境捕获了 cryptography：本地 PyInstaller Analysis 显示 `jwt.algorithms` 导入开发 `.venv` 内的 cryptography；开发依赖 MCP SDK 要求 `pyjwt[crypto]`，而产品直接依赖是普通 PyJWT、OAuth 当前仅 HS256。内存阻断 cryptography 导入后，HS256 签发/验证可用。因此约 11.20 MiB 是值得先验证的裁剪候选。应使用隔离、锁定的生产依赖构建环境，并验收完整 OAuth 流程后决定排除；没有做完整重打包，因此不是已兑现节省。[oauth.py:314](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/oauth.py:314)、[uv.lock:526](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/uv.lock:526)
2. Windows runtime 查找与 onedir 输出不一致：打包目录名不带 `.exe`，内部可执行文件带 `.exe`；Rust 查找逻辑却给两层名称都加后缀。可能导致内置 runtime 找不到，退回外部 PATH/uvx。属高置信度静态问题，未做 Windows 实机验证。[打包](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/build-bundled-runtime.mjs:50)、[runtime.rs:404](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/src-tauri/src/runtime.rs:404)
3. 非 macOS 仍单独构建一份 onefile Native host，但安装/请求逻辑明确仅支持 macOS。这份资源当前没有可用产品路径；应条件打包，或先补齐对应平台功能。没有 Windows/Linux 产物，不能给节省数字。[build-bundled-runtime.mjs:75](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client/build-bundled-runtime.mjs:75)、[chrome_bridge.py:65](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/coding_tools_mcp/chrome_bridge.py:65)
4. 桌面 CI 只构建前端并检查 Rust，不生成和启动实际内置 runtime；触发路径也没有覆盖决定 runtime 内容的 Python 核心、pyproject.toml 和 uv.lock。应添加最终包文件清单、体积预算及禁止开发环境回退的启动 smoke。[desktop.yml:5](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/.github/workflows/desktop.yml:5)、[构建步骤](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/.github/workflows/desktop.yml:59)

**不同优化方式的收益并不相同。**

| 做法 | 预期收益 | 判断 |
| --- | --- | --- |
| 默认包去掉 Playwright，完整功能由轻量扩展后端承接 | 当前文件树少 129.95 MiB；算术余额 46.52 MiB | 最大结构性收益；需先完成功能替代和兼容验证，最终数值必须重建测量 |
| 隔离生产环境，审核 cryptography | 当前 11.20 MiB 二进制是明确候选 | 可优先验证；保持认证功能和算法约束 |
| 审核 Playwright 类型定义和调试 UI 资源 | types 约 1.85 MiB、reporter/recorder/trace 等 UI 约 3.41 MiB | 小幅收益，仍须运行时回归 |
| 连接复用 | 降低重复启动与握手成本 | 改善延迟，基本不减安装体积 |
| Playwright 首次使用再下载 | 减少初始安装包 | 使用后的总依赖仍在；增加下载、离线、版本校验和回滚工作 |
| Rust strip、LTO、尺寸优化 | 作用于当前 13.23 MiB 主程序 | 放在后面，收益需实测；Tauri 官方提供相关配置建议 |
| 提高 DMG 压缩、改 onefile | 改变分发/提取形式 | 不消除 Node 或运行依赖；onefile 还增加解包与签名维护成本 |

Tauri 的 [App Size 指南](https://v2.tauri.app/concept/size/) 可用于最后阶段的二进制优化，但本项目主要成本并不在前端 UI。当前已采用 Tauri，也没有必要为这次问题重写整个桌面应用。

只删 `--collect-all playwright` 不会自动省去 Node：Playwright 自带 PyInstaller hook 也会收集其数据目录。Node 是 Python Playwright 实际用来执行 driver 的依赖。`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD` 也不能解决当前主要体积，因为本包原本就没有浏览器本体。不要直接删除 node 文件，也不要删整个 crypto 库后跳过认证验收。

**更适合本项目的实现路线。** 对“操作用户现在打开、已经登录的 Chrome”，我推荐在现有 Python core 内建立统一浏览器后端接口，让默认调用经过扩展桥接，保留 Playwright 适配器作为可选增强。不要把核心工具实现搬进 Tauri，以免 CLI/远程 MCP 与 GUI 耦合。

Chrome 自身提供 Accessibility、DOM、DOMSnapshot、Input、Page、Network、Runtime 等 CDP 域，扩展 debugger API 可以访问这些域；跨进程 iframe 还需要 target/session 管理。这说明轻量实现技术上成立，但自动等待、元素定位和错误恢复的工程成本仍然存在。[Chrome debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)

| 需求 | 默认轻量后端的实现方向 | 当前扩展是否已有 |
| --- | --- | --- |
| 标签页、窗口、导航 | tabs/windows API，并绑定选中的浏览器实例 | 仅有标签列表 |
| 页面理解 | 可访问性/DOM 语义快照，名称、状态、正文和稳定元素引用 | 只有任意 JS 求值，未封装完整快照 |
| 点击、输入、滚动、按键 | CDP Input 等操作，加可见性、遮挡、状态等待和动作后验证 | 未实现完整动作层 |
| 截图 | Page.captureScreenshot，限定范围、像素和字节 | 未实现 |
| 控制台、网络 | 会话内事件订阅与有界缓冲，支持 cursor | 未实现 |
| 跨 frame、重连、并发 | 实例、tab、frame/session 管理与每页队列 | 未实现 |

对正在使用的 Chrome，扩展模式还有实际接入价值：Chrome 136 起，默认用户数据目录不再接受 remote-debugging-port/pipe；使用调试端口通常需要独立 user-data-dir。这会让“操作已有日常登录态”和“启动专用自动化浏览器”变成不同体验。[Chrome 官方变更](https://developer.chrome.com/blog/remote-debugging-port)

扩展模式需要安装和授予调试权限，也需要处理受限制页面、DevTools 导致的 detach、Chrome 更新与多 profile。普通用户交付宜提供应用内状态/安装引导和正式扩展分发流程；当前手动开发者模式加载更适合开发阶段。不能把扩展包很小等同于这些工程工作已经完成。

如果明确不想安装扩展、主要操作独立测试 Chrome，则可以考虑 Python 通过 WebSocket 直接调用 CDP，去掉额外 Node driver。它仍有调试端口/专用 profile 的接入要求，并把 selector、自动等待、frame、重连和下载处理责任交给项目。它是替代路线，不建议同时维护第三套功能重复的默认后端。

如果目标扩大为复杂跨浏览器 E2E、成熟 locator 和自动等待，保留 Playwright 更划算。`puppeteer-core` 虽不下载 Chrome，但它仍是 JS 库；本项目如果因此增加一个需随包 Node 的组件，不会自然解决当前主要成本。[Puppeteer 官方安装说明](https://pptr.dev/guides/installation)

**页面理解应优先投资语义与定位质量。** 先返回诸如“复选框：同意条款，未选中；按钮：提交，可点击；输入框：客户名称”的语义信息，再给模型稳定引用。截图用于画布、布局和难以从结构判断的内容；无需每步全页截图或让模型反复生成大段 JS。Playwright 官方推荐 role/name/label 定位，其现有 `aria_snapshot` 也可在暂时保留 Playwright 的阶段使用，不必引入另一套大型依赖。[Locators](https://playwright.dev/python/docs/locators)、[aria_snapshot](https://playwright.dev/python/docs/api/class-locator#locator-aria-snapshot)

不要用 `element.click()` 或直接设置 `.value` 就宣称与 Playwright 动作等价。可靠动作需要正确事件、焦点、可见性与遮挡判断，等待合适状态，并检查页面是否真的变化。轻量后端节省的是随包依赖，不能省略这部分行为保证。

**建议按以下顺序实施，并以验收结果决定切换。**

1. 先修重复 selector、密码值返回、迟到回复关闭 host、文本结果遗漏；补对应少量高价值回归。建立当前最终包的大小清单。
2. 隔离并锁定生产构建依赖，验证 cryptography 候选；修 Windows runtime 路径，停止打包当前无产品路径的非 macOS host，加入最终包启动测试。这一步无需更改浏览器后端。
3. 为现有 Playwright 引入专用会话 worker、连接复用、事件缓冲及统一 timeout；改善语义快照和引用，保留当前 CDP tab ID 及公开工具兼容。
4. 将扩展补成满足同一内部契约的后端：实例/profile/tab/frame、稳定引用、deadline、错误、结果预算。优先完成日常导航、读页、点击、输入和截图，再补事件与复杂 frame。兼容旧 ID/selector 的请求明确路由到原后端，不静默改变含义。
5. 通过同一套合成页面与真实场景验收后，再把扩展设为默认、Playwright 移为可选依赖/组件。基础版仍保留公共 CLI、工具名和 schema 的兼容行为，缺失高级能力时返回明确能力说明。重建签名后的 App/DMG，再报告最终节省。

验收应包括：同名控件不会误点；密码默认不进入快照；label/shadow/frame/状态可理解；页面更新后的引用可检测失效；点击后的网络/控制台可追溯；超时/断连不会影响其他会话；两个 profile 不串线；无开发环境机器能运行；输出预算清晰且不会静默丢失；打包体积与冷/热调用耗时有回归记录。

**本轮验证边界。** 运行现有 2 项 browser evaluate deadline 测试和 8 项 schema drift 测试，共 10 项通过。隔离 Chrome 测试只在临时空白 profile 的合成页面上运行，退出后清理测试进程和目录；未触碰用户现有 Chrome 会话。另复现 NativeBridge 迟到回复异常和 network 文本遗漏。原始 Chrome 探针结果保存于 [验证数据](/Users/IIDEV/Documents/ii-research/coding-tools-mcp/reports/reviews/2026-09-09-chrome-control-probes.json)。本轮没有运行全量 CI、正式扩展端到端测试、Windows/Linux 实机测试，也没有产出瘦身后的发布包。
