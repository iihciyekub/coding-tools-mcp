

```bash
cd /Users/IIDEV/Documents/ii-research/coding-tools-mcp/apps/desktop-client

npm install

npm run tauri build -- --bundles app
```

如果构建成功，App 通常会在：

```bash
src-tauri/target/release/bundle/macos/Coding\ Tools\ MCP.app
```

然后用你的开发者签名：

```bash
codesign --force --deep --options runtime --timestamp \
  --sign "Developer ID Application: Yongjian Li (2NLAH5MYH8)" \
  "src-tauri/target/release/bundle/macos/Coding Tools MCP.app"
```

验证签名：

```bash
codesign --verify --deep --strict --verbose=2 \
  "src-tauri/target/release/bundle/macos/Coding Tools MCP.app"
```

先对 App 本体进行 Apple 公证并装订票据：

```bash
ditto -c -k --keepParent \
  "src-tauri/target/release/bundle/macos/Coding Tools MCP.app" \
  "src-tauri/target/release/bundle/macos/Coding Tools MCP_0.3.21.app.zip"

xcrun notarytool submit \
  "src-tauri/target/release/bundle/macos/Coding Tools MCP_0.3.21.app.zip" \
  --keychain-profile "WOSAide-Notary" \
  --wait

xcrun stapler staple "src-tauri/target/release/bundle/macos/Coding Tools MCP.app"
xcrun stapler validate "src-tauri/target/release/bundle/macos/Coding Tools MCP.app"
```

再生成带标准拖拽安装界面的 DMG：

```bash
cd apps/desktop-client
bash ./create-dmg.sh \
  "src-tauri/target/release/bundle/macos/Coding Tools MCP.app" \
  "src-tauri/target/release/bundle/dmg/Coding-Tools-MCP-0.3.21-arm64.dmg"
```

该脚本会生成标准 macOS Finder 安装窗口：左侧为较大的 Coding Tools MCP 图标，右侧为 Applications 快捷方式，用户可直接拖拽安装。

给 DMG 签名：

```bash
codesign --force --timestamp \
  --sign "Developer ID Application: Yongjian Li (2NLAH5MYH8)" \
  "src-tauri/target/release/bundle/dmg/Coding-Tools-MCP-0.3.21-arm64.dmg"
```

提交 Apple 公证：

```bash
xcrun notarytool submit \
  "src-tauri/target/release/bundle/dmg/Coding-Tools-MCP-0.3.21-arm64.dmg" \
  --keychain-profile "WOSAide-Notary" \
  --wait
```

公证通过后：

```bash
xcrun stapler staple "src-tauri/target/release/bundle/dmg/Coding-Tools-MCP-0.3.21-arm64.dmg"
xcrun stapler validate "src-tauri/target/release/bundle/dmg/Coding-Tools-MCP-0.3.21-arm64.dmg"
```

最关键的修正就两个：

```text
npm ci      ❌
npm install ✅
```

以及：

```text
→   不要复制进 Terminal
```

你现在先从 `npm install` 开始即可。