# Migrating to 0.4

Version 0.4 narrows Coding Tools MCP around software-engineering primitives and
adds model-neutral context/environment capabilities. The frozen 0.3 contract is
[runtime-contract-v0.3.md](runtime-contract-v0.3.md); the active 0.4 contract is
[runtime-contract-v0.4.md](runtime-contract-v0.4.md).

## Breaking changes

- The optional App/Computer Control toolset was removed. The runtime no longer
  accepts `--enable-computer-tools` or `--computer-helper`, and Desktop no longer
  bundles the macOS Computer helper or Application Control settings page.
- Clients must not call the removed `computer_*` or `app_*` session/control tools.
  Use filesystem, shell, Git, LSP, tests and explicit local Web verification for
  coding workflows instead.
- The registered tool inventory is now 71 tools. Workflow discovery remains a
  static startup choice; no dynamic `tools/list` mutation was introduced.

## New capabilities

- `agent_environment` is available in Full Access / host mode and returns
  metadata for installed local agent environments, Skills, Plugins, rules and
  worktrees without returning credential contents.
- `context_checkpoint` provides model-free create/get/list checkpoints with Git,
  running-command and capability fingerprints plus caller-supplied semantic
  summaries. Reads report stale state explicitly.

## Desktop migration

Existing profile JSON containing the old `computer_enabled` field remains
readable because unknown legacy fields are ignored. Saving the profile writes
only the current schema. No Accessibility or Screen Recording permission is
required by Coding Tools MCP 0.4.

## Browser verification

0.4 does not add a persistent browser-control MCP surface. Local Web validation
remains an explicit developer workflow through
`python -m coding_tools_mcp.browser_check` plus existing command/file/image
primitives, using a temporary browser context rather than the user's daily
Chrome profile.

## Release evidence

The SWE-bench Lite smoke uses `SWE-bench/SWE-bench_Lite` and pins
`swebench==5.0.2` so the harness and dataset schema are reproducible together.
