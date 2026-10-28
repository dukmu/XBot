# Loader ownership

`loader/api` defines plugin trees, overlays and `ScopeLoader`; `create_loader`
is the public constructor. `loader/impl/runtime.py` imports implementation
entries and mounts declared facets. It does not construct an Engine, inspect
conversation metadata, select providers or assemble a session's services.

Every module exposes one or more ordinary functions named `mount_process`,
`mount_session`, `mount_thread`. Each declares `inject` and `provide`; `Config`
may name its Pydantic configuration model. Functions without a facet for the
current scope are skipped. A module with no facets is a configuration error.
Builtins resolve to `XBotv2.<owner>.impl.plugin`; external entries use their
exact module name. A plugin directory discovers `<package>/impl/plugin.py`.
There is no package-root export or old entry fallback.

The process owns one XCore root. A business scope uses `Context.scope` and
isolates the services declared for that scope, including declarations from
disabled facets. A missing private service cannot fall through to a parent
with the same name. Publication is checked at runtime against both the facet
and enclosing scope declarations, including nested components.

A scope runs its own `ready` callbacks after the complete activation graph.
Failures remain attributed to their actual fiber, including nested initialization
events; the loader then applies the same required/optional decision. Agents
initializes its own business state from ready; session management does not call
Agent initialization.

A required import, configuration, activation, unresolved dependency or promised
service failure prevents opening and closes that new owner. Optional failures
roll back only their facet; they still cannot satisfy a required consumer.
`no_plugins` removes entries marked `optional`; it does not contain a list of
business plugin names. Child delegation policy belongs to subagents.

Use the lifecycle acceptance tests under `loader/tests` for shared roots,
concurrent openings, cancellation shielding, isolation, rollback, ownership,
closing retry, worker drain and external facet discovery.
