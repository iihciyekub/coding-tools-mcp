# Security Boundary

Coding Tools MCP exposes primitives, not an agent workflow engine.

The boundary is:

- direct file tools accept workspace-relative paths plus absolute paths contained
  by explicitly configured file-access roots
- `exec_command` starts in a workspace cwd
- safe/trusted modes filter secrets and loader/startup env
- safe/trusted modes block destructive commands
- safe mode defaults network-intent commands to the `deny` network policy and blocks shell expansion and inline scripts
- an explicit network policy can require approval or allow only literal allowlisted domains independently of the permission mode
- Landlock confines filesystem access when available

`host` mode is an explicit opt-out from the ordinary command boundary. It preserves the
server process's host environment and disables ordinary command gates and Landlock so
`exec_command` can use local SSH/Git credentials and operate outside the
workspace. An explicitly selected `deny` or `allowlist` network policy still
applies. Direct file tools remain limited to the workspace plus explicitly
configured file-access roots. Treat a host-mode MCP endpoint as remote command
execution with the authority of the server user.

The boundary is not:

- a complete OS sandbox on every platform
- a package-manager policy engine
- a project build/test/install orchestrator
- an active network egress firewall

For untrusted workspaces or untrusted MCP clients, run the server inside an external container or VM with no host secrets and restricted network egress.
