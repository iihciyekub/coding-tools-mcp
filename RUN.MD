

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

再生成 DMG：

```bash
hdiutil create \
  -volname "Coding Tools MCP" \
  -srcfolder "src-tauri/target/release/bundle/macos/Coding Tools MCP.app" \
  -ov \
  -format UDZO \
  "Coding Tools MCP_0.3.9_aarch64.dmg"
```

给 DMG 签名：

```bash
codesign --force --timestamp \
  --sign "Developer ID Application: Yongjian Li (2NLAH5MYH8)" \
  "Coding Tools MCP_0.3.9_aarch64.dmg"
```

提交 Apple 公证：

```bash
xcrun notarytool submit \
  "Coding Tools MCP_0.3.9_aarch64.dmg" \
  --keychain-profile "WOSAide-Notary" \
  --wait
```

公证通过后：

```bash
xcrun stapler staple "Coding Tools MCP_0.3.9_aarch64.dmg"
xcrun stapler validate "Coding Tools MCP_0.3.9_aarch64.dmg"
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