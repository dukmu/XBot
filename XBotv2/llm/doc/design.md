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

OpenAI-compatible models can set `output_token_parameter` to `max_tokens` (the default) or `max_completion_tokens`. The selected generation output budget is sent under that field; this is independent of the model name. Use `max_completion_tokens` for the configured OpenAI GPT-5.6 models and `max_tokens` for DeepSeek. Configure `max_context_tokens`, `max_output_tokens`, `input_modalities`, and supported `effort` values for the actual endpoint and model. For LM Studio these depend on the locally loaded model and its server settings.

The example profile reads `MINIMAX_API_TOKEN` and `DEEPSEEK_API_KEY` from the environment. Set the OpenAI and Anthropic `api_key` fields before selecting those providers; environment references such as `$${env:OPENAI_API_KEY}` and `$${env:ANTHROPIC_API_KEY}` are expanded at startup and must be set if configured. MiniMax uses the China endpoint; international accounts should use the endpoint documented for their platform. The MiniMax M3 example enables adaptive thinking; its 32,768 output budget is a configured request limit, not a claim about the model maximum. The OpenAI examples omit temperature so reasoning requests do not depend on sampling parameter support. DeepSeek Chat accepts `none` to disable thinking through `reasoning_effort`, according to its Chat Completions reference.

The example model metadata was checked on 2026-10-28 against the official [OpenAI GPT-5.6 model pages](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [OpenAI Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create), [DeepSeek model catalog](https://api-docs.deepseek.com/api/list-models/) and [Chat Completions reference](https://api-docs.deepseek.com/api/create-chat-completion/), [MiniMax Anthropic API](https://platform.minimax.cn/docs/api-reference/text-anthropic-api), and [Claude Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview). Provider availability and limits can change.
