"""Public configuration behavior."""

import importlib


def test_llm_catalog_types_are_not_reexported_by_its_config_parser():
    contracts = importlib.import_module("XBotv2.llm.api.contracts")
    parser = importlib.import_module("XBotv2.llm.impl.config")

    for name in ("LlmConfig", "ModelConfig", "ProviderConfig"):
        assert getattr(contracts, name).__module__ == "XBotv2.llm.api.contracts"
        assert not hasattr(parser, name)


def test_llm_config_names_its_default_provider_explicitly():
    from XBotv2.llm.api.contracts import LlmConfig

    assert "default_provider" in LlmConfig.model_fields
    assert "default" not in LlmConfig.model_fields
