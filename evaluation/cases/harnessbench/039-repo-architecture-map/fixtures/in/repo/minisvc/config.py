from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str
    readonly: bool = False


def load_settings(env: dict[str, str]) -> Settings:
    return Settings(
        database_path=env.get("MINISVC_DB", "orders.sqlite"),
        readonly=env.get("MINISVC_READONLY", "0") == "1",
    )
