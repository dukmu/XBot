"""Normalize OpenAI-compatible and Anthropic errors at the adapter boundary."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from anthropic import (
    APIError as AnthropicAPIError,
    APIResponseValidationError as AnthropicAPIResponseValidationError,
    APIStatusError as AnthropicAPIStatusError,
    APIConnectionError as AnthropicAPIConnectionError,
    APITimeoutError as AnthropicAPITimeoutError,
    AuthenticationError as AnthropicAuthenticationError,
    RateLimitError as AnthropicRateLimitError,
)
from openai import (
    APIError as OpenAIAPIError,
    APIResponseValidationError as OpenAIAPIResponseValidationError,
    APIStatusError as OpenAIAPIStatusError,
    APIConnectionError as OpenAIAPIConnectionError,
    APITimeoutError as OpenAIAPITimeoutError,
    AuthenticationError as OpenAIAuthenticationError,
    RateLimitError as OpenAIRateLimitError,
)
from pydantic import JsonValue, TypeAdapter, ValidationError

from XBotv2.core.domain import ProviderError, ProviderErrorCategory

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_CONNECTION_ERRORS = (
    OpenAIAPIConnectionError,
    OpenAIAPITimeoutError,
    AnthropicAPIConnectionError,
    AnthropicAPITimeoutError,
)
_RATE_LIMIT_ERRORS = (OpenAIRateLimitError, AnthropicRateLimitError)
_AUTHENTICATION_ERRORS = (
    OpenAIAuthenticationError,
    AnthropicAuthenticationError,
)


def normalize_sdk_provider_error(error: Exception) -> ProviderError:
    """Convert an SDK exception into the provider-neutral Core error value."""
    status_code = _status_code(error)
    payload = _provider_error_payload(error)
    code = _payload_text(payload, "code")
    error_type = _payload_text(payload, "type")
    message = _payload_text(payload, "message")
    if not code:
        code = error_type
    if not code:
        code = f"http_{status_code}" if status_code is not None else type(error).__name__

    if status_code in {401, 403} or isinstance(error, _AUTHENTICATION_ERRORS):
        category: ProviderErrorCategory = "authentication"
    elif status_code == 429 or isinstance(error, _RATE_LIMIT_ERRORS):
        category = "rate_limit"
    elif isinstance(
        error,
        (ConnectionError, TimeoutError, httpx.TransportError, *_CONNECTION_ERRORS),
    ):
        category = "transport"
    else:
        category = "provider"

    return ProviderError(
        code=code,
        message=str(error) or type(error).__name__,
        retryable=retryable_provider_error(error, status_code),
        category=category,
        provider_details=_provider_details(payload, status_code),
    )


def retryable_provider_error(
    error: Exception,
    status_code: int | None,
) -> bool:
    if status_code is not None:
        return status_code in {408, 409, 429} or status_code >= 500
    return isinstance(
        error,
        (
            ConnectionError,
            TimeoutError,
            httpx.TransportError,
            *_CONNECTION_ERRORS,
            *_RATE_LIMIT_ERRORS,
        ),
    )


def provider_context_overflow(
    error: Exception,
    *,
    types: frozenset[str] = frozenset(),
    codes: frozenset[str] = frozenset(),
    statuses: frozenset[int] = frozenset(),
    message_prefixes: tuple[str, ...] = (),
) -> ProviderError | None:
    """Match only the context-overflow vocabulary declared by one adapter."""
    status_code = _status_code(error)
    payload = _provider_error_payload(error)
    error_type = _payload_text(payload, "type")
    code = _payload_text(payload, "code")
    message = _payload_text(payload, "message")
    matched: str | None = None

    if error_type and error_type in types:
        matched = message or error_type
    elif code and code in codes:
        matched = message or code
    elif status_code is not None and status_code in statuses:
        matched = message or f"HTTP {status_code}"
    elif (
        message_prefixes
        and status_code == 400
        and message.startswith(message_prefixes)
    ):
        matched = message
    if matched is None:
        return None

    return ProviderError(
        code=code or error_type or "context_overflow",
        message=matched,
        retryable=False,
        category="context_overflow",
        provider_details=_provider_details(payload, status_code),
    )


def _status_code(error: Exception) -> int | None:
    if isinstance(
        error,
        (
            OpenAIAPIStatusError,
            OpenAIAPIResponseValidationError,
            AnthropicAPIStatusError,
            AnthropicAPIResponseValidationError,
        ),
    ):
        return error.status_code
    return None


def _provider_error_payload(error: Exception) -> Mapping[str, JsonValue] | None:
    """Read and validate a known SDK error envelope before interpretation."""
    if isinstance(error, OpenAIAPIError):
        body = error.body
    elif isinstance(error, AnthropicAPIError):
        body = error.body
    else:
        return None
    if body is None or not isinstance(body, Mapping):
        return None
    try:
        validated_body = _JSON_OBJECT.validate_python(body)
    except ValidationError as error:
        raise TypeError("Provider SDK error body must be a JSON object") from error
    nested = validated_body.get("error")
    if isinstance(nested, Mapping):
        try:
            return _JSON_OBJECT.validate_python(nested)
        except ValidationError as error:
            raise TypeError("Provider SDK error payload must be a JSON object") from error
    return validated_body


def _payload_text(payload: Mapping[str, JsonValue] | None, field: str) -> str:
    if payload is None:
        return ""
    value = payload.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"Provider SDK error {field} must be text or None")
    return value


def _provider_details(
    payload: Mapping[str, JsonValue] | None,
    status_code: int | None,
) -> dict[str, JsonValue]:
    details: dict[str, JsonValue] = {}
    if status_code is not None:
        details["status_code"] = status_code
    if payload is not None:
        for key, value in payload.items():
            if not isinstance(key, str):
                raise TypeError("Provider SDK error detail keys must be text")
            details[key] = value
    return details
