# `token_manager`

Observes the current request estimate and provider context measurement.

See the [plugin overview](../../../../../token_manager/README.md) and its
[design and configuration](../../../../../token_manager/doc/design.md) for the
current dependencies, lifecycle, state, defaults and usage.

`api/models.py` defines `EstimatedContext`. Its current diagnostic state stays in the implementation.

Cross-plugin consumers import these `api` modules. The loader entry and
business implementation are under `impl/`; the package root has no exports.
