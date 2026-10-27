# LLM

The LLM plugin owns provider configuration, adapter construction, model binding and provider selection. `LLM` exposes the provider directory and `MODEL` the active model stream via `api/contracts.py`. `api/invoke.py` executes a complete auxiliary request against a `ModelPort`; HTTP models are in `api/protocol.py`.

It depends on core provider and stream contracts and on Agent runtime selection for commands. Provider adapters in `impl/` own vendor request formatting, streaming, errors and SDK connections. The provider directory is scoped to the plugin instance and model binding to the thread; requests and their network resources are transient. `LlmConfig` defaults to provider name `default` and an empty provider mapping. Each configured provider needs a default model listed in its models; its protocol defaults to `openai` and transport inactivity timeout to 60 seconds. Each model defaults to a 32,000-token context, no output limit and text input. The tree is resolved before plugin configuration is applied; selecting a provider or model changes the active thread binding. The application runtime root remains outside this migration.

`/provider [status|list|use <name>]`, `/model [status|list|use [<provider>] <model>]`, and `/effort [<level>]` use the Agent runtime. HTTP exposes `GET /providers` and thread-scoped `PUT .../provider` and `PUT .../effort` routes.

```python
from XBotv2.llm.api.contracts import MODEL
from XBotv2.llm.api.invoke import invoke_llm

response = await invoke_llm(ctx.require(MODEL), request)
```

Here `request` is a `ModelRequest`; the helper returns the completed auxiliary response.
