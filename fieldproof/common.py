from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any


class Problem(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Any = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


def missing(kind: str, key: str) -> Problem:
    return Problem(404, f"{kind}_missing", f"Unknown {kind}: {key}")


def conflict(code: str, message: str, *, details=None) -> Problem:
    return Problem(409, code, message, details=details)


def invalid(code: str, message: str, *, details=None) -> Problem:
    return Problem(422, code, message, details=details)


def encoded(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: Any) -> str:
    return sha256(encoded(value).encode("utf-8")).hexdigest()


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def utc_now() -> str:
    return utc_text(datetime.now(UTC))


def changed_fields(before: dict, after: dict) -> list[str]:
    keys = before.keys() | after.keys()
    return sorted(key for key in keys if before.get(key) != after.get(key))
