from dataclasses import dataclass


@dataclass(frozen=True)
class ApiError(Exception):
    status_code: int
    code: str
