# XBotv2 documentation

XBotv2 is a plugin-composed client/server Agent runtime. This directory keeps
only the short project entry points. The detailed, versioned extension manual
is the bundled [xbot-plugin-development skill](../.agents/skills/xbot-plugin-development/SKILL.md),
including XCore APIs, every built-in plugin, service/event contracts, router
details, testing patterns, and installed-package workflows.

- [Getting started](getting-start.md) — install and run a minimal plugin.
- [Plugins](plugins.md) — tree and ownership map; details live in the skill.
- [Security](security.md) — sandbox and permission boundaries.
- [Persistence](persistence.md) — history, state, and artifacts.
- [HTTP API](http-api.md) — transport index; schemas and route details live in the skill.
- [Clients and runtime behavior](clients.md) — Textual TUI, command discovery,
  streaming, interactions, read-only thread views, and status/usage.
- [Development](development.md) — repository workflow and verification.
- [`project/`](project/) — engineering behavior, backlog, and recorded findings.

The source checkout and the installed package are authoritative. Keep this
directory concise; add detailed API material to the skill rather than creating
a second documentation system.
