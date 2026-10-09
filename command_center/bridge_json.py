#!/usr/bin/env python3
"""The JSON contract shared by the voice-bridge commands (``ccc inspect/send/answer/…  -j``).

Envelope on stdout: ``{"schema_version": 1, "ok": bool, "data": {…} | null, "error":
{"code": str, "message": str} | null}``; logs go to stderr only. Exit codes: 0 ok,
1 refused/failed (``error.code`` says why), 2 usage, 3 internal. Times are ISO-8601 UTC.
A command that already acted (a delivery exists) reports ``ok: false`` WITH its ``data``
when the outcome is not a success, so the caller can follow the delivery up.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3

_LOG = logging.getLogger(__name__)

#: Max length of a message for ``ccc send`` (characters, after reading stdin).
MESSAGE_MAX = 4000


class BridgeError(Exception):
    """A refusal or failure with a machine-readable ``code`` and an exit code."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = EXIT_REFUSED,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.data = data


def usage(code: str, message: str) -> BridgeError:
    """A usage error (exit 2)."""
    return BridgeError(code, message, EXIT_USAGE)


def iso(ms: int | float | None) -> str | None:
    """Epoch milliseconds → ISO-8601 UTC (``2026-10-09T12:00:00Z``; ``None`` for 0/None)."""
    if not ms:
        return None
    return (
        datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def envelope(
    ok: bool, data: dict[str, Any] | None = None, error: BridgeError | None = None
) -> dict[str, Any]:
    """The §6 envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "data": data,
        "error": {"code": error.code, "message": error.message} if error else None,
    }


def emit(env: dict[str, Any]) -> None:
    """Print *env* as one JSON line on stdout."""
    sys.stdout.write(json.dumps(env, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_json(body: Callable[[], dict[str, Any]]) -> int:
    """Run *body*, print its result as ONE envelope and return the exit code.

    A :class:`BridgeError` becomes ``ok: false`` (with its ``data``, if any) and its exit
    code; any other exception is ``internal`` (exit 3, the traceback goes to stderr).
    """
    try:
        data = body()
    except BridgeError as err:
        emit(envelope(False, err.data, err))
        return err.exit_code
    except Exception as exc:  # pylint: disable=broad-exception-caught  # exit 3, never a trace on stdout
        _LOG.exception("internal error")
        emit(envelope(False, None, BridgeError("internal", f"{type(exc).__name__}: {exc}")))
        return EXIT_INTERNAL
    emit(envelope(True, data))
    return EXIT_OK


def log(message: str) -> None:
    """One log line on stderr (never stdout — stdout carries only the envelope)."""
    sys.stderr.write(message.rstrip("\n") + "\n")
    sys.stderr.flush()


def control_chars(text: str, allowed: str = "\n\t") -> list[str]:
    """Every C0 / DEL / C1 control character in *text* that is not in *allowed*.

    ESC (so ``ESC[201~`` — the end of a bracketed paste — can never be smuggled in),
    NUL and CR are all C0 and therefore rejected.
    """
    bad: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in allowed:
            continue
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            bad.append(f"U+{code:04X}")
    return bad


def validate_message(text: str, *, limit: int = MESSAGE_MAX, what: str = "message") -> str:
    """*text* if it is a sendable message, else a usage :class:`BridgeError`."""
    if not text.strip():
        raise usage("empty_message", f"the {what} is empty")
    if len(text) > limit:
        raise usage("message_too_long", f"the {what} has {len(text)} characters (max {limit})")
    bad = control_chars(text)
    if bad:
        raise usage(
            "control_characters",
            f"the {what} contains control characters ({', '.join(sorted(set(bad))[:5])})",
        )
    return text


def check_fields(obj: dict[str, Any], allowed: set[str], where: str) -> None:
    """Reject unknown fields in an input object (usage error, exit 2)."""
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise usage("unknown_field", f"{where}: unknown field(s) {', '.join(unknown)}")
