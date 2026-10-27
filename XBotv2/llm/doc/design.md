# LLM

The LLM plugin owns provider configuration, adapter construction, model binding and provider selection. `LLM` exposes the provider directory and `MODEL` the active model stream via `api/contracts.py`. `api/invoke.py` executes a complete auxiliary request against a `ModelPort`; HTTP models are in `api/protocol.py`.

It depends on core provider and stream contracts and on Agent runtime selection for commands. Provider adapters in `impl/` own vendor request formatting, streaming, errors and SDK connections. Provider directories are shared by matching configuration and retained by their owning scopes. Each thread holds its selected client; each active model stream holds a separate use until it finishes. Replacing a selection or closing a thread releases its use, and the last use closes the client. Unused thread-specific directories are released; root shutdown closes the remaining directories after their streams drain. `LlmConfig` defaults to provider name `default` and an empty provider mapping. Each configured provider needs a default model listed in its models; its protocol defaults to `openai` and transport inactivity timeout to 60 seconds. Each model defaults to a 32,000-token context, no output limit and text input. The tree is resolved before plugin configuration is applied; selecting a provider or model changes the active thread binding.

`LLM.acquire(provider_config, model_config)` returns a caller-owned `ProviderUse`; release it with `await use.close()`, or transfer it with `await MODEL.replace(use)`. Replacement consumes the supplied use even when it fails. Once the new binding has been installed, a failure while releasing the old client leaves the new binding in place; Agent selection records that choice before propagating the error or cancellation. `await MODEL.replace_override(provider)` binds an external provider whose cleanup remains the caller's responsibility. Partially consumed streams must be closed; their cleanup completes before the request's use is released.

`/provider [status|list|use <name>]`, `/model [status|list|use [<provider>] <model>]`, and `/effort [<level>]` use the Agent runtime. HTTP exposes `GET /providers` and thread-scoped `PUT .../provider` and `PUT .../effort` routes.

```python
from XBotv2.llm.api.contracts import MODEL
from XBotv2.llm.api.invoke import invoke_llm

response = await invoke_llm(ctx.require(MODEL), request)
```

Here `request` is a `ModelRequest`; the helper returns the completed auxiliary response.
