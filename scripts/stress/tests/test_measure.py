"""Measurement boundaries and restoration without starting a server."""

import pytest
from openai import AsyncOpenAI, _base_client
from openai._models import FinalRequestOptions

from scripts.stress.measure import WorkloadMeasurements
from XBotv2.agents.impl import catalog, loader
from XBotv2.core.api import tools
from XBotv2.core.api.provider import ToolSchema
from XBotv2.llm.impl import openai


def request(client, path, text):
    options = FinalRequestOptions.construct(
        method="post", url=path, json_data={
            "model": "test", "stream": True,
            "messages": [{"role": "system", "content": text}],
        },
    )
    return client._build_request(options)


def test_measurement_counts_real_sdk_encoding_and_restores(tmp_path):
    original = (
        catalog.AgentCatalog.__init__, loader._parse_source, tools._parameters_schema,
        ToolSchema.__init__, openai.openai_tool_schema,
        _base_client.BaseClient._build_request, _base_client.openapi_dumps,
    )
    client = AsyncOpenAI(api_key="test")
    source = tmp_path / "agent.md"
    source.write_text('---\ndescription: Test\n---\nPrompt', encoding='utf-8')
    with pytest.raises(RuntimeError, match="controlled"):
        with WorkloadMeasurements(single_session=True) as measured:
            catalog.AgentCatalog(ownership="caller")
            loader._parse_source(source)
            def tool(value: str):
                return value
            definition = tools.Tool.from_function(tool)
            schema = ToolSchema(name=definition.name, description="", parameters=definition.parameters)
            openai.openai_tool_schema(schema)
            request(client, "/other", "ignored")
            for content in ("one", "one", "two"):
                response = request(client, "/chat/completions",
                    f"<xbot_context>\n<core_instructions>\n{content}\n</core_instructions>\n</xbot_context>")
                assert response.content
            report = measured.report()
            assert report["counts"] == {
                "agent_catalogs": 1, "agent_source_parses": 1,
                "parameter_schema_builds": 1, "tool_schema_projections": 1,
                "provider_tool_schema_mappings": 1, "model_json_encodes": 3,
            }
            assert report["model_json_cpu_ns"] > 0
            assert report["prefix"]["samples"] == 3
            assert report["prefix"]["changes"] == 1
            raise RuntimeError("controlled")
    assert original == (
        catalog.AgentCatalog.__init__, loader._parse_source, tools._parameters_schema,
        ToolSchema.__init__, openai.openai_tool_schema,
        _base_client.BaseClient._build_request, _base_client.openapi_dumps,
    )
    assert measured._prefix is None


def test_multi_session_prefix_is_disabled():
    client = AsyncOpenAI(api_key="test")
    with WorkloadMeasurements(single_session=False) as measured:
        request(client, "/chat/completions", "<core_instructions>\none\n</core_instructions>")
        report = measured.report()
    assert report["counts"]["model_json_encodes"] == 1
    assert report["prefix"]["enabled"] is False
    assert report["prefix"]["samples"] == 0
    assert report["prefix"]["changes"] is None
