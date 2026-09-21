# Permission Modes

`exec_command` has four permission modes.

These modes do not grant application-control sessions. Opt-in
[computer tools](computer-use-spec.md) use their own desktop approval and retain
truthful annotations in every permission mode.

## safe

Default mode. Commands run with:

- workspace read/write
- system toolchain and DNS resolver paths read-only
- `HOME`, `TMPDIR`, and `cache_dir` under an external server-owned runtime directory
- network-intent commands use the default `deny` network policy
- shell expansion and inline interpreter snippets blocked
- secret-looking and loader/startup env filtered
- Landlock enabled when available

Start explicitly:

```bash
coding-tools-mcp --permission-mode safe --workspace /path/to/repo
```

## trusted

Local development mode. It allows dependency downloads, shell expansion, and inline interpreter snippets while keeping secret filtering and destructive-command checks.

`HOME`, `TMPDIR`, and `cache_dir` use the same external runtime directory layout as safe mode. Only that exact runtime directory is added as an extra writable Landlock root.

```bash
coding-tools-mcp --permission-mode trusted --workspace /path/to/repo
```

## dangerous

Dangerous mode disables the ordinary `exec_command` permission gates and Landlock. An explicitly selected `deny` or `allowlist` network policy still applies. Use it only inside an isolated container or VM.

```bash
coding-tools-mcp --permission-mode dangerous --workspace /path/to/repo
```

Dangerous mode still uses the isolated command `HOME`, `TMPDIR`, and cache
directories. It is intended for a server that already runs inside a disposable
container or VM.

## host

Host mode is the explicit full-host development mode. It disables ordinary command
permission gates and Landlock like dangerous mode, defaults environment
inheritance to `all`, and does not replace `HOME`, `TMPDIR`, or ecosystem cache
locations. Commands can therefore use the server process's SSH agent, SSH/Git
configuration, credential helpers, language version managers, and files outside
the configured workspace:

```bash
coding-tools-mcp --permission-mode host --workspace /path/to/repo
```

Direct file tools (`read_file`, `apply_patch`, search, listing, and image reads)
also use host filesystem access in this mode. Relative paths remain
workspace-relative for deterministic project work, while absolute paths and
home-relative paths may resolve across the host filesystem. Git, LSP, workflow
state, checks, reviews, and project context remain anchored to the workspace.
Unrestricted host command access, including SSH/SCP/rsync, installed developer
tools, and Git over SSH, is available through `exec_command`. The server's own
transport authentication secrets are always removed from child command
environments.

As with dangerous mode, an explicitly configured `deny` or `allowlist` network
policy still applies even though the other command gates are disabled.

Host mode gives an authenticated MCP client the same command authority as the
user running the server. Use it only with trusted repositories and clients.
Prefer stdio or loopback. If it is exposed through an HTTPS tunnel, keep OAuth
or bearer authentication enabled, protect the authorization credential, and
understand that transport authentication does not make model-generated commands
safe.

Compatibility aliases:

- `--allow-network`: compatibility alias for `--network-policy unrestricted`.
- `--dangerously-skip-all-permissions`: alias for `--permission-mode dangerous`.

## Network Policy

Network command gating is independent from the four permission modes and can be
selected explicitly with `--network-policy`:

- `deny`: network-intent commands require explicit permission. This is the
  default in `safe` mode.
- `allowlist`: commands whose literal target domains are all allowlisted run
  without a network prompt. Unknown domains and network-intent commands whose
  target cannot be determined statically require permission.
- `unrestricted`: disables the network command gate. This remains the default
  for permission modes that historically allowed networking (`trusted`,
  `dangerous`, and `host`) unless an explicit network policy overrides it.

Allowlist entries are exact host names or `*.example.com` patterns:

```bash
coding-tools-mcp --permission-mode safe \
  --network-policy allowlist \
  --network-allow-domain github.com \
  --network-allow-domain '*.npmjs.org' \
  --workspace /path/to/repo
```

`CODING_TOOLS_MCP_NETWORK_POLICY` selects the mode and
`CODING_TOOLS_MCP_NETWORK_ALLOW_DOMAINS` accepts a comma-separated domain list.
This layer recognizes common network commands and package-manager operations and
extracts literal URL/SSH/SCP-style hosts when possible. It is deliberately
conservative: for example, `npm install package` has an unresolved destination
because registry configuration may live outside the command line, so allowlist
mode asks for permission rather than assuming the public npm registry.

This is **command-policy enforcement, not an OS-level egress firewall**. A
future sandbox/network-namespace layer is required to guarantee that an already
running process cannot reach arbitrary destinations.

## Client-Side Annotation Gates

Permission modes govern this server's own gates. They cannot affect a client that
gates on MCP annotations — one that refuses to call, or prompts on every call to, a
tool advertised as mutating. That friction lives entirely in the client, so
`--permission-mode dangerous` or `host` does nothing about it.

`--dangerously-fake-readonly-annotations` addresses that one case. It makes
`tools/list` report every tool with `readOnlyHint: true`, `destructiveHint: false`,
and `openWorldHint: false`:

```bash
coding-tools-mcp --permission-mode dangerous \
  --dangerously-fake-readonly-annotations --workspace /path/to/repo
```

The annotations are false. `apply_patch` still rewrites files and `exec_command`
still runs commands; only the advertised hints change. Because the claim is false,
it is fenced in:

- It requires `--permission-mode dangerous` or `host`, so it can only be set
  alongside an explicit unrestricted-execution choice.
- Over HTTP it requires bearer auth or OAuth. A tunnel forwards to a loopback bind,
  so the bind address cannot distinguish a private sandbox from a publicly reachable
  one; authentication can. Use stdio for an unauthenticated local sandbox.
- `server_info.annotation_override` and the server card's
  `tools.annotationOverride` report `fake_readonly`, and both keep listing the real
  per-tool annotations. `check_exec_environment` adds a warning. The lie is confined
  to `tools/list`, so ground truth is always one call away.

`CODING_TOOLS_MCP_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS=1` is equivalent. This is
not a tool profile: the catalog is unchanged and every tool remains callable.

## Runtime Directory

Safe and trusted modes keep command runtime state outside the Git worktree:

```text
/tmp/coding-tools-mcp/<workspace-hash>/<instance-id>/
  home/
  tmp/
  cache/
```

On Windows, the parent is the platform temp directory instead of `/tmp`. The server creates these directories lazily when `exec_command` first needs an environment. `server_info` and `check_exec_environment` report `runtime_dir`, `home`, `tmpdir`, and `cache_dir`.

The server does not create workspace-local `.coding-tools/` directories by default. Runtime directories are per server instance; after stopping the server, operators may remove an instance directory or the whole external runtime tree. Normal OS temp cleanup may also remove stale directories.

Set `CODING_TOOLS_MCP_RUNTIME_ROOT` to choose an explicit external runtime parent. The server reports `RUNTIME_DIR_UNWRITABLE` instead of falling back into the workspace for runtime state.
