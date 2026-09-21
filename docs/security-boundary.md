# Security Boundary

Coding Tools MCP exposes primitives, not an agent workflow engine.

The boundary is:

- in safe/trusted/dangerous modes, direct file tools accept workspace-relative
  paths plus absolute paths contained by explicitly configured file-access roots
- `exec_command` starts in a workspace cwd
- safe/trusted modes filter secrets and loader/startup env
- safe/trusted modes block destructive commands
- safe mode defaults network-intent commands to the `deny` network policy and blocks shell expansion and inline scripts
- an explicit network policy can require approval or allow only literal allowlisted domains independently of the permission mode
- Landlock confines filesystem access when available

`host` mode is an explicit opt-out from the ordinary command boundary. It preserves the
server process's host environment and disables ordinary command gates and Landlock so
`exec_command` can use local SSH/Git credentials and operate outside the
workspace. Host mode also lets ordinary file tools and `apply_patch` resolve
absolute and home-relative paths across the host filesystem. Project-scoped Git,
LSP, checks, reviews, workflow state, and project instructions remain anchored to
the configured workspace. An explicitly selected `deny` or `allowlist` network
policy still applies. Treat a host-mode MCP endpoint as remote code and file
access with the authority of the server user.

Opt-in [computer tools](computer-use-spec.md) require an explicit desktop approval
for a bounded app session, even in host mode. That is a policy for these tools,
not an OS isolation boundary against host shell access. Workspace clients share
the same trust domain. Screenshot/text results are sent to the connected client;
computer tools skip workspace hooks and omit typed text, queries and request
reasons from diagnostic traces.

The boundary is not:

- a complete OS sandbox on every platform
- a package-manager policy engine
- a project build/test/install orchestrator
- an active network egress firewall

For untrusted workspaces or untrusted MCP clients, run the server inside an external container or VM with no host secrets and restricted network egress.
