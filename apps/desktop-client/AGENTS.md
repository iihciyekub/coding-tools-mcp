# Tauri desktop client guide

This subtree owns the desktop application. Keep desktop-only behavior here and keep the core MCP runtime in `coding_tools_mcp/` independent of the GUI.

## Boundaries

- Keep the React UI in `src/` and privileged local operations in `src-tauri/`.
- Do not duplicate MCP tool behavior in the desktop client; invoke and supervise the core runtime instead.
- User-facing remote-access helpers belong under `integrations/`, not under desktop UI modules or root `scripts/`.
- The webview must never receive an arbitrary shell-execution primitive. Expose narrow Tauri commands only.
- Secrets belong in the operating-system keychain, never in `profiles.json`, logs, command lines, or frontend persistence.
- When changing the UI, run `npm run check` and `npm run build`.
- When changing runtime discovery or process management, run `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, and `cargo test`.
