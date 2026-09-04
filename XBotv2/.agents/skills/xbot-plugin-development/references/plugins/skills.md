# `skills`

Agent-facing skill management — discovers `SKILL.md` files from workspace
and global `.agents/skills/` directories, registers skill Tools, exposes
slash commands, and enforces per-turn tool permission scopes.

Compatible with the [agentskills.io](https://agentskills.io) standard.

- **Import/profile:** `skills`, Agent profile.
- **Source:** `XBotv2/skills/plugin.py`,
  `XBotv2/skills/registry.py`,
  `XBotv2/skills/skill_tool.py`,
  `XBotv2/skills/permission_scope.py`.
- **Injects/provides:** `tools`, `commands`, `sandbox`,
  `runtime_paths` → (none directly; registers Tools and Commands).
- **Subscribes to events:** `application/initialized`,
  `before/user-message-accept`, `before/tool-schema-bind`,
  `turn/end`.
- **Tool guard:** `_guard_tool_scope` (per-turn tool allow/deny).

## Public data models

### `Skill` (`XBotv2/skills/registry.py:21-35`)

```python
@dataclass
class Skill:
    name: str
    description: str
    path: Path
    content: str
    frontmatter: dict[str, object] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    disable_model_invocation: bool = False
    user_invocable: bool = True
    scope: str = "project"
```

`name` must match the directory name and regex `^[a-z0-9]+(-[a-z0-9]+)*$`.
`description` is capped at 1536 chars. `scope` is `"project"` or `"global"`.

### `SkillRegistry` (`XBotv2/skills/registry.py:37-120`)

```python
class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def discover(
        self,
        workspace: Path,
        *,
        global_dirs: list[Path] | tuple[Path, ...] = (),
    ) -> None:
        self._skills.clear()
        self._scan_project(workspace)
        self._scan_global(global_dirs)

    def list_skills(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.name)

    def load_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)
```

Discovery walks parent directories from the workspace root to the `.git`
root (or `/`), searching `.claude/skills`, `.agents/skills`,
`.opencode/skills` in each directory.

Frontmatter parsing (`SKILL.md`):

```yaml
---
name: skill-name
description: "One-line description"
allowed-tools: shell, read
xbotv2-disallowed-tools: edit, path
disable-model-invocation: false
user-invocable: true
---

Skill content here.
```

### `SkillPermissionScope` (`XBotv2/skills/permission_scope.py:7-45`)

```python
class SkillPermissionScope:
    def __init__(self) -> None:
        self._allowed: list[re.Pattern[str]] = []
        self._disallowed: list[re.Pattern[str]] = []

    def add(
        self,
        allowed: list[str] | None = None,
        disallowed: list[str] | None = None,
    ) -> None:
        """Compile and store regex patterns for allow/deny checks."""

    def check(self, tool_name: str, args: dict[str, Any] | None = None) -> str | None:
        """Return 'deny', 'allow', or None (no opinion).

        Checks against tool name and 'tool(command)' form.
        Deny patterns take precedence over allow patterns.
        """

    def clear(self) -> None:
        """Reset all patterns."""

def _wildcard_to_regex(pattern: str) -> str: ...
def _compile_pattern(pattern: str) -> re.Pattern[str]: ...
def validate_tool_patterns(patterns: list[str]) -> None: ...
```

Pattern syntax: `"shell"`, `"shell(git *)"`, `"mcp__*"`, `"*tool*"`.
`*` becomes `.*`, `?` becomes `.`. Parenthesized forms match
`tool_name(command)` where `command` is extracted from `args["command"]`.

### `load_skill` (`XBotv2/skills/skill_tool.py:14-25`)

```python
async def load_skill(
    name: str,
    *,
    arguments: str = "",
    skill_registry: Any = None,
    sandbox: Any = None,
) -> str: ...
```

Supports `$ARGUMENTS`, `$0`, `$1`, `$2`... substitution and shell
injection via `` !`command` ``:

```python
_SHELL_INJECT_RE = re.compile(r"!`([^`]+)`")
```

## `SkillsPlugin` (`XBotv2/skills/plugin.py:34-190`)

```python
class SkillsPlugin:
    inject = ["tools", "commands", "sandbox", "runtime_paths"]
    name = "skills"

    def __init__(self) -> None:
        self._registry = SkillRegistry()
        self._permission_scope = SkillPermissionScope()
        self._active_skills: set[str] = set()
        self._skill_tools: list[str] = []
        self._skill_commands: list[str] = []
        self._model_skill_names: set[str] = set()
        self._metadata_budget_chars = 8_000
        self._initialized = False

    def apply(self, ctx, config=None) -> None:
        self._tools = ctx.tools
        self._commands = ctx.commands
        self._sandbox = ctx.sandbox
        self._runtime_paths = ctx.runtime_paths
        ctx.dispose(self._cleanup_runtime)
        ctx.on(APPLICATION_INITIALIZED, self._on_session_init)
        ctx.on(Events.BEFORE_USER_MESSAGE_ACCEPT, self._on_before_user_message)
        ctx.on(Events.BEFORE_TOOL_SCHEMA_BIND, self._on_before_tool_schema)
        ctx.on(Events.TURN_END, self._on_turn_end)
        ctx.tools.guard(self._guard_tool_scope)

    async def _on_session_init(self, event: ApplicationInitialized) -> None:
        """Discover skills and register tools/commands."""

    async def _on_before_user_message(self, ctx: EventContext) -> dict | None:
        """Expand /skill-name [instructions] into prompt_container."""

    async def _on_before_tool_schema(self, ctx: EventContext) -> None:
        """Budget-aware tool description trimming for skill Tools."""

    async def _on_turn_end(self, ctx: EventContext) -> None:
        """Clear active skills and permission scope."""

    async def _guard_tool_scope(self, tool_call: Any, _entry: Any) -> Any:
        """Deny tools not allowed by active skills."""

    async def _cleanup_runtime(self) -> None:
        """Unregister session skill tools/commands, reset state."""

    def diagnostics(self) -> dict[str, Any]: ...
```

## Tool registration

Each non-disabled skill registers a Tool via `_skill_as_tool()`:

```python
def _skill_as_tool(self, skill: Skill) -> Tool:
    handler = SkillToolHandler(self, skill)
    return Tool(
        name=skill.name,
        description=f"Load Skill instructions for this turn. {skill.description}",
        function=handler.invoke,
        parameters={"type": "object", "properties": {}},
    )
```

Registered with namespace `skills:{skill.scope}`.

## Slash command registration

Each user-invocable skill registers a `kind="prompt"` command:

```python
Command(
    name=skill.name,
    kind="prompt",
    description=skill.description,
    usage=f"/{skill.name} [instructions]",
)
```

When the model outputs `/skill-name instructions`, `_on_before_user_message`
intercepts it, loads the SKILL.md content, expands arguments, and
returns a `prompt_container` that replaces the raw user input.

## Turn lifecycle

```
APPLICATION_INITIALIZED → discover + register tools/commands
BEFORE_USER_MESSAGE → intercept /skill-name
BEFORE_TOOL_SCHEMA_BIND → budget-aware description trimming
BEFORE_TOOL_CALL → permission scope check
TURN_END → clear active skills + permission scope
```

## On-disk artifacts

None directly. Skills are discovered from:

```text
<workspace>/.claude/skills/<name>/SKILL.md
<workspace>/.agents/skills/<name>/SKILL.md
<workspace>/.opencode/skills/<name>/SKILL.md
<data_dir>/.agents/skills/<name>/SKILL.md
```

## Cross-references

- Depends on: `tools`, `commands`, `sandbox`, `runtime_paths`,
  `agentloop` (subscribes to `APPLICATION_INITIALIZED`,
  `BEFORE_USER_MESSAGE_ACCEPT`, `BEFORE_TOOL_SCHEMA_BIND`,
  `TURN_END`).
- Depended on by: the Agent (skill Tools and slash commands).
- Pairs with: `sandbox` (shell injection uses sandbox),
  `permissions` (tool scope guard).

## Common pitfalls

- **`disable-model-invocation=true`**: the skill Tool is NOT
  registered, but it may still be invoked via slash command if
  `user_invocable=true`.
- **`user_invocable=false`**: the slash command is NOT registered,
  but the Tool IS registered (the model can call it directly).
- **Tool scope is per-turn**: `_on_turn_end` clears all active
  skills and permission scopes. A skill's tool allow/deny only
  applies during the turn it was activated.
- **Skill name must match directory name**: `_parse()` validates
  `name == path.parent.name`. A frontmatter name that differs from
  the directory name causes the skill to be silently skipped.
- **`allowed-tools` and `xbotv2-disallowed-tools` validation**:
  invalid patterns raise `ValueError` in `_scan_dir()` and cause
  the entire skill to be skipped (not just the pattern).
- **Shell injection `` !`cmd` `` requires sandbox**: if
  `sandbox is None` or `not sandbox.enabled`, the command
  produces `[shell injection unavailable: enabled sandbox required]`.
