import asyncio
import json
import re

from websockets.exceptions import WebSocketException


OUTPUT_SCHEMA_VERSION = 1
SENSITIVE_KEY_RE = re.compile(
    r"token|ticket|authorization|cookie|credential|secret",
    re.I,
)


class UncertainDispatchError(RuntimeError):
    def __init__(self, message: str, retry: dict[str, str]):
        super().__init__(message)
        self.retry = retry


def redact_sensitive(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if SENSITIVE_KEY_RE.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
        return re.sub(r"(?i)([?&]wsTicket=)[^&\s\"']+", r"\1[REDACTED]", value)
    return value


def safe_error_message(error: object) -> str:
    if isinstance(error, (dict, list)):
        return json.dumps(redact_sensitive(error), separators=(",", ":"))
    return str(redact_sensitive(str(error)))


def error_category(error: BaseException) -> str:
    if isinstance(error, UncertainDispatchError):
        return "uncertain_result"
    if isinstance(error, (ValueError, KeyError)):
        return "invalid_request"
    if isinstance(error, FileNotFoundError):
        return "configuration"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(error, (OSError, WebSocketException)):
        return "connection"
    return "remote"


def emit_json(value: object, *, indent: int | None = None, flush: bool = False) -> None:
    if isinstance(value, dict) and "schemaVersion" not in value:
        value = {"schemaVersion": OUTPUT_SCHEMA_VERSION, **value}
    print(json.dumps(redact_sensitive(value), indent=indent), flush=flush)
