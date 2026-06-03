"""Context builder with plugin fragment injection points.

Assembles the provider message list for each LLM call. Supports injection
points where plugins can add text without core knowing about plugin content.

Message structure (cache-friendly):
    [SystemMessage: system_prefix]
    [SystemMessage: plugin fragments (system_instructions stage)]
    [SystemMessage: runtime rules]
    [SystemMessage: sandbox summary]
    [... message history ...]
    [SystemMessage: plugin fragments (dag_suffix stage)]
    [SystemMessage: current state]
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContextBuilder:
    """Assembles provider message lists with plugin extension points.

    Fragment stages in render order:
    - "system_prefix": inserted after the system base prompt
    - "system_instructions": inserted after agent instructions
    - "system_rules": inserted after runtime rules
    - "dag_suffix": inserted at the end, after message history

    Plugins register fragments at named stages. Core renders them in
    order but never inspects their content.

    Cache: the stable system prefix is memoized per session. Dynamic
    fragments and suffix are rebuilt each turn.
    """

    FRAGMENT_STAGES = (
        "system_prefix",
        "system_instructions",
        "system_rules",
        "dag_suffix",
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

    def register_fragment(self, stage: str, plugin_name: str, text: str) -> None:
        """Register a prompt fragment from a plugin."""
        if stage not in self.FRAGMENT_STAGES:
            raise ValueError(
                f"Unknown fragment stage: {stage!r}. "
                f"Choose from {self.FRAGMENT_STAGES}"
            )
        self._fragments[stage][plugin_name] = text
        self.invalidate_cache()

    def unregister_fragment(self, stage: str, plugin_name: str) -> None:
        """Remove a plugin's fragment."""
        self._fragments.get(stage, {}).pop(plugin_name, None)
        self.invalidate_cache()

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
        messages: list[BaseMessage],
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
    ) -> list[BaseMessage]:
        """Build the complete message list for an LLM call.

        Args:
            messages: Message history so far.
            agent_name, agent_role, user_name, user_id: Identity fields.
            instructions: Agent instructions text.
            memory: Long-term memory text.
            sandbox_summary: Sandbox status text.
            system_notice: Runtime notice.
            turn_count, mailbox_pending, active_subagents: Current state.
        """
        result: list[BaseMessage] = []

        # 1. Stable system prefix (memoized)
        result.append(SystemMessage(content=self._build_system_prefix(
            agent_name=agent_name,
            agent_role=agent_role,
            user_name=user_name,
            user_id=user_id,
            instructions=instructions,
            memory=memory,
            sandbox_summary=sandbox_summary,
            system_notice=system_notice,
        )))

        # 2. Plugin fragments: system_instructions stage
        for text in self._fragments["system_instructions"].values():
            if text.strip():
                result.append(SystemMessage(content=text))

        # 3. Runtime rules (hardcoded — plugins can add via system_rules stage)
        rules = self._build_rules(mailbox_pending)
        result.append(SystemMessage(content=rules))

        # 4. Plugin fragments: system_rules stage
        for text in self._fragments["system_rules"].values():
            if text.strip():
                result.append(SystemMessage(content=text))

        # 5. History (sanitized)
        result.extend(self._sanitize_history(messages))

        # 6. Plugin fragments: dag_suffix stage (dynamic)
        suffix_parts: list[str] = []
        for text in self._fragments["dag_suffix"].values():
            if text.strip():
                suffix_parts.append(text)

        # 7. Current state (always last)
        suffix_parts.append(self._build_current_state(
            turn_count=turn_count,
            mailbox_pending=mailbox_pending,
            active_subagents=active_subagents,
            user_name=user_name,
            user_id=user_id,
        ))
        result.append(SystemMessage(content="\n\n".join(suffix_parts)))

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
        key = hashlib.sha256(
            "|".join([
                agent_name, agent_role, user_name, user_id,
                instructions, memory, sandbox_summary, system_notice,
            ]).encode()
        ).hexdigest()

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
            f"Time: {_now_iso()}",
            f"User: {user_name} ({user_id})",
            f"Turn: {turn_count + 1}",
        ]
        if mailbox_pending > 0:
            lines.append(f"Pending mailbox: {mailbox_pending}")
        if active_subagents > 0:
            lines.append(f"Active subagents: {active_subagents}")
        return "\n".join(lines)

    @staticmethod
    def _sanitize_history(messages: list[BaseMessage]) -> list[BaseMessage]:
        """Drop orphan ToolMessages before sending to providers."""
        valid_tool_call_ids: set[str] = set()
        sanitized: list[BaseMessage] = []

        for msg in messages:
            if isinstance(msg, AIMessage):
                for call in getattr(msg, "tool_calls", []) or []:
                    call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                    if call_id:
                        valid_tool_call_ids.add(str(call_id))
                sanitized.append(msg)
            elif isinstance(msg, ToolMessage):
                tool_call_id = getattr(msg, "tool_call_id", None)
                if tool_call_id and str(tool_call_id) in valid_tool_call_ids:
                    sanitized.append(msg)
            else:
                sanitized.append(msg)

        return sanitized
