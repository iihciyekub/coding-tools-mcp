# Desktop Release Maintenance Contract

This document is the authoritative maintenance contract for the Coding Tools MCP macOS desktop distribution. It exists so that future maintainers, external contributors, and coding agents can evolve the project without silently changing the release topology, naming, signing, or Homebrew behavior.

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

The local build may use Tauri's internal naming, but the GitHub Release asset consumed by Homebrew MUST use the public name above.

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

For each stable desktop version, maintainers should follow this order:

```text
1. Update desktop version metadata
2. Run desktop checks/builds
3. Build the release app
4. Sign the helper and app
5. Notarize, staple and validate the app
6. Create the final DMG
7. Notarize, staple and validate the DMG
8. Verify version, signatures, Gatekeeper and DMG integrity
9. Publish desktop-v<version> in the maintained fork
10. Upload Coding-Tools-MCP-<version>-arm64.dmg
11. Let the Homebrew Cask workflow update the Tap
12. Verify brew style, info, fetch and an isolated install
```

Publishing the Homebrew update before the final release asset is immutable is not allowed.

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
- [ ] App and DMG notarization/stapling validation pass.
- [ ] The uploaded asset SHA-256 is recorded by GitHub and matches the local final artifact.
- [ ] The Cask points to the maintained fork and the exact release asset.
- [ ] Homebrew style/info/fetch checks pass.
- [ ] An isolated Homebrew install launches the expected desktop version.
- [ ] No signing, notarization, SSH, or GitHub private credentials were committed.
- [ ] Release-related documentation remains consistent with this contract.

If any item fails, the release is incomplete even if the DMG can be downloaded manually.
