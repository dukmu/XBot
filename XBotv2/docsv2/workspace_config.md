# Workspace Startup Configuration

XBot reads `.xbot/*.yaml` configuration once while bootstrapping a thread. It
does not watch those files or reread them between turns. Start a new runtime to
apply configuration changes. `AGENTS.md` is the exception described below.

## Runtime Variables

Each thread receives one immutable runtime-variable mapping. Paths are absolute
and cannot be changed by plugins or configuration after bootstrap.

| Variable | Value |
|---|---|
| `${workspace}` | Active workspace root |
| `${data_dir}` | Runtime data root |
| `${config_dir}` | Built-in configuration directory under `data_dir` |
| `${custom_config_dir}` | Workspace `.xbot` directory |
| `${session_dir}` | Shared session directory |
| `${thread_dir}` | Current thread directory |
| `${state_dir}` | Current thread state directory |
| `${plugin_states}` | Current thread plugin-state directory |
| `${artifacts}` | Current thread artifact directory |
| `${tool_results}` | Cached Tool-result directory |

Permission `paths` expressions and sandbox resource paths reject unknown
variables. Markdown prompt sources expand a variable only when it is the sole
content of an explicit `var` fenced block:

````markdown
```var
${workspace}
```
````

The fence is replaced by the variable value. References outside `var` blocks
remain literal Markdown, including known variables and shell expressions such
as `${HOME}`. An unknown or malformed explicit `var` block fails loading.

## AGENTS.md

The built-in `workspace_instructions` plugin reads `<workspace>/AGENTS.md`
before every model context build. Edits and deletion therefore apply to the
next model request, including the next Tool loop within the current turn. The
content is a temporary source-tagged `system_instructions` component; it is not
added to message history or the mailbox. Disable the behavior through
`.xbot/plugins.yaml` when a workspace does not want project instructions.
Agent definitions are separate files under `.agents/<name>.md`; `AGENTS.md`
frontmatter is not interpreted as runtime configuration.

## .xbot/policy.yaml

```yaml
permissions:
  allow:
    - tool: filesystem_(?:write|edit|patch|move|copy|delete|mkdir)
      paths: ${workspace}
sandbox:
  network: false
```

`paths: ${workspace}` is a special permission variable, not a regular
expression. It matches a filesystem Tool call only when all of its declared
path arguments,
including `source` and `destination`, resolve inside the active workspace.
Other `paths` values are regular expressions matched against each resolved
absolute path. The workspace variable can be embedded in a regex, for example
`paths: '${workspace}/generated/.*'`.

Workspace rules overlay the global permission and sandbox baseline. Mutable
session approvals remain in `data/sessions/<session-id>/policy.yaml` and take
precedence at runtime.

## .xbot/plugins.yaml

```yaml
paths:
  - extensions
plugins:
  workspace_instructions:
    enabled: true
  sample:
    config:
      strict: true
```

Paths are relative to the workspace and may not escape it. A workspace entry
replaces the global config for the same plugin. `enabled: false` prevents a
discovered plugin from loading.

## .xbot/hooks.yaml

```yaml
hooks:
  - stage: before_tool_call
    target: hooks/check_tool.py:check_tool
```

When the file exists, its hook list replaces the global hook list. A script path
is relative to `.xbot` and cannot escape that directory. Normal
`package.module:handler` targets remain available for installed modules.
Targets are trusted Python code imported once at startup. Invalid stages,
imports, paths, or attributes fail bootstrap rather than silently omitting
policy behavior. Standalone hooks are Core startup configuration; they are not
plugins and do not participate in plugin discovery.
