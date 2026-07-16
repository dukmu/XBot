# Workspace Startup Configuration

XBot reads workspace configuration once while bootstrapping a thread. It does
not watch these files or reread them between turns. Start a new runtime to apply
changes.

## AGENTS.md

The built-in `workspace_instructions` plugin reads `<workspace>/AGENTS.md` once
and registers it as a source-tagged `system_instructions` prompt fragment. It
is not added to message history or the mailbox. Disable the behavior through
`.xbot/plugins.yaml` when a workspace does not want project instructions.
Agent definitions are separate files under `.agents/<name>.md`; `AGENTS.md`
frontmatter is not interpreted as runtime configuration.

## .xbot/policy.yaml

```yaml
permissions:
  allow:
    - tool: filesystem_read
sandbox:
  network: false
```

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
