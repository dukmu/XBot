"""Tests for protocol-adjacent provider and configuration behavior."""

import yaml
import pytest

from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.config import ModelConfig, ProviderConfig


class TestProviderConfig:
    """LLM client factory tests."""

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
        from XBotv2.llm.client import create_llm

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
        llm = create_llm(config, config.resolve())
        from XBotv2.llm.openai import OpenAICompatibleProvider

        assert isinstance(llm, OpenAICompatibleProvider)
        assert llm.model_name == "deepseek-chat"

    def test_create_llm_anthropic_protocol(self):
        """An anthropic-protocol config creates an Anthropic client."""
        from XBotv2.llm.client import create_llm

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
        llm = create_llm(config, config.resolve())
        from XBotv2.llm.anthropic import AnthropicProvider

        assert isinstance(llm, AnthropicProvider)
        assert llm.model == "claude-x"

    def test_create_llm_env_var_expansion(self, monkeypatch):
        """Env vars in config are expanded."""
        from XBotv2.llm.client import create_llm

        monkeypatch.setenv("TEST_KEY", "expanded-key")

        config = ProviderConfig(
            protocol="openai",
            default_model="gpt-4",
            models=[ModelConfig(model="gpt-4")],
            api_key="${TEST_KEY}",
        )
        llm = create_llm(config, config.resolve())
        assert llm.client.api_key == "expanded-key"

    def test_create_llm_from_mock_provider_config(self):
        """Provider config can select deterministic MockLLM."""
        from XBotv2.llm.client import create_llm
        from XBotv2.llm.mock import MockLLM

        config = ProviderConfig(
            protocol="mock",
            default_model="mock",
            models=[ModelConfig(model="mock", mock_responses=[{"content": "mocked"}])],
        )
        llm = create_llm(config, config.resolve())

        assert isinstance(llm, MockLLM)
        assert llm.responses == [{"content": "mocked"}]

    def test_unknown_protocol_raises(self):
        """Unknown protocol implementations fail closed instead of silently
        falling back to OpenAI."""
        from XBotv2.llm.client import create_llm

        config = self._config(protocol="not-a-protocol")
        with pytest.raises(ValueError, match="Unknown protocol implementation"):
            create_llm(config, config.resolve())

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
            "api_key": "${TEST_API_KEY}",
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
        assert deepseek.api_key == "sk-test-123"  # env var expanded

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
        assert ModelConfig(model="r", thinking="enabled").model_mode == "enabled"

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
        """LlmService.configure stores definitions; names()/default_name() reflect them."""
        from XBotv2.llm.service import LlmService

        service = LlmService()
        service.configure("default", {
            "default": {
                "protocol": "openai",
                "default_model": "test",
                "models": [{"model": "test"}],
            },
            "other": {
                "protocol": "anthropic",
                "default_model": "other",
                "models": [{"model": "other", "max_output_tokens": 8192}],
            },
        })

        assert service.default_name() == "default"
        assert set(service.names()) == {"default", "other"}
        assert service.provider_config("default").resolve().model == "test"

    def test_unknown_provider_is_rejected(self):
        from XBotv2.llm.service import LlmService

        service = LlmService()
        service.configure("default", {
            "default": {
                "protocol": "openai",
                "default_model": "fallback-model",
                "models": [{"model": "fallback-model"}],
            },
        })

        with pytest.raises(
            ValueError,
            match="Unknown provider config: nonexistent_provider.*Configured providers: default",
        ):
            service.provider_config("nonexistent_provider")

    def test_missing_env_var_is_rejected(self):
        from XBotv2.llm.config import parse_provider_config

        with pytest.raises(ValueError, match="NONEXISTENT_VAR"):
            parse_provider_config({
                "protocol": "openai",
                "default_model": "gpt-4",
                "models": [{"model": "gpt-4"}],
                "api_key": "${NONEXISTENT_VAR}",
            })

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
        from XBotv2.config.tree import load_server_tree

        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "plugins.yaml").write_text(
            yaml.safe_dump([
                {"id": "llm", "name": "llm", "config": {
                    "default": "custom",
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

        tree = load_server_tree(
            paths=RuntimePaths.from_data_dir(tmp_path),
            provider_name="default",
            workspace_root=str(tmp_path),
            no_plugins=False,
        )
        llm = next(entry for entry in tree.entries if entry.id == "llm")
        assert llm.config["default"] == "custom"
        assert llm.config["providers"]["custom"]["default_model"] == "custom-model"
        assert llm.config["providers"]["custom"]["models"][0]["model"] == "custom-model"
