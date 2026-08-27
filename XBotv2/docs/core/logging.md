# Runtime logging

XBot configures one rotating UTF-8 application log under
`<data-dir>/logs/xbotv2.log`. `XBOT_LOG_FILE` overrides that path. The handler
captures both `xbotv2.*` and `xcore.*`; XCore lifecycle records are therefore
part of the same ordered application trace instead of disappearing into an
unconfigured root logger.

## Levels

- `INFO` records application/plugin completion states, API requests and
  responses, tool registration and execution outcomes, context composition,
  model request/response statistics, Agent/provider selection, and durable
  configuration changes.
- `DEBUG` records plugin dependencies and service availability, event
  dispatch and handler duration, listener registration, tool selection, and
  other composition details useful during development.
- `WARNING` records rejected or timed-out operations and invalid lifecycle
  calls.
- `ERROR` records failed handlers, plugins, providers, tools, and application
  cleanup.

The CLI log-level setting controls both XBot and XCore namespaces. Individual
categories can override it through `XBOT_LOG_LEVELS`, using comma-separated
`logger=LEVEL` entries such as
`xbotv2.llm=DEBUG,xcore.events=WARNING,xbotv2.persistence=DEBUG`. Only
`xbotv2.*` and `xcore.*` categories are accepted; invalid category names or
levels fail during startup instead of being ignored. A program embedding XBot
can pass the same mapping as `category_levels` to `setup_logging`.

Framework
access loggers remain suppressed because the XBot API records already include
method, path, status, full response-stream duration, and `x-request-id`
correlation. The pure-ASGI logger forwards streaming bodies directly and does
not add response buffering.

## Structured summaries

Records use a stable event name followed by searchable `key=value` fields.
Examples include `plugin.state`, `event.dispatch`, `context.built`,
`llm.request.ready`, `llm.response`, and `tool.execute.finish`.

The runtime carries `http_request_id`, domain `request_id`, `session_id`, and
`thread_id` through Python async task context. `http_request_id` identifies the
HTTP exchange and matches the `x-request-id` response header; `request_id`
identifies the accepted message/turn and may span work beyond that exchange.
This correlation applies to both structured XBot records and ordinary
XCore/provider records; callers do not need to repeat fields at every layer.
Bound fields take precedence and are emitted only once.

The main operational categories are `api`, `session`, `acp`, `agentloop`,
`context`, `tools`, `llm`, `usage`, `persistence`, `config`, and `application`.
XCore uses `xcore.context`, `xcore.plugin`, `xcore.service`, and `xcore.events`.

Runtime logs never include raw prompts, message content, tool argument values,
tool results, request bodies, or credentials. They describe content through
counts, character lengths, field names, source names, statuses, and token
usage. Fields whose names identify credentials are redacted by the core log
service. Empty optional fields are omitted. Exception records retain file,
line, function, and exception type for debugging, but omit the exception
message and source-code line because either may contain request content.

## Plugin use

`runtime_log` is a root XCore service available before the plugin tree mounts.
A plugin that writes domain records declares the dependency and binds its own
category and stable context:

```python
class ExamplePlugin:
    name = "example"
    inject = ["runtime_log"]

    def apply(self, ctx, config=None):
        log = ctx.runtime_log.bind("example", plugin=self.name)
        log.info("example.ready", items=3)
```

Plugins should rely on XCore's own lifecycle, dependency, service, and event
records rather than duplicating them. Domain logs belong at ownership
boundaries: after a state change is committed, immediately before an external
request, and when that request or operation finishes.
