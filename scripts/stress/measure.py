"""Temporary measurements for the real OpenAI-compatible product workload."""

from contextlib import ExitStack
from contextvars import ContextVar
from functools import wraps
from time import thread_time_ns


class WorkloadMeasurements:
    def __init__(self, *, single_session: bool):
        self.single_session = single_session
        self.counts = {
            "agent_catalogs": 0,
            "agent_source_parses": 0,
            "parameter_schema_builds": 0,
            "tool_schema_projections": 0,
            "provider_tool_schema_mappings": 0,
            "model_json_encodes": 0,
        }
        self.model_json_cpu_ns = 0
        self.prefix_samples = 0
        self.prefix_changes = 0
        self._prefix = None
        self._model_request = ContextVar("stress_model_request", default=False)
        self._stack = ExitStack()

    def _wrap(self, owner, name, wrapper):
        original = getattr(owner, name)
        setattr(owner, name, wrapper(original))
        self._stack.callback(setattr, owner, name, original)

    def _count(self, key):
        def wrap(original):
            @wraps(original)
            def measured(*args, **kwargs):
                self.counts[key] += 1
                return original(*args, **kwargs)
            return measured
        return wrap

    def __enter__(self):
        from openai import _base_client
        from XBotv2.agents.impl import catalog, loader
        from XBotv2.core.api import tools
        from XBotv2.core.api.provider import ToolSchema
        from XBotv2.llm.impl import openai

        try:
            self._wrap(catalog.AgentCatalog, "__init__", self._count("agent_catalogs"))
            self._wrap(loader, "_parse_source", self._count("agent_source_parses"))
            self._wrap(tools, "_parameters_schema", self._count("parameter_schema_builds"))
            self._wrap(ToolSchema, "__init__", self._count("tool_schema_projections"))
            self._wrap(openai, "openai_tool_schema", self._count("provider_tool_schema_mappings"))
            self._wrap(_base_client.BaseClient, "_build_request", self._wrap_request)
            self._wrap(_base_client, "openapi_dumps", self._wrap_encode)
        except BaseException:
            self._stack.close()
            raise
        return self

    def _wrap_request(self, original):
        @wraps(original)
        def measured(client, options, *args, **kwargs):
            model_request = options.method.lower() == "post" and options.url.rstrip("/").endswith("/chat/completions")
            token = self._model_request.set(model_request)
            try:
                return original(client, options, *args, **kwargs)
            finally:
                self._model_request.reset(token)
        return measured

    def _wrap_encode(self, original):
        @wraps(original)
        def measured(value):
            if not self._model_request.get():
                return original(value)
            if self.single_session:
                self._observe_prefix(value)
            self.counts["model_json_encodes"] += 1
            started = thread_time_ns()
            try:
                return original(value)
            finally:
                self.model_json_cpu_ns += thread_time_ns() - started
        return measured

    def _observe_prefix(self, value):
        if not isinstance(value, dict):
            return
        messages = value.get("messages")
        if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
            return
        first = messages[0]
        content = first.get("content")
        if first.get("role") != "system" or not isinstance(content, str):
            return
        start_tag, end_tag = "<core_instructions>\n", "\n</core_instructions>"
        start = content.find(start_tag)
        if start < 0:
            return
        end = content.find(end_tag, start + len(start_tag))
        if end < 0:
            return
        prefix = content[start:end + len(end_tag)]
        if self._prefix is not None and prefix != self._prefix:
            self.prefix_changes += 1
        self._prefix = prefix
        self.prefix_samples += 1

    def report(self):
        return {
            "counts": dict(self.counts),
            "model_json_cpu_ns": self.model_json_cpu_ns,
            "prefix": {
                "enabled": self.single_session,
                "samples": self.prefix_samples,
                "changes": self.prefix_changes if self.single_session else None,
                "boundary": "first system message's core_instructions element",
            },
            "scope": (
                "Synchronous OpenAI SDK openapi_dumps for POST /chat/completions; "
                "thread CPU only. Counts cover this process and include auxiliary "
                "model requests. Wrappers and prefix comparisons add observer overhead; "
                "provider cache hits require provider-reported usage."
            ),
        }

    def __exit__(self, *exc):
        self._stack.close()
        self._prefix = None
