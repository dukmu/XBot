"""Package roots expose only their declared, owner-level contracts."""

import importlib


def test_public_package_exports_are_resolvable_and_unique():
    packages = (
        "XBotv2.core",
        "XBotv2.agentloop",
        "XBotv2.application",
        "XBotv2.jobs",
        "XBotv2.permissions",
        "XBotv2.protocol",
        "XBotv2.session",
        "XBotv2.usage",
    )
    for package_name in packages:
        package = importlib.import_module(package_name)
        assert len(package.__all__) == len(set(package.__all__)), package_name
        assert all(hasattr(package, name) for name in package.__all__), package_name


def test_core_public_surface_uses_domain_messages_and_tool_outcomes():
    import XBotv2.core as core

    assert hasattr(core, "ConversationMessage")
    assert hasattr(core, "HumanInputMessage")
    assert hasattr(core, "ToolOutcome")
    assert not hasattr(core, "ClientEvent")
    assert not hasattr(core, "Message")


def test_model_request_is_owned_only_by_the_core_provider_contract():
    import XBotv2.agentloop as agentloop
    import XBotv2.agentloop.contracts as agentloop_contracts
    from XBotv2.core.provider import ModelRequest

    assert ModelRequest.__module__ == "XBotv2.core.provider"
    assert not hasattr(agentloop, "ModelRequest")
    assert not hasattr(agentloop_contracts, "ModelRequest")


def test_all_tools_selection_is_owned_by_the_agentloop_registry_contract():
    import XBotv2.agentloop as agentloop
    import XBotv2.core as core

    assert agentloop.AllTools.__module__ == "XBotv2.agentloop.contracts"
    assert not hasattr(core, "AllTools")


def test_llm_catalog_types_are_not_reexported_by_its_config_parser():
    contracts = importlib.import_module("XBotv2.llm.contracts")
    parser = importlib.import_module("XBotv2.llm.config")

    for name in ("LlmConfig", "ModelConfig", "ProviderConfig"):
        assert getattr(contracts, name).__module__ == "XBotv2.llm.contracts"
        assert not hasattr(parser, name)


def test_llm_config_names_its_default_provider_explicitly():
    from XBotv2.llm.contracts import LlmConfig

    assert "default_provider" in LlmConfig.model_fields
    assert "default" not in LlmConfig.model_fields


def test_sdk_error_normalization_is_owned_by_the_llm_boundary():
    import XBotv2.core as core
    from XBotv2.llm.provider_errors import provider_context_overflow

    assert callable(provider_context_overflow)
    assert not hasattr(core, "provider_context_overflow")
