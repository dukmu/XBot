"""Context builder with plugin fragment injection points.

Assembles the provider message list for each LLM call. Supports injection
points where plugins can add text without core knowing about plugin content.

Message structure (cache-friendly):
    [system prefix]
    [plugin fragments (system_instructions stage)]
    [runtime rules]
    [sandbox summary]
    [... message history ...]
    [plugin fragments (context_suffix stage)]
    [current state]
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from xbotv2.api.context import ContextComponent, PromptFragmentStage
from xbotv2.api.messages import Message


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContextBuilder:
    """Assembles provider message lists with plugin extension points.

    Fragment stages in render order:
    - "system_prefix": inserted after the system base prompt
    - "system_instructions": inserted after agent instructions
    - "system_rules": inserted after runtime rules
    - "context_suffix": inserted at the end, after message history

    Plugins register fragments at named stages. Core renders them in
    order but never inspects their content.

    Cache: the stable system prefix is memoized per session. Dynamic
    fragments and suffix are rebuilt each turn.
    """

    FRAGMENT_STAGES: tuple[PromptFragmentStage, ...] = (
        "system_prefix",
        "system_instructions",
        "system_rules",
        "context_suffix",
    )

    def __init__(self) -> None:
        # fragments[stage][plugin_name] = text
        self._fragments: dict[str, dict[str, str]] = {
            stage: {} for stage in self.FRAGMENT_STAGES
        }
        # Explicit cache (NOT module-level — test-safe)
        self._cached_prefix: str | None = None
        self._cached_prefix_key: str = ""

    # ------------------------------------------------------------------
    # Fragment registration (called by plugin loader)
    # ------------------------------------------------------------------

    def register_fragment(
        self,
        stage: PromptFragmentStage,
        plugin_name: str,
        text: str,
    ) -> None:
        """Register a prompt fragment from a plugin."""
        if stage not in self.FRAGMENT_STAGES:
            raise ValueError(
                f"Unknown fragment stage: {stage!r}. "
                f"Choose from {self.FRAGMENT_STAGES}"
            )
        self._fragments[stage][plugin_name] = text
        self.invalidate_cache()

    def unregister_fragment(
        self,
        stage: PromptFragmentStage,
        plugin_name: str,
    ) -> None:
        """Remove a plugin's fragment."""
        self._fragments.get(stage, {}).pop(plugin_name, None)
        self.invalidate_cache()

    def get_fragment(
        self,
        stage: PromptFragmentStage,
        plugin_name: str,
    ) -> str | None:
        return self._fragments.get(stage, {}).get(plugin_name)

    def invalidate_cache(self) -> None:
        """Force the stable prefix to be rebuilt next call."""
        self._cached_prefix = None
        self._cached_prefix_key = ""

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(
        self,
        *,
        messages: list[Message],
        agent_name: str = "XBotv2",
        agent_role: str = "",
        user_name: str = "User",
        user_id: str = "default-user",
        instructions: str = "",
        memory: str = "",
        sandbox_summary: str = "",
        system_notice: str = "",
        turn_count: int = 0,
        mailbox_pending: int = 0,
        active_subagents: int = 0,
    ) -> list[Message]:
        """Build the complete message list for an LLM call."""
        return self.messages_from_components(self.build_components(
            messages=messages,
            agent_name=agent_name,
            agent_role=agent_role,
            user_name=user_name,
            user_id=user_id,
            instructions=instructions,
            memory=memory,
            sandbox_summary=sandbox_summary,
            system_notice=system_notice,
            turn_count=turn_count,
            mailbox_pending=mailbox_pending,
            active_subagents=active_subagents,
        ))

    def build_components(
        self,
        *,
        messages: list[Message],
        agent_name: str = "XBotv2",
        agent_role: str = "",
        user_name: str = "User",
        user_id: str = "default-user",
        instructions: str = "",
        memory: str = "",
        sandbox_summary: str = "",
        system_notice: str = "",
        turn_count: int = 0,
        mailbox_pending: int = 0,
        active_subagents: int = 0,
    ) -> list[ContextComponent]:
        """Build source-tagged context components in provider render order."""
        components: list[ContextComponent] = []

        components.append(ContextComponent(
            role="system",
            source="system_prefix",
            content=self._build_system_prefix(
                agent_name=agent_name,
                agent_role=agent_role,
                user_name=user_name,
                user_id=user_id,
                instructions=instructions,
                memory=memory,
                sandbox_summary=sandbox_summary,
                system_notice=system_notice,
            ),
        ))

        for plugin_name, text in self._fragments["system_instructions"].items():
            if text.strip():
                components.append(ContextComponent(
                    role="system",
                    source="plugin_fragment",
                    content=text,
                    plugin_name=plugin_name,
                    stage="system_instructions",
                ))

        components.append(ContextComponent(
            role="system",
            source="runtime_rules",
            content=self._build_rules(mailbox_pending),
        ))

        for plugin_name, text in self._fragments["system_rules"].items():
            if text.strip():
                components.append(ContextComponent(
                    role="system",
                    source="plugin_fragment",
                    content=text,
                    plugin_name=plugin_name,
                    stage="system_rules",
                ))

        for message in self._sanitize_history(messages):
            components.append(ContextComponent(
                role=message.role,
                source="history",
                content=str(getattr(message, "content", "")),
                message=message,
            ))

        suffix_parts: list[str] = []
        suffix_owners: list[str] = []
        for plugin_name, text in self._fragments["context_suffix"].items():
            if text.strip():
                suffix_parts.append(text)
                suffix_owners.append(plugin_name)

        current_state = self._build_current_state(
            turn_count=turn_count,
            mailbox_pending=mailbox_pending,
            active_subagents=active_subagents,
            user_name=user_name,
            user_id=user_id,
        )
        suffix_parts.append(current_state)
        components.append(ContextComponent(
            role="system",
            source="context_suffix",
            content="\n\n".join(suffix_parts),
            plugin_name=",".join(suffix_owners) if suffix_owners else None,
            stage="context_suffix" if suffix_owners else None,
        ))

        return components

    @staticmethod
    def messages_from_components(components: list[ContextComponent]) -> list[Message]:
        result: list[Message] = []
        for index, component in enumerate(components):
            if not isinstance(component, ContextComponent):
                raise TypeError(
                    f"context component {index} must be a ContextComponent"
                )
            if component.message is not None:
                result.append(component.message)
            else:
                result.append(Message(role="system", content=component.content))
        return result

    # ------------------------------------------------------------------
    # Sub-builders
    # ------------------------------------------------------------------

    def _build_system_prefix(
        self,
        agent_name: str,
        agent_role: str,
        user_name: str,
        user_id: str,
        instructions: str,
        memory: str,
        sandbox_summary: str,
        system_notice: str,
    ) -> str:
        """Build the stable system prompt prefix. Memoized."""
        key = (agent_name, agent_role, user_name, user_id,
               instructions, memory, sandbox_summary, system_notice)

        if self._cached_prefix is not None and key == self._cached_prefix_key:
            return self._cached_prefix

        parts = [
            f"You are {agent_name}, {agent_role}.",
            f"User: {user_name} ({user_id})",
            "",
        ]

        if instructions:
            parts.append(f"## Instructions\n{instructions}\n")

        if memory:
            parts.append(f"## Memory\n{memory}\n")

        if system_notice:
            parts.append(f"## System Notice\n{system_notice}\n")

        if sandbox_summary:
            parts.append(f"## Sandbox\n{sandbox_summary}\n")

        # Plugin fragments: system_prefix stage
        for text in self._fragments["system_prefix"].values():
            if text.strip():
                parts.append(text)

        result = "\n".join(parts)
        self._cached_prefix = result
        self._cached_prefix_key = key
        return result

    @staticmethod
    def _build_rules(mailbox_pending: int) -> str:
        """Build the runtime rules section."""
        rules = [
            "## Runtime Rules",
            "- Always use tools to interact with the system.",
            "- Read files before writing; check existence before creating.",
            "- Never invent file contents or command outputs.",
            "- If a tool fails, explain the error and suggest alternatives.",
            "- Be concise but complete.",
        ]
        if mailbox_pending > 0:
            rules.append(
                f"- You have {mailbox_pending} pending mailbox message(s). "
                f"Use mailbox_read to check them."
            )
        return "\n".join(rules)

    @staticmethod
    def _build_current_state(
        turn_count: int,
        mailbox_pending: int,
        active_subagents: int,
        user_name: str,
        user_id: str,
    ) -> str:
        """Build the current-state suffix section."""
        lines = [
            "# Current State",
            f"Time: {now_iso()}",
            f"User: {user_name} ({user_id})",
            f"Turn: {turn_count + 1}",
        ]
        if mailbox_pending > 0:
            lines.append(f"Pending mailbox: {mailbox_pending}")
        if active_subagents > 0:
            lines.append(f"Active subagents: {active_subagents}")
        return "\n".join(lines)

    @staticmethod
    def _sanitize_history(messages: list[Message]) -> list[Message]:
        valid_tool_call_ids: set[str] = set()
        sanitized: list[Message] = []

        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for call in msg.tool_calls:
                    call_id = call.id
                    if call_id:
                        valid_tool_call_ids.add(call_id)
                sanitized.append(msg)
            elif msg.role == "tool":
                if msg.tool_call_id and msg.tool_call_id in valid_tool_call_ids:
                    sanitized.append(msg)
            else:
                sanitized.append(msg)

        return sanitized
