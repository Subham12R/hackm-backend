from dataclasses import dataclass


def decode_pem_env(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace("\\n", "\n")


@dataclass(frozen=True)
class Settings:
    database_url: str | None = None
