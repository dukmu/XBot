"""Tests for protocol-adjacent provider and configuration behavior."""

import pytest

from api.paths import RuntimePaths
from config.models import ProviderConfig


class TestProviderConfig:
    """LLM client factory tests."""

    def test_create_llm_deepseek(self):
        """DeepSeek provider config creates OpenAI client."""
        from llm.client import create_llm

        config = ProviderConfig(
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            api_key="test-key",
            temperature=0.7,
            max_output_tokens=8192,
        )
        llm = create_llm(config)
        from llm.openai import OpenAICompatibleProvider

        assert isinstance(llm, OpenAICompatibleProvider)
        assert llm.model_name == "deepseek-chat"

    def test_create_llm_lmstudio(self):
        """LM Studio Anthropic protocol creates Anthropic client."""
        from llm.client import create_llm

        config = ProviderConfig(
            provider="lmstudio",
            model="qwen2.5-coder-7b-instruct",
            base_url="http://localhost:1234/v1",
            api_key="lm-studio",
            temperature=0.7,
            max_output_tokens=4096,
        )
        llm = create_llm(config)
        from llm.anthropic import AnthropicProvider

        assert isinstance(llm, AnthropicProvider)
        assert llm.model == "qwen2.5-coder-7b-instruct"

    def test_create_llm_env_var_expansion(self, monkeypatch):
        """Env vars in config are expanded."""
        from llm.client import create_llm

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
        from llm.client import create_llm
        from llm.mock import MockLLM

        llm = create_llm(ProviderConfig(
            provider="mock",
            mock_responses=[{"content": "mocked"}],
        ))

        assert isinstance(llm, MockLLM)
        assert llm.responses == [{"content": "mocked"}]

    def test_unknown_provider_raises(self):
        """Unknown provider names fail closed instead of silently using OpenAI."""
        from llm.client import create_llm

        with pytest.raises(ValueError, match="Unknown provider"):
            create_llm(ProviderConfig(provider="not-a-provider", model="x"))


class TestProviderConfigLoader:
    """Provider config loading from multi-provider YAML — the original bug."""

    def test_selects_named_provider_section(self, tmp_path, monkeypatch):
        """load_provider_config selects the correct YAML section."""
        from config.loader import load_provider_config

        monkeypatch.setenv("TEST_API_KEY", "sk-test-123")

        # data_dir is the data root; providers.yaml lives at <data_dir>/config/
        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
default: deepseek
providers:
  deepseek:
    provider: deepseek
    model: deepseek-chat
    base_url: https://api.deepseek.com/v1
    api_key: ${TEST_API_KEY}
  openai:
    provider: openai
    model: gpt-4o
    api_key: sk-openai-xxx
""")

        # Load default → should get deepseek
        c = load_provider_config(RuntimePaths.from_data_dir(tmp_path), "default")
        assert c.provider == "deepseek"
        assert c.model == "deepseek-chat"
        assert c.base_url == "https://api.deepseek.com/v1"
        assert c.api_key == "sk-test-123"  # env var expanded

        # Load openai → should get openai section
        c2 = load_provider_config(RuntimePaths.from_data_dir(tmp_path), "openai")
        assert c2.provider == "openai"
        assert c2.model == "gpt-4o"
        assert c2.api_key == "sk-openai-xxx"

    def test_preserves_reasoning_configuration(self, tmp_path):
        from config.loader import load_provider_config

        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
default: minimax
providers:
  minimax:
    provider: anthropic
    model: MiniMax-M3
    api_key: test-key
    max_output_tokens: 8192
    reasoning_effort: high
    thinking_enabled: true
""")

        config = load_provider_config(
            RuntimePaths.from_data_dir(tmp_path),
            "minimax",
        )

        assert config.reasoning_effort == "high"
        assert config.thinking_enabled is True
        assert config.model_mode == "high"

    def test_model_mode_is_empty_without_explicit_provider_setting(self):
        from config.models import ProviderConfig

        assert ProviderConfig().model_mode == ""
        assert ProviderConfig(thinking_enabled=True).model_mode == "thinking"

    def test_provider_names(self, tmp_path):
        from config.loader import load_provider_names

        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
default: default
providers:
  default:
    provider: openai
    model: test
  other:
    provider: anthropic
    model: other
""")

        assert load_provider_names(RuntimePaths.from_data_dir(tmp_path)) == (
            "default",
            ["default", "other"],
        )

    def test_env_var_expansion_in_nested_section(self, tmp_path, monkeypatch):
        """${VAR} patterns are expanded in provider sections."""
        from config.loader import load_provider_config

        monkeypatch.setenv("MY_KEY", "expanded-value")

        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
default: test
providers:
  test:
    provider: openai
    model: gpt-4
    api_key: ${MY_KEY}
""")

        c = load_provider_config(RuntimePaths.from_data_dir(tmp_path), "test")
        assert c.api_key == "expanded-value"

    def test_missing_env_var_is_rejected(self, tmp_path):
        from config.loader import load_provider_config

        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
default: test
providers:
  test:
    provider: openai
    model: gpt-4
    api_key: ${NONEXISTENT_VAR}
""")

        with pytest.raises(ValueError, match="NONEXISTENT_VAR"):
            load_provider_config(RuntimePaths.from_data_dir(tmp_path), "test")

    def test_unknown_provider_is_rejected(self, tmp_path):
        from config.loader import load_provider_config

        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
default: default
providers:
  default:
    provider: openai
    model: fallback-model
""")

        with pytest.raises(
            ValueError,
            match="Unknown provider config: nonexistent_provider.*Available providers: default",
        ):
            load_provider_config(
                RuntimePaths.from_data_dir(tmp_path),
                "nonexistent_provider",
            )

    def test_named_provider_requires_provider_configuration(self, tmp_path):
        from config.loader import load_provider_config

        with pytest.raises(
            ValueError,
            match="Unknown provider config: minimax.*No providers are configured",
        ):
            load_provider_config(RuntimePaths.from_data_dir(tmp_path), "minimax")
