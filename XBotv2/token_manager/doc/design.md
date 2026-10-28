# Token budget observation

Observes model requests and responses for the latest thread request and exposes diagnostics through the `token_manager` service. It depends on session and model events and has no plugin-specific configuration. Estimates and observations are transient and cleared on unload. It contributes no tools, commands, or prompt text; consumers call `diagnostics()`.
