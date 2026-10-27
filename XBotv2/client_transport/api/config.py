"""HTTP client transport configuration."""
from pydantic import BaseModel, ConfigDict


class ClientTransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    base_url: str = "http://127.0.0.1:4096"
    uds_path: str | None = None
