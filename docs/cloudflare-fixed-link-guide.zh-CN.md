# Cloudflare 固定链接配置指南

适用于支持“连接设置 → 固定链接”的 Coding Tools MCP Desktop，包括 `coding-tools-mcp-dev`。

配置完成后，MCP 地址可以一直使用同一个域名，例如 `https://mcp.example.com/mcp`。
重启 App 不会更换这个地址；Mac 休眠、断网或退出 App 时，服务仍会离线。

## 1. 先确认准备条件

- 一个 Cloudflare 账号。
- 一个已接入 Cloudflare、可管理 DNS 的域名。
- App 中已有访问配置，且“更多 → 环境与安装”显示环境就绪。

本文以 `example.com` 为示例域名，操作时换成你自己的域名。

本机开发版在 **2026-10-16** 读取到的 Gateway 端口是 **29766**。下面据此举例；
**实际操作以 App“连接设置 → 固定链接”显示的本地地址为准**。
正式版与开发版的配置独立，不要直接套用另一版本的端口。

## 2. 查看 App 要求的本地地址

1. 打开 `coding-tools-mcp-dev`，点击 macOS 菜单栏中的 App 图标。
2. 如果首页正在连接或已经连接，先点击“停止”；需要二次点击时按提示确认。
3. 进入“更多 → 连接设置”，选择“固定链接”。
4. 记下页面显示的地址，例如：

   ```text
   http://127.0.0.1:29766
   ```

暂时留在这个页面，接下来先配置 Cloudflare。

## 3. 在 Cloudflare 创建 Tunnel

1. 登录 [Cloudflare 控制台](https://dash.cloudflare.com/)。
2. 按当前官方入口进入 **Networking → Tunnels**（网络 → 隧道）。
   部分账号仍通过 **Zero Trust → Networks → Tunnels / Connectors** 进入。
3. 点击 **Create Tunnel**。如果需要选择连接器类型，选择 **Cloudflared**。
4. 名称可填写 `coding-tools-mcp-dev`，然后创建。
5. 在 **Setup Environment / Install and Run** 页面选择 macOS 和对应架构。
6. 从安装命令中复制 **Tunnel token 本身**。

例如页面可能显示：

```text
sudo cloudflared service install <一长串 Tunnel token>
```

只复制最后的那串 token，通常以 `eyJ` 开头。**不要把整条命令粘进 App**。
这里需要的是 Tunnel token，不是 Cloudflare 的通用 API Token、账号 ID 或 Tunnel ID。

本流程由 App 启动并维护 `cloudflared`，无需另外执行 `service install` 安装系统常驻服务。
如果 Cloudflare 此时显示隧道未连接，可以继续配置路由；等 App 启动后再查看状态。

## 4. 给 Tunnel 添加公网域名路由

打开刚创建的 Tunnel，在 **Routes → Add route → Published application** 添加路由。
旧界面可能显示为 **Public Hostnames → Add a public hostname**。

| Cloudflare 字段 | 填写示例 | 说明 |
| --- | --- | --- |
| Subdomain / 子域名 | `mcp` | 最终域名为 `mcp.example.com` |
| Domain / 域名 | `example.com` | 选择你自己的域名 |
| Path / 路径 | 留空 | 让整个域名都转发到 Gateway |
| Service Type / 服务类型 | `HTTP` | 这是 Cloudflare 到本机的连接类型 |
| Service URL / 本地服务地址 | `127.0.0.1:29766` | 若界面只有一个完整 URL 输入框，填 `http://127.0.0.1:29766` |

保存路由。公网仍使用 HTTPS，本地服务使用 HTTP。

**本地服务地址不加 `/mcp`。** Gateway 的认证与元数据接口也需要通过同一个域名访问。

如果同名子域名已有 DNS 记录，请使用另一个空闲子域名，避免覆盖正在使用的服务。

## 5. 回到 App，逐项填写

在“连接设置 → 固定链接”中填写：

| App 字段 | 应填写的内容 |
| --- | --- |
| 公网地址 | `https://mcp.example.com`，替换成第 4 步配置的域名 |
| 隧道 Token | 第 3 步复制的完整 Tunnel token |
| 打开 App 时自动连接 | 希望下次打开 App 自动启动连接时勾选；首次也可以先不勾选 |

公网地址只填写 `https://` 加域名，**不加 `/mcp`、其他路径或参数**。
新版 App 也支持直接粘贴末尾带 `/mcp` 的 MCP 地址，保存时会自动转换成公网域名；
其他路径或参数仍会提示修正。

1. 点击“保存”。
2. 返回首页，点击“启动”。
3. 首次启动若需要准备依赖，等待完成。
4. 等待 App 显示“已连接”，并确认 Cloudflare Tunnel 为 `Healthy`。

Tunnel token 会保存在 macOS Keychain。不要把真实 token 写入这份文档或发到聊天中。

## 6. MCP 客户端里填写什么

点击 App 首页的 **Server URL** 复制地址。示例：

```text
https://mcp.example.com/mcp
```

将它粘贴到 MCP 客户端的远程服务器地址栏；如果客户端要求选择传输方式，使用
**Streamable HTTP**。需要授权时，使用 App 首页的“授权口令”完成授权。

“授权口令”用于客户端登录 Gateway，与“隧道 Token”是两个不同的值。
所有工作区共用这个 Gateway 连接，无需为每个工作区单独创建 Tunnel。

## 7. 三个地址对照

| 填写位置 | 示例 | 是否加 `/mcp` |
| --- | --- | --- |
| Cloudflare 的本地服务地址 | `http://127.0.0.1:29766` | 不加 |
| App 的“公网地址” | `https://mcp.example.com` | 不加 |
| MCP 客户端的服务器地址 | `https://mcp.example.com/mcp` | 要加，直接复制 App 的 Server URL |

## 8. 测试与常见问题

完成首次连接后，可以停止并重新启动连接，检查 Server URL 是否保持一致；
再从 MCP 客户端重新连接并执行一次只读工具调用，确认连接和授权都正常。

| 现象 | 检查方法 |
| --- | --- |
| App 的字段不能编辑 | 先返回首页停止连接，再进入连接设置 |
| 公网地址无法保存 | 确认是 `https://域名`，没有 `/mcp`、路径、查询参数或空格 |
| Tunnel 显示未连接 / Inactive | 确认 App 已启动连接、Mac 已联网，token 属于这个 Tunnel；在“更多 → 运行日志”查看错误 |
| Tunnel Healthy，但访问返回 502 | 确认 App 已启动，Cloudflare Service URL 的端口与 App 页面一致，本地协议选 HTTP |
| 路由正确，但提示域名解析未生效 | 新域名可能仍被本机或 VPN 缓存为不存在。新版 App 会通过加密 DNS 复核公网；本机其他客户端仍需等待自己的 DNS 缓存更新 |
| MCP 客户端收到 404 或找不到认证接口 | 检查 Cloudflare 的 Path 是否留空、本地 Service URL 是否误加 `/mcp`，以及客户端地址是否带 `/mcp` |
| 出现额外的 Cloudflare 登录页或 HTML 挑战页 | 检查该域名的 Access/WAF 策略是否支持所用 MCP 客户端；保留 Gateway 自身认证，并为该客户端配置兼容的访问策略 |
| 退出 App、休眠后连不上 | 固定域名保留，但本机服务已离线；恢复联网、唤醒并启动 App |

“打开 App 时自动连接”只控制 App 打开后的行为，并不等于“开机登录时自动打开 App”。
如果还需要登录后自动启动，可在 macOS“系统设置 → 通用 → 登录项与扩展”中添加开发版 App。
应用重启后，旧 MCP 会话可能需要客户端重新连接或重新初始化。

固定链接与恢复行为的技术约定见
[固定公网连接与恢复](project-bound-persistent-gateway-spec.zh-CN.md#固定公网连接与恢复)。
Cloudflare 后台操作参考 [官方 Tunnel 配置指南](https://developers.cloudflare.com/tunnel/get-started/)。
