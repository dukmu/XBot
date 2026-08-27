# XBotv2 Documentation

This directory describes the current XBotv2 implementation, organized by
design boundary (core, api, protocol, tools, hooks, plugins, clients) plus
project discipline and verification.

## Overview

- [Architecture](architecture.md) — system overview, component graph, and data flow

## Core Runtime

- [Core](core/core.md) — core models and the agent loop's streaming/input contract
- [Prompt assembly](core/prompts.md) — context builder contract
- [Agents](core/agents.md) — Agent and subagent definitions
- [Configuration](core/workspace_config.md) — global / session / workspace YAML layers
- [Runtime logging](core/logging.md) — levels, structured records, and plugin use

## API and Protocol

- [Python public API](api/public_api.md) — stable extension surface for plugins
- [API inventory](api/api_inventory.md) — maintained `api` symbol list
- [Wire protocol](protocol/protocol.md) — HTTP/SSE session and stream contract
- [SDK contract](protocol/sdk.md) — OpenAPI-described HTTP contract for clients
- [ACP compatibility](protocol/acp_compatibility.md)
- [TUI / opencode requirements](protocol/tui_opencode_requirements.md)

## Tools and Hooks

- [Built-in tools](tools/tools.md)
- [Hooks](hooks/hooks.md)
- [Hook stage matrix](hooks/hook_stage_matrix.md)

## Plugins

- [Plugin system](plugins/plugins.md)
- [Compact plugin](plugins/compact.md)
- [TodoList plugin](plugins/todolist.md)
- [Goal plugin](plugins/goal.md)
- [Token manager plugin](plugins/token_manager.md)
- [Browser plugin](plugins/browser.md)

## Clients

- [Web client](clients/web.md)

## Project and Verification

- [Engineering behavior](project/behavior.md) — change discipline and non-negotiables
- [Architecture iteration backlog](project/iteration_backlog.md)
- [Testing](verification/testing.md)
- [Transport benchmark](verification/transport-bench-v20260605.md)

Files named `plan_stage*.md` at the XBotv2 root are historical implementation
plans. They are not specifications. When a plan conflicts with these documents,
the API inventory, or the typed contracts under `api`, the current
contracts are authoritative.
