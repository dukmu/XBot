# `coretools`

Base filesystem, shell, and content tools — always available to the
Agent. Replaces the previous granular wrapper tools with four merged
tools: `read`, `edit`, `path`, `search` plus a `shell` tool.

- **Import/profile:** `coretools`, Agent profile.
- **Source:** `XBotv2/coretools/plugin.py`,
  `XBotv2/coretools/filesystem.py`,
  `XBotv2/coretools/shell.py`,
  `XBotv2/coretools/result_cache.py`.
- **Injects/provides:** `tools`, `session`, `artifacts`, `sandbox`,
  `jobs`, `workspace_root` → (none — registers Tools directly).
- **Subscribes to events:** `after/tools` (tool result cache hook),
  `hook` stages (workspace hooks).

## Public data models

### Filesystem tools (`XBotv2/coretools/filesystem.py:37-160`)

```python
async def read(
    path: str,
    mode: Literal["utf8", "binary", "stat", "media", "list"] = "utf8",
    offset: int = 0,
    limit: int = 2000,
    char_offset: int = 0,
    max_chars: int = 12000,
    line_numbers: bool = False,
    url: str | None = None,
    data: str | None = None,
    media_type: str | None = None,
    recursive: bool = False,
    max_entries: int = 500,
    include_hidden: bool = True,
    *,
    sandbox=None,
    artifacts: ArtifactStorePort | None = None,
) -> ToolResult: ...
```

`mode` variants:

| mode | Behavior |
|---|---|
| `utf8` | Bounded UTF-8 read with line/char limits |
| `binary` | Base64-encoded bytes + metadata |
| `stat` | File metadata (MIME, size, SHA-256, dimensions) |
| `media` | Image content loaded as `ImageContent` for model visibility |
| `list` | Directory listing with bounded metadata |

### `ToolResult` for `read`

```python
# utf8 mode, text file:
ToolResult.success("<markdown content>")

# utf8 mode, non-text file:
ToolResult.success(
    f"Non-text file: {path} ({media_type}, {size_bytes} bytes, "
    f"sha256={sha256}, {width}x{height} {format})")

# binary mode:
ToolResult.success(f"Binary file: {path} ({size_bytes} bytes, sha256={sha256}, base64 in data)")

# stat mode:
ToolResult.success(json.dumps({"media_type": "...", "size_bytes": 1234, "sha256": "..."}))

# media mode:
ToolResult.success(
    f"Image content loaded: {selected} ({len(payload)} bytes)",
    images=(ImageContent(path=ref.id, media_type=ref.media_type, size=ref.size),)
)

# list mode:
ToolResult.success(json.dumps([{"path": "...", "type": "file"|"dir", ...}]))
```

### `edit` tool

```python
async def edit(
    path: str,
    mode: Literal["write", "replace", "patch"] = "write",
    content: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
    replace_all: bool = False,
    patch: str | None = None,
    *,
    sandbox=None,
) -> ToolResult: ...
```

| mode | Required args | Behavior |
|---|---|---|
| `write` | `content` | Atomic file replacement; creates parent dirs |
| `replace` | `old_text`, `new_text` | Replace first occurrence; use `replace_all=True` for all |
| `patch` | `patch` | Unified diff (single-file) |

### `path` tool

```python
async def path(
    operation: Literal["move", "copy", "delete", "mkdir"],
    path: str,
    source: str | None = None,
    destination: str | None = None,
    overwrite: bool = False,
    recursive: bool = False,
    parents: bool = False,
    *,
    sandbox=None,
) -> ToolResult: ...
```

| operation | Required | Optional |
|---|---|---|
| `move` | `path`, `destination` | `overwrite` |
| `copy` | `path`, `destination` | `parents`, `overwrite` |
| `delete` | `path` | `recursive` |
| `mkdir` | `path` | `parents` |

### `search` tool

```python
async def search(
    pattern: str,
    path: str,
    mode: Literal["content", "name"] = "content",
    glob: str | None = None,
    max_results: int = 20,
    case_sensitive: bool = True,
    literal: bool = False,
    include_hidden: bool = False,
    exclude: list[str] | None = None,
    max_line_chars: int = 200,
    kind: Literal["file", "directory", "any"] = "any",
    *,
    sandbox=None,
) -> ToolResult: ...
```

| mode | pattern | glob | kind |
|---|---|---|---|
| `content` | regex or literal | basename/dir glob | files only |
| `name` | glob | — | file / dir / any |

### `shell` tool (`XBotv2/coretools/shell.py`)

```python
async def shell(
    command: str,
    cwd: str | None = None,
    background: bool = False,
    name: str | None = None,
    sandbox_permissions: Literal["use_default", "require_escalated"] = "use_default",
    justification: str | None = None,
    *,
    sandbox=None,
    jobs=None,
    workspace_root: str = "",
) -> ToolResult: ...
```

`sandbox_permissions="require_escalated"` bypasses the sandbox guard
(permissions layer still owns the approval). `background=True` starts
a `SessionShell` job; `name` is optional label.

### `ToolResultCacheHook` (`XBotv2/coretools/result_cache.py`)

```python
def make_tool_result_cache_hook(
    artifacts: ArtifactStorePort,
    cache_threshold_chars: int = 12_000,
    preview_chars: int = 8_000,
    tail_chars: int = 2_000,
) -> Callable[[EventContext], None]:
    """After-tool cache hook. Stores oversized results in artifacts."""
```

## How `apply()` works (`CoreToolsComponent`)

```python
def apply(self, ctx, config):
    artifacts = ctx.artifacts
    result_config = dict(config.get("tool_results") or {})
    cache_threshold_chars = int(result_config.get("cache_threshold_chars", 12_000))
    preview_chars = int(result_config.get("preview_chars", 8_000))
    tail_chars = int(result_config.get("tail_chars", 2_000))
    workspace_xbot = Path(ctx.workspace_root) / ".xbot"
    hooks = [...]
    workspace_tools = [...]
    from XBotv2.coretools.filesystem import filesystem_tools
    from XBotv2.coretools.shell import shell_tools
    tools = (
        *filesystem_tools(ctx.sandbox, artifacts),
        *shell_tools(ctx.sandbox, ctx.jobs, str(ctx.workspace_root)),
    )
    for tool in tools:
        ctx.tools.register(tool)
    ctx.on(Events.AFTER_TOOLS, make_tool_result_cache_hook(...))
    for declaration in hooks:
        ctx.on(declaration.stage, _resolve_hook_target(declaration))
    for declaration in workspace_tools:
        exported = _resolve_workspace_target(declaration, directory="tools")
        for tool in tools:
            ctx.tools.register(tool, namespace="workspace")
```

`filesystem_tools(sandbox, artifacts)` returns `(read, edit, path, search)`.
`shell_tools(sandbox, jobs, workspace_root)` returns `(shell,)`.
Each tool is registered with the standard `Tool.from_function(...)` shape.

## On-disk artifacts

Tool results exceeding `cache_threshold_chars` are stored as
`ArtifactKind.TOOLS_RESULT` in the artifact store. The hook sets
`ToolResult.data` to a reference when the content is too large for
direct display.

## Cross-references

- Depends on: `tools`, `session`, `artifacts`, `sandbox`, `jobs`,
  `workspace_root`, `agentloop` (`AFTER_TOOLS`).
- Depended on by: the Agent (model-facing tools).
- Pairs with: `sandbox` (path capability gates), `permissions`
  (tool allow/deny gates).

## Common pitfalls

- **Using `read(mode=media)` with 3 sources**: exactly one of
  `path`, `url`, `data` is required — raises
  `"invalid_content_source"` otherwise.
- **Passing `url` when `sandbox.network=False`**: raises
  `"network_disabled"`. The sandbox check happens before the HTTP
  request.
- **Max content bytes for media**: `MAX_CONTENT_BYTES = 25 MB`.
  Content exceeding this raises `"content_too_large"`.
- **Supported image types**: only `image/gif`, `image/jpeg`,
  `image/png`, `image/webp` are supported. Other MIME types raise
  `"unsupported_image_type"`.
- **`search(mode="name")` with `pattern="*"`**: the glob must be
  absolute or relative to the `path` argument; `**` is not
  automatically recursive.
- **`edit(mode="replace")` without `old_text` matching**: raises
  `ValueError` if the text is not found or is ambiguous (appears
  more than once without `replace_all=True`).
- **`shell(sandbox_permissions="require_escalated")`**: the guard
  returns `None` (pass), but the permission layer still approves
  or denies — sandbox bypass does not mean permission bypass.
