# Desktop release

Stable desktop releases are produced by GitHub Actions. The canonical workflow
is `.github/workflows/desktop-release.yml`.

## Release a new desktop version

1. Update the desktop version metadata (`package.json`, `Cargo.toml`,
   `tauri.conf.json`, lockfiles, and the UI version constant).
2. Run the desktop checks locally.
3. Commit the release changes.
4. Create and push the matching desktop tag.

Example for Desktop 0.3.31:

```bash
git tag desktop-v0.3.31
git push origin desktop-v0.3.31
```

The cloud workflow then:

- builds the Apple Silicon desktop app;
- imports the Developer ID Application certificate from GitHub Actions Secrets;
- signs, notarizes, staples and validates the app;
- creates, signs, notarizes, staples and validates the DMG;
- builds `Coding-Tools-MCP-<version>-win-x64-portable.zip` on Windows;
- publishes both assets plus `SHA256SUMS.txt` to the GitHub Release;
- updates `iihciyekub/homebrew-tap`;
- verifies the Homebrew Cask can be fetched and installed on macOS arm64.

Required GitHub Actions secrets:

```text
APPLE_CERTIFICATE
APPLE_CERTIFICATE_PASSWORD
APPLE_ID
APPLE_PASSWORD
APPLE_TEAM_ID
HOMEBREW_TAP_DEPLOY_KEY
```

`APPLE_CERTIFICATE` is the base64-encoded exported Developer ID Application
`.p12`. `APPLE_PASSWORD` must be an Apple app-specific password. The Homebrew
deploy key should have write access only to `iihciyekub/homebrew-tap`.

The same existing tag can also be selected from **Actions → Desktop Release →
Run workflow**. Published stable assets are immutable; if a release already
exists for the tag, bump the desktop version and create a new tag instead.

## Local development build

```bash
cd apps/desktop-client
npm ci
npm run check
npm run tauri build -- --bundles app --no-sign --ci
```

The unsigned local app is written to:

```text
apps/desktop-client/src-tauri/target/release/bundle/macos/Coding Tools MCP.app
```

Local manual signing/notarization is a fallback only. Stable public releases
must follow `docs/desktop-release-maintenance.md`.
