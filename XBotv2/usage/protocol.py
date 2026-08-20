"""Wire models owned by the usage capability."""

from pydantic import Field

from XBotv2.protocol import WireModel


class UsageData(WireModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    requests: int = Field(default=1, ge=0)
    context_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    prompt_cache_write_tokens: int = Field(default=0, ge=0)


__all__ = ["UsageData"]
