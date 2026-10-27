# Plugins and scopes

Each owner keeps public contracts in `api/`, implementation in `impl/`, owner
design in `doc/` and focused verification in `tests/`. Package roots are not
facades. External consumers import the owning `api` contract.

The loader imports only `<owner>.impl.plugin` entries. A module defines ordinary
`mount_process`, `mount_session` and/or `mount_thread` functions with explicit
`inject` and `provide` declarations. Missing facets are skipped. Missing
required services, cycles and configuration/activation failures prevent opening.
An optional entry rolls back only itself; it cannot bypass a required guard.

There is one XCore process root. Session and thread scopes are real owner
fibers; ordinary context extensions do not own independent lifetimes. Declared
private services never fall back to a parent's same-named service, even when a
thread facet is disabled. Closing rejects new work, awaits executing work and
only then revokes services and releases storage/locks.

| Owner | Process | Session | Thread |
|---|---|---|---|
| loader | import/discovery/lifecycle | mount declared facets | mount declared facets |
| config | defaults and carrier config routes | overlay files | resolved settings |
| llm | directories and shared clients by resolved config | — | independent model binding |
| agents | definition infrastructure | — | current definition and explicit selections |
| session | opening/closing/identity/locating | owner identity | identity and event stream |
| persistence | readers/resource definitions | writer lock | durable stores/artifacts |
| jobs | one registry | — | authorized lifetime binding |
| subagents | — | collaboration coordinator | delegation tools and owned children |
| agentloop | HTTP routes | — | Engine, working set, inbox, turn execution |
| server / ACP | listener and transport | — | locate owner capabilities through session API |
| interactions | — | — | user interaction and client event sinks |
| tui | terminal client | — | — |

Provider catalogs share only equal resolved configurations. Workspace/session
overlays remain effective, and mutable model selection is thread-local. Agent
resume resolves the saved Agent reference against current definitions and
reapplies explicit provider/model/effort facts; it does not freeze old prompts.

The configuration order is bundled tree, global overlay, workspace overlay,
session overlay, explicit memory override. `--no-plugins` disables entries
marked `optional`; the loader has no list of business plugin names. External
plugin directory discoveries are optional. Subagents currently limit nested
delegation in their own thread facet.

See [loader](../loader/doc/design.md), [session](../session/doc/design.md),
[persistence](../persistence/doc/design.md) and the runnable
[first-plugin guide](../.agents/skills/xbot-plugin-development/references/first-plugin.md).
