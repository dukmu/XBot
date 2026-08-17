"""Tests for protocol-adjacent provider and configuration behavior."""

import yaml
import pytest

from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.config import ProviderConfig


class TestProviderConfig:
    """LLM client factory tests."""

    def test_create_llm_deepseek(self):
        """DeepSeek provider config creates OpenAI client."""
        from XBotv2.llm.client import create_llm

        config = ProviderConfig(
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://XBotv2.core.deepseek.com/v1",
            api_key="test-key",
            temperature=0.7,
            max_output_tokens=8192,
        )
        llm = create_llm(config)
        from XBotv2.llm.openai import OpenAICompatibleProvider

        assert isinstance(llm, OpenAICompatibleProvider)
        assert llm.model_name == "deepseek-chat"

    def test_create_llm_lmstudio(self):
        """LM Studio Anthropic protocol creates Anthropic client."""
        from XBotv2.llm.client import create_llm

        config = ProviderConfig(
            provider="lmstudio",
            model="qwen2.5-coder-7b-instruct",
            base_url="http://localhost:1234/v1",
            api_key="lm-studio",
            temperature=0.7,
            max_output_tokens=4096,
        )
        llm = create_llm(config)
        from XBotv2.llm.anthropic import AnthropicProvider

        assert isinstance(llm, AnthropicProvider)
        assert llm.model == "qwen2.5-coder-7b-instruct"

    def test_create_llm_env_var_expansion(self, monkeypatch):
        """Env vars in config are expanded."""
        from XBotv2.llm.client import create_llm

        monkeypatch.setenv("TEST_KEY", "expanded-key")

        config = ProviderConfig(
            provider="openai",
            model="gpt-4",
            api_key="${TEST_KEY}",
        )
        llm = create_llm(config)
        assert llm.client.api_key == "expanded-key"

    def test_create_llm_from_mock_provider_config(self):
        """Provider config can select deterministic MockLLM."""
        from XBotv2.llm.client import create_llm
        from XBotv2.llm.mock import MockLLM

        llm = create_llm(ProviderConfig(
            provider="mock",
            mock_responses=[{"content": "mocked"}],
        ))

        assert isinstance(llm, MockLLM)
        assert llm.responses == [{"content": "mocked"}]

    def test_unknown_provider_raises(self):
        """Unknown provider names fail closed instead of silently using OpenAI."""
        from XBotv2.llm.client import create_llm

        with pytest.raises(ValueError, match="Unknown provider"):
            create_llm(ProviderConfig(provider="not-a-provider", model="x"))


class TestProviderConfigLoader:
    """Provider config loading from the llm plugin's tree config — the original bug."""

    def test_selects_named_provider_section(self, tmp_path, monkeypatch):
        """parse_provider_config selects the correct section."""
        from XBotv2.llm.config import parse_provider_config

        monkeypatch.setenv("TEST_API_KEY", "sk-test-123")

        deepseek = parse_provider_config({
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://XBotv2.core.deepseek.com/v1",
            "api_key": "${TEST_API_KEY}",
        })
        assert deepseek.provider == "deepseek"
        assert deepseek.model == "deepseek-chat"
        assert deepseek.base_url == "https://XBotv2.core.deepseek.com/v1"
        assert deepseek.api_key == "sk-test-123"  # env var expanded

        openai = parse_provider_config({
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "sk-openai-xxx",
        })
        assert openai.provider == "openai"
        assert openai.model == "gpt-4o"
        assert openai.api_key == "sk-openai-xxx"

    def test_preserves_reasoning_configuration(self):
        from XBotv2.llm.config import parse_provider_config

        config = parse_provider_config({
            "provider": "anthropic",
            "model": "MiniMax-M3",
            "api_key": "test-key",
            "max_output_tokens": 8192,
            "reasoning_effort": "high",
            "thinking_enabled": True,
        })

        assert config.reasoning_effort == "high"
        assert config.thinking_enabled is True
        assert config.model_mode == "high"

    def test_model_mode_is_empty_without_explicit_provider_setting(self):
        from XBotv2.llm.config import ProviderConfig

        assert ProviderConfig().model_mode == ""
        assert ProviderConfig(thinking_enabled=True).model_mode == "thinking"

    def test_llm_service_lists_configured_providers(self):
        """LlmService.configure stores definitions; names()/default_name() reflect them."""
        from XBotv2.llm.service import LlmService

        service = LlmService()
        service.configure("default", {
            "default": {"provider": "openai", "model": "test"},
            "other": {"provider": "anthropic", "model": "other",
                      "max_output_tokens": 8192},
        })

        assert service.default_name() == "default"
        assert set(service.names()) == {"default", "other"}
        assert service.provider_config("default").model == "test"

    def test_unknown_provider_is_rejected(self):
        from XBotv2.llm.service import LlmService

        service = LlmService()
        service.configure("default", {
            "default": {"provider": "openai", "model": "fallback-model"},
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
                "provider": "openai",
                "model": "gpt-4",
                "api_key": "${NONEXISTENT_VAR}",
            })

    def test_api_key_env_resolves_from_environment(self, monkeypatch):
        from XBotv2.llm.config import parse_provider_config

        monkeypatch.setenv("PROVIDER_TEST_KEY", "sk-resolved")
        config = parse_provider_config({
            "provider": "anthropic",
            "model": "m3",
            "api_key_env": "PROVIDER_TEST_KEY",
            "max_output_tokens": 8192,
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
                        "custom": {"provider": "openai", "model": "custom-model"},
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
        assert llm.config["providers"]["custom"]["model"] == "custom-model"
