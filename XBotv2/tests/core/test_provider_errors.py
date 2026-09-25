"""Provider SDK errors are normalized at the LLM adapter boundary."""

import pytest
import httpx
from anthropic import APIStatusError as AnthropicAPIStatusError
from openai import APIStatusError as OpenAIAPIStatusError

from XBotv2.core.domain import ProviderError
from XBotv2.llm.provider_errors import (
    normalize_sdk_provider_error,
    provider_context_overflow,
)


def _sdk_error(*, status_code, body, message="provider failed", sdk="openai"):
    request = httpx.Request("POST", "https://provider.test/v1/messages")
    response = httpx.Response(status_code, request=request)
    error_type = (
        OpenAIAPIStatusError if sdk == "openai" else AnthropicAPIStatusError
    )
    return error_type(message, response=response, body=body)


@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
def test_sdk_error_uses_code_before_type_and_nested_error_payload(sdk):
    error = _sdk_error(
        status_code=429,
        sdk=sdk,
        body={
            "request_id": "outer-id",
            "error": {
                "code": "specific_code",
                "type": "generic_type",
                "message": "too many requests",
            },
        },
    )

    normalized = normalize_sdk_provider_error(error)

    assert normalized == ProviderError(
        code="specific_code",
        message="provider failed",
        retryable=True,
        category="rate_limit",
        provider_details={
            "status_code": 429,
            "code": "specific_code",
            "type": "generic_type",
            "message": "too many requests",
        },
    )


@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
def test_sdk_error_uses_type_only_when_code_is_absent(sdk):
    error = _sdk_error(
        status_code=400,
        sdk=sdk,
        body={"error": {"code": "", "type": "invalid_request_error"}},
    )

    normalized = normalize_sdk_provider_error(error)

    assert normalized.code == "invalid_request_error"
    assert normalized.category == "provider"
    assert normalized.retryable is False


def test_unknown_error_shape_is_not_interpreted_as_an_sdk_error():
    class ForeignError(Exception):
        status_code = 429
        body = {"error": {"code": "provider_code"}}

    normalized = normalize_sdk_provider_error(ForeignError("foreign"))

    assert normalized.code == "ForeignError"
    assert normalized.retryable is False
    assert normalized.provider_details == {}


def test_transport_exception_is_retryable_without_sdk_shape_guessing():
    normalized = normalize_sdk_provider_error(ConnectionError("disconnected"))

    assert normalized.retryable is True
    assert normalized.category == "transport"


@pytest.mark.parametrize(
    ("body", "status", "types", "codes", "statuses", "prefixes", "expected"),
    [
        ({"error": {"type": "exceed_context_size_error"}}, 400,
         frozenset({"exceed_context_size_error"}), frozenset(), frozenset(), (),
         "exceed_context_size_error"),
        ({"error": {"code": "context_length_exceeded"}}, 400,
         frozenset(), frozenset({"context_length_exceeded"}), frozenset(), (),
         "context_length_exceeded"),
        ({"error": {"message": "prompt is too long for model"}}, 400,
         frozenset(), frozenset(), frozenset(), ("prompt is too long",),
         "prompt is too long for model"),
    ],
)
def test_context_overflow_uses_only_declared_provider_discriminators(
    body,
    status,
    types,
    codes,
    statuses,
    prefixes,
    expected,
):
    error = _sdk_error(status_code=status, body=body, message="too large")

    normalized = provider_context_overflow(
        error,
        types=types,
        codes=codes,
        statuses=statuses,
        message_prefixes=prefixes,
    )

    assert normalized is not None
    assert normalized.category == "context_overflow"
    assert normalized.message == expected
    assert normalized.retryable is False


def test_context_overflow_does_not_match_unlisted_code_or_message():
    error = _sdk_error(
        status_code=400,
        body={"error": {"code": "some_other_error", "message": "bad input"}},
    )

    assert provider_context_overflow(
        error,
        codes=frozenset({"context_length_exceeded"}),
        message_prefixes=("prompt is too long",),
    ) is None
