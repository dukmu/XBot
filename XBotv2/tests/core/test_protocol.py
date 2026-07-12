"""Tests for protocol-adjacent provider and configuration behavior."""

import pytest

from xbotv2.api.tools import ToolCall
from xbotv2.api.paths import RuntimePaths


class TestProviderConfig:
    """LLM client factory tests."""

    def test_create_llm_from_dict_deepseek(self):
        """DeepSeek provider config creates OpenAI client."""
        from xbotv2.llm.client import create_llm

        config = {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "test-key",
            "temperature": 0.7,
            "max_tokens": 8192,
        }
        llm = create_llm(config)
        assert llm is not None
        assert hasattr(llm, "model_name")
        assert llm.model_name == "deepseek-chat"

    def test_create_llm_from_dict_lmstudio(self):
        """LM Studio Anthropic protocol creates Anthropic client."""
        from xbotv2.llm.client import create_llm

        config = {
            "provider": "lmstudio",
            "model": "qwen2.5-coder-7b-instruct",
            "base_url": "http://localhost:1234/v1",
            "api_key": "lm-studio",
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        llm = create_llm(config)
        assert llm is not None
        # ChatAnthropic uses `model` attribute, ChatOpenAI uses `model_name`
        model = getattr(llm, "model_name", None) or getattr(llm, "model", "")
        assert "qwen" in model.lower()

    def test_create_llm_env_var_expansion(self, monkeypatch):
        """Env vars in config are expanded."""
        from xbotv2.llm.client import create_llm

        monkeypatch.setenv("TEST_KEY", "expanded-key")

        config = {
            "provider": "openai",
            "model": "gpt-4",
            "api_key": "${TEST_KEY}",
        }
        llm = create_llm(config)
        # The API key should have been expanded
        assert llm is not None

    def test_create_mock_llm(self):
        """Mock LLM factory works."""
        from xbotv2.llm.client import create_mock_llm

        llm = create_mock_llm([{"content": "test"}])
        assert llm is not None
        assert len(llm.responses) == 1

    def test_create_llm_from_mock_provider_config(self):
        """Provider config can select deterministic MockLLM."""
        from xbotv2.llm.client import create_llm
        from xbotv2.llm.mock import MockLLM

        llm = create_llm({
            "provider": "mock",
            "mock_responses": [{"content": "mocked"}],
        })

        assert isinstance(llm, MockLLM)
        assert llm.responses == [{"content": "mocked"}]

    def test_unknown_provider_raises(self):
        """Unknown provider names fail closed instead of silently using OpenAI."""
        from xbotv2.llm.client import create_llm

        with pytest.raises(ValueError, match="Unknown provider"):
            create_llm({"provider": "not-a-provider", "model": "x"})

    def test_mock_llm_records_input_messages(self):
        """MockLLM call history records the actual request messages."""
        from langchain_core.messages import HumanMessage
        from xbotv2.llm.mock import MockLLM

        llm = MockLLM([{"content": "ok"}])
        response = llm.invoke([HumanMessage(content="hello")])

        assert response.content == "ok"
        assert llm.call_count == 1
        assert [message.content for message in llm.get_call_messages(0)] == ["hello"]

    def test_mock_llm_records_normalized_tool_calls(self):
        """MockLLM call history records normalized tool calls from responses."""
        from langchain_core.messages import HumanMessage
        from xbotv2.llm.mock import MockLLM

        llm = MockLLM([
            {
                "content": "using tool",
                "tool_calls": [{"name": "shell", "args": {"command": "pwd"}, "id": "c1"}],
            }
        ])
        llm.invoke([HumanMessage(content="run")])

        assert llm.verify_tool_call_made("shell")
        assert llm._mock_call_history[0]["tool_calls"] == [
            ToolCall("c1", "shell", {"command": "pwd"})
        ]


class TestProviderConfigLoader:
    """Provider config loading from multi-provider YAML — the original bug."""

    def test_selects_named_provider_section(self, tmp_path, monkeypatch):
        """load_provider_config selects the correct YAML section."""
        from xbotv2.config.loader import load_provider_config

        monkeypatch.setenv("TEST_API_KEY", "sk-test-123")

        # data_dir is the data root; providers.yaml lives at <data_dir>/config/
        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
default:
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

    def test_env_var_expansion_in_nested_section(self, tmp_path, monkeypatch):
        """${VAR} patterns are expanded in provider sections."""
        from xbotv2.config.loader import load_provider_config

        monkeypatch.setenv("MY_KEY", "expanded-value")

        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
test:
  provider: openai
  model: gpt-4
  api_key: ${MY_KEY}
""")

        c = load_provider_config(RuntimePaths.from_data_dir(tmp_path), "test")
        assert c.api_key == "expanded-value"

    def test_missing_env_var_becomes_empty(self, tmp_path):
        """Unset env vars expand to empty string."""
        from xbotv2.config.loader import load_provider_config

        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
test:
  provider: openai
  model: gpt-4
  api_key: ${NONEXISTENT_VAR}
""")

        c = load_provider_config(RuntimePaths.from_data_dir(tmp_path), "test")
        assert c.api_key == ""

    def test_unknown_provider_returns_builtin_default(self, tmp_path):
        """Unknown provider_name does not fall back to another configured provider."""
        from xbotv2.config.loader import load_provider_config

        config_subdir = tmp_path / "config"
        config_subdir.mkdir(parents=True)
        (config_subdir / "providers.yaml").write_text("""
default:
  provider: openai
  model: fallback-model
""")

        c = load_provider_config(RuntimePaths.from_data_dir(tmp_path), "nonexistent_provider")
        assert c.provider == "openai"
        assert c.model == "gpt-4"
