# `todolist`

Owns the thread task list, task tools and read-only HTTP projection.

See the [plugin overview](../../../../../todolist/README.md) and its
[design and configuration](../../../../../todolist/doc/design.md) for the
current dependencies, lifecycle, state, defaults and usage.

`api/models.py` defines task state; `api/events.py` defines `TaskChanged`; `api/service.py` declares `GET_TODOS`.

Cross-plugin consumers import these `api` modules. The loader entry and
business implementation are under `impl/`; the package root has no exports.
