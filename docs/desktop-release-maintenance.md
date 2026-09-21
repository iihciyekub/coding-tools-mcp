# Desktop Release Maintenance Contract

This document is the authoritative maintenance contract for the Coding Tools MCP desktop distribution. It exists so that future maintainers, external contributors, and coding agents can evolve the project without silently changing the release topology, naming, signing, notarization, Windows portable packaging, or Homebrew behavior.

Changes to the desktop release pipeline MUST preserve this contract unless a pull request explicitly proposes and documents a contract change.

## 1. Canonical repository topology

The maintained distribution uses three distinct roles:

- `iihciyekub/coding-tools-mcp` is the maintained fork and canonical source for desktop releases.
- `xyTom/coding-tools-mcp` is the upstream source repository and is tracked as `upstream` for synchronization only.
- `iihciyekub/homebrew-tap` is the Homebrew Tap and contains the public Cask definition.

For maintainer clones, the expected Git remotes are:

```text
origin   -> git@github.com:iihciyekub/coding-tools-mcp.git
upstream -> git@github.com:xyTom/coding-tools-mcp.git
```

Do not publish maintained desktop releases, release assets, or Homebrew updates to the upstream repository. Upstream changes should be fetched and reviewed before being merged or rebased into maintained branches.

If repository ownership is intentionally transferred in the future, update this document, the desktop README, the Homebrew workflow, and the Cask in one coordinated change.

## 2. Keep Python and desktop release series separate

The Python package and the desktop application are related but independently versioned products.

- Python/core releases use `v<version>` tags and the existing Python/npm release workflow.
- Desktop releases use `desktop-v<version>` tags.
- A desktop version MUST match `apps/desktop-client/package.json` and `apps/desktop-client/src-tauri/tauri.conf.json`.
- `releasePlatforms` in `apps/desktop-client/package.json` selects the platforms for that version: `["macos"]` or `["macos", "windows"]`. Omitting it preserves the historical two-platform release. A macOS-only release skips Windows entirely; it does not replace the most recent Windows download.
- A desktop release MUST NOT be forced through the Python package version validator merely to reuse a tag.

Example:

```text
Python/core:  v0.3.8
Desktop:      desktop-v0.3.20
```

Do not collapse these two release series unless the project deliberately adopts a single-version policy and updates all release tooling at the same time.

## 3. Desktop artifact naming is a public contract

Apple Silicon desktop releases use this exact public asset name:

```text
Coding-Tools-MCP-<version>-arm64.dmg
```

Windows x64 portable releases use:

```text
Coding-Tools-MCP-<version>-win-x64-portable.zip
```

The local build may use Tauri's internal naming, but the GitHub Release assets MUST use the public names above. Homebrew consumes the arm64 DMG.

`apps/desktop-client/create-dmg.sh` MUST derive the version from the desktop configuration instead of hard-coding a release number.

If Intel or universal builds are added later, introduce a documented naming rule and corresponding Cask architecture handling rather than silently replacing the arm64 asset.

## 4. Signing and notarization are release requirements

A public macOS DMG MUST NOT be published as a stable desktop release unless all of the following are true:

1. The bundled App Helper is signed with the intended stable Developer ID identity.
2. The containing `Coding Tools MCP.app` is signed with Hardened Runtime and a secure timestamp.
3. The app is accepted by Apple notarization and the ticket is stapled and validated.
4. The final DMG is accepted by Apple notarization and the ticket is stapled and validated.
5. The mounted app passes `codesign --verify --deep --strict`.
6. Gatekeeper assessment reports a notarized Developer ID source.
7. The final DMG passes integrity verification.

Developer certificates, notarization credentials, private keys, and other Apple secrets MUST NOT be committed to the repository, release assets, logs, or the Homebrew Tap.

Local signing is acceptable. CI signing is also acceptable if credentials are stored only in protected repository/environment secrets and the resulting artifact satisfies the same checks.

## 5. GitHub Release contract

A desktop release is canonical only when it exists in `iihciyekub/coding-tools-mcp` with:

- tag `desktop-v<version>`;
- target commit from the maintained fork;
- signed, notarized and stapled DMG asset named `Coding-Tools-MCP-<version>-arm64.dmg`;
- Windows x64 portable asset named `Coding-Tools-MCP-<version>-win-x64-portable.zip` when `releasePlatforms` includes Windows;
- `SHA256SUMS.txt` covering every public desktop asset selected for that version;
- a stable SHA-256 digest for that exact uploaded asset.

Do not replace an already published stable DMG with different bytes under the same tag/version. If the artifact must change, publish a new desktop version.

This immutability rule matters because Homebrew Casks pin the SHA-256 of the release asset.

## 6. Homebrew contract

The public install command is:

```bash
brew install --cask iihciyekub/tap/coding-tools-mcp
```

The Cask lives at:

```text
iihciyekub/homebrew-tap/Casks/coding-tools-mcp.rb
```

The Cask MUST:

- reference the maintained fork, not the upstream repository;
- use the exact desktop release version and SHA-256;
- install `Coding Tools MCP.app`;
- declare the supported architecture explicitly;
- remain valid under the currently supported Homebrew Cask DSL;
- avoid deprecated DSL merely to preserve historical syntax.

`.github/workflows/homebrew-cask.yml` is the authoritative automation for updating the Tap after a desktop release. Its write credential MUST remain scoped to `iihciyekub/homebrew-tap` only. A repository-wide personal access token should not be introduced when a narrower deploy key is sufficient.

## 7. Release sequence

For each stable desktop version, `.github/workflows/desktop-release.yml` is the canonical automation. Pushing `desktop-v<version>` (or manually dispatching that existing tag) performs this sequence:

```text
1. Update desktop version metadata
2. Push desktop-v<version>
3. Validate tag/version consistency
4. Build the Apple Silicon release app
5. Import the Developer ID certificate from Actions secrets
6. Sign, notarize, staple and validate the app
7. Create, sign, notarize, staple and validate the final DMG
8. Build and verify the Windows x64 portable ZIP if selected by releasePlatforms
9. Publish the immutable GitHub Release plus SHA256SUMS.txt
10. Invoke the Homebrew Cask workflow directly
11. Verify brew style, info, fetch and an isolated install
```

Publishing the Homebrew update before the final release asset is immutable is not allowed.

### Canonical maintainer / coding-agent command

When the maintainer says **publish/release the Desktop app** and the matching
`desktop-v<version>` tag already exists on `origin`, the coding agent SHOULD
execute the release rather than merely describing how to do it. Use the default
maintained branch as the workflow source and the immutable tag as the release
source:

```bash
VERSION=0.3.35
gh workflow run desktop-release.yml \
  -R iihciyekub/coding-tools-mcp \
  --ref iiaide \
  -f tag="desktop-v${VERSION}" \
  -f mode=release
```

Then resolve and watch the run to completion:

```bash
RUN_ID="$(gh run list \
  -R iihciyekub/coding-tools-mcp \
  --workflow desktop-release.yml \
  --event workflow_dispatch \
  --limit 1 \
  --json databaseId \
  --jq '.[0].databaseId')"

gh run watch "$RUN_ID" \
  -R iihciyekub/coding-tools-mcp \
  --exit-status
```

For a **new** Desktop version, first synchronize all Desktop version metadata,
commit the release source, create the matching `desktop-v<version>` tag, and
push that tag. The tag push automatically starts the same `mode=release` path,
so do not start a duplicate manual dispatch unless the first run needs to be
retried:

```bash
git tag "desktop-v${VERSION}"
git push origin "desktop-v${VERSION}"
```

Before dispatching an existing tag, confirm it exists on `origin` and resolves
to the intended release commit. Never move, force-update, or recreate a
published release tag.

After the workflow succeeds, verify the public result rather than treating a
green intermediate job as sufficient:

```bash
gh release view "desktop-v${VERSION}" \
  -R iihciyekub/coding-tools-mcp \
  --json tagName,name,assets,url

brew info --cask iihciyekub/tap/coding-tools-mcp
```

The completed run must contain the macOS signed/notarized DMG, the immutable
GitHub Release, and a successful Homebrew Cask update/verification. If any of
those stages is skipped or fails, the Desktop release is incomplete.

Do not use local ad-hoc signing as a substitute for this path when the
`production-release` GitHub Environment is available. Do not retrieve, print,
or copy secret values into logs or chat; checking secret **names** and presence
is sufficient for release diagnostics.

The canonical CI path uses a protected `production-release` GitHub Environment,
the same credential model used by WOS Aide: an exported Developer ID Application
`.p12` plus an App Store Connect Team API Key `.p8`. When Apple credentials are
available only in the maintainer's local keychain, local build/sign/notarization
may still supply the same immutable release artifacts. After verified local
assets are published, the `release.published` trigger or a manual dispatch of
`homebrew-cask.yml` updates and verifies the Tap.

The platform selector is an additive release-contract change: tag names, macOS asset URLs and Homebrew behavior remain unchanged. For macOS-only versions, Windows users keep using the last version that includes a portable ZIP. Explicit `windows-artifact` dispatches remain available for maintainers independently of the stable release platform selector.

The `production-release` Environment requires these secrets. Their values MUST
never be committed:

```text
MAC_CSC_P12_BASE64
MAC_CSC_KEY_PASSWORD
APPLE_API_KEY_P8_BASE64
APPLE_API_KEY_ID
APPLE_API_ISSUER
```

`MAC_CSC_P12_BASE64` is a base64-encoded Developer ID Application `.p12`
including its private key. `APPLE_API_KEY_P8_BASE64`, `APPLE_API_KEY_ID`, and
`APPLE_API_ISSUER` identify a Team API Key accepted by `notarytool`. The
environment variable `MAC_CSC_NAME` names the expected signing identity and
defaults to `Yongjian Li (2NLAH5MYH8)`.

Use `scripts/setup-desktop-release-secrets.sh` to create/update the Environment
and upload the five Apple values without writing credential material to Git.
The repository-level `HOMEBREW_TAP_DEPLOY_KEY` remains separate and MUST stay
scoped to `iihciyekub/homebrew-tap` only.

Stable release jobs MUST fail if any Apple signing/notarization secret is
missing. They MUST NOT convert a missing-credential condition into a green job
that silently skips app/DMG production.

## 8. Required validation after release-pipeline changes

Any pull request that changes DMG creation, release naming, signing/notarization documentation, GitHub release automation, or Homebrew automation MUST validate the affected path before merge.

At minimum, when applicable:

```bash
brew style iihciyekub/tap/coding-tools-mcp
brew info --cask iihciyekub/tap/coding-tools-mcp
brew fetch --cask iihciyekub/tap/coding-tools-mcp
```

For an actual release, also perform an isolated install test so an existing manually installed `/Applications/Coding Tools MCP.app` does not hide packaging problems.

The test installation must not overwrite or delete a maintainer's existing production installation.

## 9. Contribution rules for release infrastructure

Release-infrastructure changes should be narrow and reviewable.

- Do not rename release tags, assets, the app bundle, or the Cask without treating it as a compatibility change.
- Do not move release ownership back to `upstream` as a convenience.
- Do not mix unrelated product changes into signing/Homebrew credential changes.
- Do not commit generated private credentials or local keychain exports.
- Do not grant broader GitHub write permissions than the workflow requires.
- Do not force-push or rewrite published release tags.
- Prefer one authoritative rule here and link to it from other documentation.

Pull requests that intentionally change this contract must state the migration impact for existing users, existing Homebrew installs, existing release URLs, and downstream automation.

## 10. Maintainer acceptance checklist

Before considering a desktop release complete, confirm:

- [ ] `origin` is the maintained fork and `upstream` is the source repository.
- [ ] Desktop version metadata agrees across the desktop package and Tauri config.
- [ ] The release uses `desktop-v<version>`.
- [ ] The uploaded asset is `Coding-Tools-MCP-<version>-arm64.dmg`.
- [ ] The Windows portable asset is `Coding-Tools-MCP-<version>-win-x64-portable.zip` when Windows is selected; otherwise no Windows build or asset is required.
- [ ] `SHA256SUMS.txt` covers every selected desktop asset.
- [ ] App and DMG notarization/stapling validation pass.
- [ ] The uploaded asset SHA-256 is recorded by GitHub and matches the local final artifact.
- [ ] The Cask points to the maintained fork and the exact release asset.
- [ ] Homebrew style/info/fetch checks pass.
- [ ] An isolated Homebrew install launches the expected desktop version.
- [ ] No signing, notarization, SSH, or GitHub private credentials were committed.
- [ ] Release-related documentation remains consistent with this contract.

If any item fails, the release is incomplete even if the DMG can be downloaded manually.
