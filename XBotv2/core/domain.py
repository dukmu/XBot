"""Canonical cross-layer value types.

This module contains stable values shared across domain, runtime, and protocol
boundaries. They are deliberately small and immutable; owning layers may
construct them, while other layers only carry or project them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Mapping, NewType, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue

MessageId = NewType("MessageId", str)
InputId = NewType("InputId", str)
TurnId = NewType("TurnId", str)
InteractionId = NewType("InteractionId", str)
NoticeId = NewType("NoticeId", str)
ToolCallId = NewType("ToolCallId", str)
JobId = NewType("JobId", str)
HistoryRevision = NewType("HistoryRevision", str)
Cursor = NewType("Cursor", str)


@dataclass(frozen=True, slots=True)
class SessionScope:
    kind: Literal["session"] = "session"


@dataclass(frozen=True, slots=True)
class TurnScope:
    turn_id: TurnId
    kind: Literal["turn"] = "turn"


EventScope: TypeAlias = Annotated[
    SessionScope | TurnScope,
    Field(discriminator="kind"),
]


class ProviderExtensions(BaseModel):
    provider: str = Field(min_length=1)
    payload: Mapping[str, JsonValue] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True)


ProviderErrorCategory: TypeAlias = Literal[
    "authentication",
    "context_overflow",
    "rate_limit",
    "transport",
    "provider",
    "contract",
]


class ProviderError(BaseModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    category: ProviderErrorCategory
    provider_details: Mapping[str, JsonValue] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelRoute(BaseModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class StandardGenerationMode(BaseModel):
    kind: Literal["standard"] = "standard"
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReasoningGenerationMode(BaseModel):
    kind: Literal["reasoning"] = "reasoning"
    effort: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


GenerationMode: TypeAlias = StandardGenerationMode | ReasoningGenerationMode


class GenerationSettings(BaseModel):
    mode: GenerationMode
    temperature: float | None = None
    max_output_tokens: int = Field(ge=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResolvedModelSelection(BaseModel):
    route: ModelRoute
    generation: GenerationSettings
    context_window: int = Field(ge=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentExecutionLimits(BaseModel):
    max_turns: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResolvedRuntimeSelection(BaseModel):
    agent_name: str = Field(min_length=1)
    prompt: str
    limits: AgentExecutionLimits
    enabled_tools: tuple[str, ...]
    model: ResolvedModelSelection
    model_config = ConfigDict(extra="forbid", frozen=True)


class TokenCounters(BaseModel):
    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_create: int = Field(default=0, ge=0)
    prompt_cache_write: int = Field(default=0, ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)

    def add(self, delta: "TokenCounters") -> "TokenCounters":
        return TokenCounters(
            input=self.input + delta.input,
            output=self.output + delta.output,
            cache_read=self.cache_read + delta.cache_read,
            cache_create=self.cache_create + delta.cache_create,
            prompt_cache_write=(
                self.prompt_cache_write + delta.prompt_cache_write
            ),
        )


class ProviderMeasured(BaseModel):
    kind: Literal["provider_measured"] = "provider_measured"
    tokens: int = Field(ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)


class MeasurementUnavailable(BaseModel):
    kind: Literal["measurement_unavailable"] = "measurement_unavailable"
    reason: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


ObservedContext: TypeAlias = ProviderMeasured | MeasurementUnavailable


class TurnRequest(BaseModel):
    kind: Literal["turn"] = "turn"
    turn_id: TurnId
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuxiliaryRequest(BaseModel):
    kind: Literal["auxiliary"] = "auxiliary"
    owner: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


RequestPurpose: TypeAlias = TurnRequest | AuxiliaryRequest


class RequestObservation(BaseModel):
    selection: ResolvedModelSelection
    purpose: RequestPurpose
    estimated_input_tokens: int = Field(ge=0)
    observed_context: ObservedContext
    model_config = ConfigDict(extra="forbid", frozen=True)


class UsageDelta(BaseModel):
    counters: TokenCounters
    model_config = ConfigDict(extra="forbid", frozen=True)


class UsageSnapshot(BaseModel):
    total_counters: TokenCounters = Field(default_factory=TokenCounters)
    requests: tuple[RequestObservation, ...] = ()
    latest_turn_observation: RequestObservation | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)

    def add(self, observation: RequestObservation, delta: UsageDelta) -> "UsageSnapshot":
        latest = (
            observation
            if isinstance(observation.purpose, TurnRequest)
            else self.latest_turn_observation
        )
        return UsageSnapshot(
            total_counters=self.total_counters.add(delta.counters),
            requests=(*self.requests, observation),
            latest_turn_observation=latest,
        )


class ModelTiming(BaseModel):
    total_ms: float = Field(ge=0)
    first_delta_ms: float | None = Field(default=None, ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def decode_ms(self) -> float | None:
        if self.first_delta_ms is None:
            return None
        return round(max(0.0, self.total_ms - self.first_delta_ms), 3)


class ToolTiming(BaseModel):
    duration_ms: float = Field(ge=0)
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompletedStop(BaseModel):
    kind: Literal["completed"] = "completed"
    model_config = ConfigDict(extra="forbid", frozen=True)


class LengthLimitedStop(BaseModel):
    kind: Literal["length_limited"] = "length_limited"
    limit_kind: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCallsRequestedStop(BaseModel):
    kind: Literal["tool_calls_requested"] = "tool_calls_requested"
    model_config = ConfigDict(extra="forbid", frozen=True)


ModelStop: TypeAlias = CompletedStop | LengthLimitedStop | ToolCallsRequestedStop


class ModelExchange(BaseModel):
    observation: RequestObservation
    usage: UsageDelta
    timing: ModelTiming
    stop: ModelStop
    provider_extensions: ProviderExtensions
    model_config = ConfigDict(extra="forbid", frozen=True)


class StructuredToolOutput(BaseModel):
    """Owner-defined structured tool result boundary."""

    kind: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransactionRef(BaseModel):
    kind: str = Field(min_length=1)
    id: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransactionCommitted(BaseModel):
    kind: Literal["committed"] = "committed"
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransactionAborted(BaseModel):
    kind: Literal["aborted"] = "aborted"
    reason: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransactionFailed(BaseModel):
    kind: Literal["failed"] = "failed"
    error: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


TransactionOutcome: TypeAlias = TransactionCommitted | TransactionAborted | TransactionFailed


class TransactionStarted(BaseModel):
    kind: Literal["transaction_started"] = "transaction_started"
    transaction: TransactionRef
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransactionEnded(BaseModel):
    kind: Literal["transaction_ended"] = "transaction_ended"
    transaction: TransactionRef
    outcome: TransactionOutcome
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = [
    "AgentExecutionLimits",
    "AuxiliaryRequest",
    "CompletedStop",
    "GenerationMode",
    "ReasoningGenerationMode",
    "GenerationSettings",
    "HistoryRevision",
    "InputId",
    "Cursor",
    "InteractionId",
    "JobId",
    "LengthLimitedStop",
    "MeasurementUnavailable",
    "MessageId",
    "ModelExchange",
    "ModelRoute",
    "ModelStop",
    "ModelTiming",
    "NoticeId",
    "ObservedContext",
    "ProviderExtensions",
    "ProviderError",
    "ProviderErrorCategory",
    "ProviderMeasured",
    "RequestObservation",
    "ResolvedModelSelection",
    "ResolvedRuntimeSelection",
    "StandardGenerationMode",
    "StructuredToolOutput",
    "TokenCounters",
    "ToolCallId",
    "ToolCallsRequestedStop",
    "ToolTiming",
    "TransactionAborted",
    "TransactionCommitted",
    "TransactionEnded",
    "TransactionFailed",
    "TransactionOutcome",
    "TransactionRef",
    "TransactionStarted",
    "TurnId",
    "TurnRequest",
    "UsageDelta",
    "UsageSnapshot",
]
