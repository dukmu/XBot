# `skills`

Discovers `SKILL.md` files for the active Agent thread, registers one no-argument
Tool per model-invocable skill, registers prompt commands for user-invocable
skills, and applies each activated skill's per-turn Tool scope.

- **Import/profile:** `XBotv2.skills.impl.plugin`, Agent profile.
- **Source:** `skills/impl/plugin.py`, `registry.py`, `skill_tool.py`, and `permission_scope.py`.
- **Injects:** `tools`, `commands`, `variables`, `sandbox`, `runtime_paths`.
- **Lifecycle:** discovers skills at `APPLICATION_INITIALIZED`; clears active
  permissions and runtime registrations on turn end or plugin disposal.
- **Prompt input:** a user message beginning with a discovered `/<skill-name>`
  is expanded only when that skill is user-invocable.

The plugin ignores its configuration input; discovery runs on application
initialization. Registry contents and active scope are runtime state, not
persisted conversation state. For example, `/<skill-name> review this`
activates a user-invocable skill and supplies its arguments.

## Discovery and frontmatter

The registry searches upward from the workspace through the workspace's
`.claude/skills`, `.agents/skills`, and `.opencode/skills` directories, stopping
at the Git root. When no `.git` directory is found while walking up, the scan
stops at the workspace itself — `_find_git_root` returns the original `path` it
was given, **not** the filesystem root. It then scans global directories supplied
by the plugin; XBot supplies `<data-dir>/.agents/skills`. Each skill must be a
directory containing `SKILL.md`. The frontmatter `name` must match the directory
name and the lowercase hyphenated name pattern; `description` is required and
truncated to 1,536 characters. Earlier discovered duplicate names take
precedence.

Recognized frontmatter includes:

```yaml
name: skill-name
description: "Short purpose"
allowed-tools: read, search
xbotv2-disallowed-tools: shell
disable-model-invocation: false
user-invocable: true
```

`allowed-tools` and `xbotv2-disallowed-tools` accept a comma-separated string
or a list of strings. `*` and `?` are wildcard characters. A parenthesized
pattern such as `shell(git *)` matches the Tool name plus its `command`
argument. An allow list restricts the turn to matching Tools; a deny pattern
denies matching Tools. These skill scopes are an additional guard and do not
grant permission or widen the sandbox.

## Activation entry points

- A model-invocable skill registers a Tool named after the skill under namespace
  `skills:<scope>`. The Tool accepts no model arguments, loads its `SKILL.md`
  instructions, activates the skill scope, and returns the instructions as
  ordinary Tool output.
- A user-invocable skill registers a `kind="prompt"` command named after the
  skill with usage `/<name> [instructions]`. When a matching human input is
  accepted, the plugin loads the skill and replaces that input with a typed
  `skill_invocation` prompt container containing the instructions and user
  arguments.
- `disable-model-invocation: true` omits the model-facing Tool. A skill that is
  not user-invocable rejects direct user slash invocation. Frontmatter controls
  these entry points; no separate skill catalog is injected into conversation
  history.

Arguments substitute `$ARGUMENTS` and `$0` with the complete argument string;
`$1`, `$2`, and later numbered placeholders use whitespace-split arguments.
Markdown variables are expanded from the active thread's `RuntimeVariables`.
Shell placeholders of the form `` !`command` `` run only when the sandbox is
present and enabled; otherwise the placeholder resolves to an explanatory
unavailable message. This is preprocessing of the skill content, not a separate
Tool execution path.

## Tool scope lifetime

When a skill is activated, its allowed/disallowed patterns are added to the
current turn's `SkillPermissionScope`. The common Tool guard returns a denial for
matching disallowed Tools or for Tools outside an active allow list. Scopes clear
at `turn/end`, so they do not become persistent permission grants.

Skill invocation Tools remain callable while skill scopes are active; the guard recognizes the `skills:` namespace.

## Extension constraints

- Register through the standard tool and command services; the plugin tracks and
  unregisters runtime registrations.
- Skills metadata descriptions are budgeted when preparing the model request;
  this trims tool descriptions, it does not inject the full skill catalog.
- Skill scope is not a substitute for the permissions plugin or sandbox.
- Do not claim the command catalog marks skill entries specially; prompt
  commands are ordinary `kind="prompt"` entries.
