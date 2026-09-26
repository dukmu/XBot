"""Compile canonical conversation values into provider request messages."""

from __future__ import annotations

from typing import Sequence

from XBotv2.config.contracts import UserContext
from XBotv2.context_builder.contracts import (
    BuiltContext,
    ContextComponent,
    FilePromptComponent,
    HistoryComponent,
    InlinePromptComponent,
    PromptComponent,
    PromptStage,
)
from XBotv2.core.domain import ResolvedRuntimeSelection
from XBotv2.core.messages import (
    AssistantMessage,
    CompactionSummaryMessage,
    ConversationMessage,
    HumanInputMessage,
    RuntimeNoticeMessage,
    ToolMessage,
)
from XBotv2.core.parts import ImagePart, ReasoningPart, TextPart
from XBotv2.core.provider import (
    ProviderAssistant,
    ProviderMessage,
    ProviderSystem,
    ProviderTool,
    ProviderUser,
    ResolvedImagePart,
)
from XBotv2.core.artifacts import ArtifactRef, ArtifactStorePort
from XBotv2.core.tools import ToolFailed, ToolSucceeded
from XBotv2.core.prompts import prompt_container, prompt_element
from XBotv2.core.variables import RuntimeVariables


CORE_INSTRUCTIONS = """You are an Agent running in XBotv2. Follow the configured instruction hierarchy and report only verified results."""

_NAMED_PROMPT_SOURCES = frozenset({
    "core_instructions",
    "runtime_environment",
    "agent_identity",
    "agent_instructions",
    "memory",
})


class ContextBuilder:
    PROMPT_STAGES: tuple[PromptStage, ...] = (
        "system_prefix", "system_instructions", "system_rules", "context_suffix",
    )

    def __init__(self, artifacts: ArtifactStorePort | None = None) -> None:
        self._artifacts = artifacts
        self._components: dict[str, list[PromptComponent]] = {}

    def register_component(self, owner: str, component: PromptComponent) -> None:
        if not owner:
            raise ValueError("Prompt component owner must be non-empty")
        if not isinstance(component, (InlinePromptComponent, FilePromptComponent)):
            raise TypeError("Prompt registry accepts prompt components only")
        if component.stage not in self.PROMPT_STAGES:
            raise ValueError(f"Unknown prompt component stage: {component.stage!r}")
        self._components.setdefault(owner, []).append(component)

    def unregister_owner(self, owner: str) -> None:
        self._components.pop(owner, None)

    @property
    def artifacts(self) -> ArtifactStorePort | None:
        return self._artifacts

    def build(
        self,
        *,
        history: Sequence[ConversationMessage],
        runtime_selection: ResolvedRuntimeSelection,
        user_identity: UserContext,
        memory: str,
        sandbox_summary: str,
        runtime_paths: RuntimeVariables,
        turn: int,
    ) -> list[ProviderMessage]:
        return self.messages_from_components(self.build_components(
            history=history,
            runtime_selection=runtime_selection,
            user_identity=user_identity,
            memory=memory, sandbox_summary=sandbox_summary,
            runtime_paths=runtime_paths,
            turn=turn,
        ), artifacts=self._artifacts)

    def build_components(
        self,
        *,
        history: Sequence[ConversationMessage],
        runtime_selection: ResolvedRuntimeSelection,
        user_identity: UserContext,
        memory: str,
        sandbox_summary: str,
        runtime_paths: RuntimeVariables,
        turn: int,
    ) -> BuiltContext:
        if turn < 0:
            raise ValueError("Context turn must be non-negative")
        components: list[ContextComponent] = [
            InlinePromptComponent(
                stage="system_prefix",
                source="core_instructions",
                text=CORE_INSTRUCTIONS,
            )
        ]
        runtime = [f"Human: {user_identity.user_name} ({user_identity.user_id})"]
        if runtime_paths:
            runtime.append("Model-visible runtime paths:\n" + "\n".join(
                f"- {name}: {value}" for name, value in runtime_paths.items()))
        if sandbox_summary:
            runtime.append(f"Sandbox and permissions:\n{sandbox_summary}")
        components.append(InlinePromptComponent(
            stage="system_prefix",
            source="runtime_environment",
            text="\n\n".join(runtime),
        ))
        for stage, source, content in (
            ("system_instructions", "agent_identity", f"Name: {runtime_selection.agent_name}"),
            ("system_instructions", "agent_instructions", runtime_selection.prompt),
            ("context_suffix", "memory", memory),
        ):
            if content.strip():
                components.append(InlinePromptComponent(
                    stage=stage,
                    source=source,
                    text=content.strip(),
                ))
        registered = [
            component
            for owned in self._components.values()
            for component in owned
        ]
        components.extend(
            component
            for component in registered
            if component.text.strip()
        )
        components.extend(HistoryComponent(message=message) for message in history)
        return BuiltContext(components)

    @staticmethod
    def messages_from_components(
        built_context: BuiltContext,
        *,
        artifacts: ArtifactStorePort | None = None,
    ) -> list[ProviderMessage]:
        system_parts: list[str] = []
        result: list[ProviderMessage] = []
        prompts: list[PromptComponent] = []
        history: list[HistoryComponent] = []
        for component in built_context.components:
            if isinstance(component, (InlinePromptComponent, FilePromptComponent)):
                prompts.append(component)
            elif isinstance(component, HistoryComponent):
                history.append(component)
            else:
                raise TypeError(
                    f"Unsupported context component: {type(component).__name__}"
                )
        prompts.sort(
            key=lambda component: ContextBuilder.PROMPT_STAGES.index(component.stage)
        )
        system_parts.extend(_render_system_component(component) for component in prompts)
        result.extend(
            _compile_message(component.message, artifacts=artifacts)
            for component in history
        )
        if system_parts:
            result.insert(0, ProviderSystem(parts=(TextPart(text=prompt_container("xbot_context", system_parts)),)))
        return result


def _render_system_component(component: ContextComponent) -> str:
    if isinstance(component, HistoryComponent):
        raise TypeError("History components are not system prompt components")
    if component.source in _NAMED_PROMPT_SOURCES:
        return prompt_element(component.source, component.text)
    attributes = {"name": component.source, "stage": component.stage}
    if isinstance(component, FilePromptComponent):
        attributes["source"] = component.logical_path
    return prompt_element(
        "plugin_instruction",
        component.text,
        attributes=attributes,
    )


def _compile_message(
    message: ConversationMessage,
    *,
    artifacts: ArtifactStorePort | None = None,
) -> ProviderMessage:
    if isinstance(message, (HumanInputMessage, RuntimeNoticeMessage)):
        parts: list[TextPart | ResolvedImagePart] = []
        for part in message.parts:
            if isinstance(part, TextPart):
                parts.append(part)
                continue
            if artifacts is None:
                raise RuntimeError("Context compilation requires an ArtifactStore for images")
            ref = ArtifactRef(
                id=part.image.artifact_id,
                media_type=part.image.media_type,
                size=part.image.size,
            )
            parts.append(ResolvedImagePart(ref=part.image, absolute_path=artifacts.model_path(ref)))
        attachment_instruction = _artifact_instruction(
            message.artifacts,
            artifacts,
            missing_store_message=(
                "Context compilation requires an ArtifactStore for attachments"
            ),
        )
        if attachment_instruction is not None:
            parts.append(attachment_instruction)
        return ProviderUser(parts=tuple(parts))
    if isinstance(message, CompactionSummaryMessage):
        return ProviderSystem(parts=(TextPart(text=prompt_container(
            "historical_context",
            [prompt_element("conversation_summary", message.summary)],
            attributes={"source": "compaction"},
        )),))
    if isinstance(message, AssistantMessage):
        return ProviderAssistant(parts=message.parts)
    if isinstance(message, ToolMessage):
        if isinstance(message.outcome, (ToolSucceeded, ToolFailed)):
            parts = list(message.outcome.output.parts)
            attachment_instruction = _artifact_instruction(
                message.outcome.output.artifacts,
                artifacts,
                missing_store_message=(
                    "Context compilation requires an ArtifactStore for tool outputs"
                ),
            )
            if attachment_instruction is not None:
                parts.append(attachment_instruction)
        else:
            parts = (TextPart(text="Tool execution did not produce output"),)
        return ProviderTool(call_id=str(message.call.id), parts=tuple(parts))
    raise TypeError(f"Unsupported conversation message: {type(message).__name__}")


def _artifact_instruction(
    refs: Sequence[ArtifactRef],
    artifacts: ArtifactStorePort | None,
    *,
    missing_store_message: str,
) -> TextPart | None:
    if not refs:
        return None
    if artifacts is None:
        raise RuntimeError(missing_store_message)
    return TextPart(text=prompt_container(
        "attachments",
        [
            prompt_element(
                "attachment",
                "Use filesystem or shell tools to inspect this file when needed.",
                attributes={
                    "name": ref.name,
                    "media_type": ref.media_type,
                    "path": artifacts.model_path(ref),
                    "size": ref.size,
                },
            )
            for ref in refs
        ],
    ))


__all__ = ["CORE_INSTRUCTIONS", "ContextBuilder"]
