# Progressive tool discovery

Desktop-launched runtimes keep everyday tools directly available and expose
workflow tools through a static discovery gateway. The CLI retains its existing
defaults; select the same behavior with
`--enable-workflow-tools --defer-workflow-tools`.

When `--enable-computer-tools` is selected, the `computer` category and all ten
[computer tools](computer-use-spec.md) are directly exposed. Call their names
directly, including `app_snapshot`, so image content reaches the MCP client.
They do not go through `tool_invoke`. Changing this startup switch requires
restarting the runtime and refreshing the client's tool catalog.

Use the tools already in `tools/list` for routine file reads, searches, patches,
commands, and diffs. Discovery is for an unfamiliar capability, not a mandatory
step before every operation. Tool names, schemas, and permission checks remain
the dispatch contract; categories provide navigation, not authorization.

## Directory → summary → parameters → call

1. **Directory:** call `tool_search({})`. It returns only categories containing
   enabled tools, their direct/deferred counts, selection guidance, and a browse
   action. It does not return every tool schema.
2. **Summaries:** call `tool_search({"category":"checks"})`. This returns a
   bounded list of tools in that category, their intended uses, and schema
   retrieval actions. Follow `next_action` when the list is paginated.
3. **Parameters:** call
   `tool_search({"query":"checks_run","include_schema":true})`. An exact
   enabled tool name returns only that tool. Parameter schemas and invocation
   instructions appear in both model-facing text and structured results, so
   clients that only forward text can still construct the call.
4. **Call:** use the returned `invoke_via`. A direct tool is called by its own
   name. For a deferred tool, call `tool_invoke` with its exact `name` and an
   `arguments` object satisfying the discovered `input_schema`.

For example, after discovering `workspace_overview`, this gateway call has no
required inner arguments:

```json
{"name":"workspace_overview","arguments":{}}
```

For tools such as `checks_run`, first obtain the prerequisites named by their
schema and description (in this case, a discovered check). Do not copy the
empty arguments example to tools that require input.

Known intent can skip the directory:
`tool_search({"query":"运行测试","limit":3})` or
`tool_search({"query":"resume task","limit":3})`. Search uses local English
and Chinese intent aliases, tool names, descriptions, and category keywords;
it does not call an external embedding or language-model service. An optional
`category` narrows the search. A miss returns an action to browse the directory.
Recognized intent phrases suppress weak keyword-only matches instead of filling
the result limit with unrelated parameter schemas.

An intent search includes schemas for deferred matches, preserving the existing
search-to-gateway path. Category browsing omits schemas unless
`include_schema=true`. Reuse definitions already retrieved in the connection;
there is no need to browse every category or refresh `tools/list` after search.
This progressively retrieves descriptions and schemas; it does not dynamically
register tools or unload runtime implementations.

## Selection strategy

| Need | Preferred path |
| --- | --- |
| Read several known files | `read_files` with a shared output budget |
| Find a path | `list_files`; use `list_dir` for directory structure |
| Find text or an error | `search_text`, then bounded reads of relevant files |
| Find a symbol by name | Lightweight `code_*`; use `lsp_*` for semantic identity at a source position |
| Run a known test command | `exec_command`; use `checks_*` when discovery or persisted evidence is needed |
| Continue a running command | Reuse `command_id` with `write_stdin`; after reconnect, use `get_command` or `list_commands` before retrying execution |
| Learn an unfamiliar project | Relevant project overview/map and path-specific instructions; then narrow file reads |
| Resume a durable task | Find its ID with `task_list` if needed, then use `task_context` |
| Stage or commit explicit changes | Read `git_status`, perform the intended guarded operation, and refresh fingerprints after state changes |
| Preserve review evidence | `review_*` stores snapshots/findings; the caller performs the actual analysis |
| Restore selected text files | Choose a checkpoint, preview with `checkpoint_diff`, then use its fresh restore token |

Persistent tasks, plans, reviews, and checkpoints are useful when tracking,
handoff, evidence, or rollback is required. A small edit does not need to create
all of these records. Runtime diagnostics are for relevant preflight or observed
environment problems, not compulsory overhead on every call.

The live directory is the authoritative navigation surface. Its category
membership, English selection hints, and Chinese search aliases are maintained
together in `coding_tools_mcp/tool_catalog.py`; availability and dispatch remain
owned by the runtime registry. The full inventory is documented in
[Tools and schemas](tools-and-schemas.md), and the result/input contract is in
[Runtime contract](runtime-contract-v0.3.md#tool_search).
