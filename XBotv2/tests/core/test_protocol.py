"""Tests for protocol-adjacent provider and configuration behavior."""

from collections.abc import AsyncIterator

import yaml
import pytest

from XBotv2.core.paths import RuntimePaths
from XBotv2.core.domain import SessionScope
from XBotv2.llm.contracts import LlmConfig, ModelConfig, ProviderConfig
from XBotv2.core.history import HistoryPage
from XBotv2.core.timing import SessionStats
from XBotv2.session.contracts import HistoryMutation, SessionEventFrame
from XBotv2.session.protocol import HistoryUpdatedEvent


class _TrackingEventSubscription(AsyncIterator[SessionEventFrame]):
    def __init__(self, *frames: SessionEventFrame) -> None:
        self._frames = iter(frames)
        self.closed = False

    def __aiter__(self) -> "_TrackingEventSubscription":
        return self

    async def __anext__(self) -> SessionEventFrame:
        try:
            return next(self._frames)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class TestProviderConfig:
    """LLM client factory tests."""

    @staticmethod
    def _create(config: ProviderConfig):
        from XBotv2.llm.plugin import build_llm_service

        return build_llm_service().create(config, config.resolve())

    def _config(self, **overrides) -> ProviderConfig:
        base = {
            "protocol": "openai",
            "default_model": "test",
            "models": [ModelConfig(model="test")],
        }
        base.update(overrides)
        return ProviderConfig(**base)

    def test_create_llm_openai_protocol(self):
        """An openai-protocol config creates an OpenAI client."""
        config = ProviderConfig(
            protocol="openai",
            base_url="https://api.example.com/v1",
            api_key="test-key",
            default_model="deepseek-chat",
            models=[
                ModelConfig(
                    model="deepseek-chat",
                    temperature=0.7,
                    max_output_tokens=8192,
                )
            ],
        )
        llm = self._create(config)
        from XBotv2.llm.openai import OpenAICompatibleProvider

        assert isinstance(llm, OpenAICompatibleProvider)

    def test_create_llm_anthropic_protocol(self):
        """An anthropic-protocol config creates an Anthropic client."""
        config = ProviderConfig(
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            api_key="test-key",
            default_model="claude-x",
            models=[
                ModelConfig(
                    model="claude-x",
                    temperature=0.7,
                    max_output_tokens=4096,
                )
            ],
        )
        llm = self._create(config)
        from XBotv2.llm.anthropic import AnthropicProvider

        assert isinstance(llm, AnthropicProvider)

    def test_create_llm_env_var_expansion(self, monkeypatch):
        """Env vars in config are expanded."""
        monkeypatch.setenv("TEST_KEY", "expanded-key")

        config = ProviderConfig(
            protocol="openai",
            default_model="gpt-4",
            models=[ModelConfig(model="gpt-4")],
            api_key="expanded-key",
        )
        llm = self._create(config)
        assert llm.client.api_key == "expanded-key"

    def test_create_llm_from_mock_provider_config(self):
        """Provider config can select deterministic MockLLM."""
        from XBotv2.llm.mock import MockLLM

        config = ProviderConfig(
            protocol="mock",
            default_model="mock",
            models=[ModelConfig(model="mock", mock_responses=[{"content": "mocked"}])],
        )
        llm = self._create(config)

        assert isinstance(llm, MockLLM)
        assert llm.responses == [{"content": "mocked"}]

    def test_unknown_protocol_raises(self):
        """Unknown protocol implementations fail closed instead of silently
        falling back to OpenAI."""
        config = self._config(protocol="not-a-protocol")
        with pytest.raises(ValueError, match="Unknown protocol implementation"):
            self._create(config)

    def test_unknown_model_in_catalog_fails_closed(self):
        config = ProviderConfig(
            protocol="openai",
            default_model="known",
            models=[ModelConfig(model="known")],
        )
        with pytest.raises(ValueError, match="Unknown model 'missing'"):
            config.resolve("missing")

    def test_default_model_must_be_in_catalog(self):
        with pytest.raises(ValueError, match="default_model"):
            ProviderConfig(
                protocol="openai",
                default_model="missing",
                models=[ModelConfig(model="known")],
            )


class TestProviderConfigLoader:
    """Provider config loading from the llm plugin's tree config."""

    def test_selects_named_provider_section(self, tmp_path, monkeypatch):
        """parse_provider_config validates one catalog provider entry."""
        from XBotv2.llm.config import parse_provider_config

        monkeypatch.setenv("TEST_API_KEY", "sk-test-123")

        deepseek = parse_provider_config({
            "protocol": "openai",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-123",
            "default_model": "deepseek-chat",
            "models": [
                {"model": "deepseek-chat", "temperature": 0.7},
                {"model": "deepseek-reasoner"},
            ],
        })
        assert deepseek.protocol == "openai"
        assert deepseek.default_model == "deepseek-chat"
        assert deepseek.resolve().model == "deepseek-chat"
        assert deepseek.resolve("deepseek-reasoner").model == "deepseek-reasoner"
        assert deepseek.base_url == "https://api.example.com/v1"
        assert deepseek.api_key == "sk-test-123"  # boundary-expanded value

        openai = parse_provider_config({
            "protocol": "openai",
            "default_model": "gpt-4o",
            "models": [{"model": "gpt-4o"}],
            "api_key": "sk-openai-xxx",
        })
        assert openai.protocol == "openai"
        assert openai.resolve().model == "gpt-4o"
        assert openai.api_key == "sk-openai-xxx"

    def test_preserves_reasoning_configuration(self):
        from XBotv2.llm.config import parse_provider_config

        config = parse_provider_config({
            "protocol": "anthropic",
            "api_key": "test-key",
            "default_model": "MiniMax-M3",
            "models": [
                {
                    "model": "MiniMax-M3",
                    "max_output_tokens": 8192,
                    "reasoning_effort": "high",
                    "thinking": "enabled",
                }
            ],
        })

        model = config.resolve()
        assert model.reasoning_effort == "high"
        assert model.thinking == "enabled"
        assert model.model_mode == "high"

    def test_model_mode_is_empty_without_explicit_setting(self):
        assert ModelConfig(model="plain").model_mode == ""
        assert ModelConfig(model="r", thinking="enabled").model_mode == ""

    def test_vendor_thinking_mode_is_not_a_reasoning_effort(self):
        model = ModelConfig(model="m3", thinking="adaptive")

        assert model.thinking == "adaptive"
        assert model.model_mode == ""

    def test_effort_tiers_validate_active_reasoning_effort(self):
        config = ModelConfig(
            model="m",
            reasoning_effort="high",
            effort=["low", "medium", "high"],
        )
        assert config.effort == ["low", "medium", "high"]
        with pytest.raises(ValueError, match="must be one of"):
            ModelConfig(
                model="m",
                reasoning_effort="max",
                effort=["low", "medium", "high"],
            )

    def test_llm_service_lists_configured_providers(self):
        """LlmService keeps the validated catalog and exposes its names."""
        from XBotv2.llm.service import LlmService

        service = LlmService(LlmConfig(
            default_provider="default",
            providers={
                "default": ProviderConfig(
                    protocol="openai",
                    default_model="test",
                    models=[ModelConfig(model="test")],
                ),
                "other": ProviderConfig(
                    protocol="anthropic",
                    default_model="other",
                    models=[ModelConfig(model="other", max_output_tokens=8192)],
                ),
            },
        ))

        assert service.default_name() == "default"
        assert set(service.names()) == {"default", "other"}
        assert service.provider_config("default").resolve().model == "test"

    def test_llm_service_owns_the_validated_configuration(self):
        from XBotv2.llm.service import LlmService

        config = LlmConfig(
            default_provider="primary",
            providers={
                "primary": ProviderConfig(
                    protocol="mock",
                    default_model="mock-v1",
                    models=[ModelConfig(model="mock-v1")],
                ),
            },
        )

        service = LlmService(config)

        assert service.default_name() == "primary"
        assert service.names() == ("primary",)
        assert service.provider_config("primary") is config.providers["primary"]

    def test_llm_service_resolves_configured_credential_only_at_selection(
        self, monkeypatch
    ):
        from XBotv2.llm.service import LlmService

        monkeypatch.setenv("PROVIDER_TEST_KEY", "secret-from-environment")
        config = LlmConfig(
            default_provider="custom",
            providers={
                "custom": ProviderConfig(
                    protocol="openai",
                    api_key_env="PROVIDER_TEST_KEY",
                    default_model="model-v1",
                    models=[ModelConfig(model="model-v1")],
                ),
            },
        )

        service = LlmService(config)

        assert service.provider_config("custom").api_key == (
            "secret-from-environment"
        )

    def test_unknown_provider_is_rejected(self):
        from XBotv2.llm.service import LlmService

        service = LlmService(LlmConfig(
            default_provider="default",
            providers={
                "default": ProviderConfig(
                    protocol="openai",
                    default_model="fallback-model",
                    models=[ModelConfig(model="fallback-model")],
                ),
            },
        ))

        with pytest.raises(
            ValueError,
            match="Unknown provider config: nonexistent_provider.*Configured providers: default",
        ):
            service.provider_config("nonexistent_provider")

    def test_missing_env_var_is_rejected(self, tmp_path, monkeypatch):
        """An unset ${env:NAME} fails closed at the config-load boundary."""
        from XBotv2.config.loader import load_plugin_tree
        from XBotv2.core.paths import RuntimePaths

        (tmp_path / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "plugins.yaml").write_text(
            "plugins:\n"
            "- id: llm\n"
            "  name: llm\n"
            "  config:\n"
            "    providers:\n"
            "      custom:\n"
            "        protocol: openai\n"
            "        default_model: gpt-4\n"
            "        api_key: \"${env:NONEXISTENT_VAR}\"\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="NONEXISTENT_VAR"):
            load_plugin_tree(
                RuntimePaths.from_data_dir(tmp_path),
                tmp_path,
                session_id="s1",
            )

    def test_api_key_env_resolves_from_environment(self, monkeypatch):
        from XBotv2.llm.config import parse_provider_config

        monkeypatch.setenv("PROVIDER_TEST_KEY", "sk-resolved")
        config = parse_provider_config({
            "protocol": "anthropic",
            "default_model": "m3",
            "models": [{"model": "m3", "max_output_tokens": 8192}],
            "api_key_env": "PROVIDER_TEST_KEY",
        })
        assert config.api_key == "sk-resolved"

    def test_server_profile_reads_merged_llm_entry(self, tmp_path):
        """The server application consumes the overlaid LLM profile entry."""
        from XBotv2.application.tree import load_server_tree

        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "plugins.yaml").write_text(
            yaml.safe_dump([
                {"id": "llm", "name": "llm", "config": {
                    "default_provider": "custom",
                    "providers": {
                        "custom": {
                            "protocol": "openai",
                            "default_model": "custom-model",
                            "models": [{"model": "custom-model"}],
                        },
                    },
                }},
            ]),
            encoding="utf-8",
        )

        tree = load_server_tree(paths=RuntimePaths.from_data_dir(tmp_path))
        llm = next(entry for entry in tree.entries if entry.id == "llm")
        assert llm.config["default_provider"] == "custom"
        assert llm.config["providers"]["custom"]["default_model"] == "custom-model"
        assert llm.config["providers"]["custom"]["models"][0]["model"] == "custom-model"


def _event_subscription() -> _TrackingEventSubscription:
    return _TrackingEventSubscription(
        SessionEventFrame(
            sequence=1,
            scope=SessionScope(),
            event=HistoryUpdatedEvent(
                operation="undo",
                mutation=HistoryMutation(
                    removed_turns=1,
                    history=HistoryPage(items=(), older_cursor=None),
                    stats=SessionStats(turns=1),
                ),
            ),
        )
    )


@pytest.mark.asyncio
async def test_session_sse_closes_subscription_after_normal_iteration() -> None:
    from XBotv2.session.protocol import _session_sse

    subscription = _event_subscription()
    frames = [
        frame
        async for frame in _session_sse(subscription, "session-1", "agent")
    ]

    assert len(frames) == 1
    assert subscription.closed


@pytest.mark.asyncio
async def test_session_sse_closes_subscription_when_encoding_fails(monkeypatch) -> None:
    from XBotv2.session import protocol

    subscription = _event_subscription()

    def fail_encoding(*_args, **_kwargs):
        raise ValueError("invalid event")

    monkeypatch.setattr(protocol, "_format_sse", fail_encoding)
    with pytest.raises(ValueError, match="invalid event"):
        await anext(protocol._session_sse(subscription, "session-1", "agent"))

    assert subscription.closed


@pytest.mark.asyncio
async def test_session_sse_closes_subscription_when_generator_is_closed() -> None:
    from XBotv2.session.protocol import _session_sse

    subscription = _event_subscription()
    stream = _session_sse(subscription, "session-1", "agent")
    await anext(stream)
    await stream.aclose()

    assert subscription.closed
