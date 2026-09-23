#!/usr/bin/env python3
"""The OpenCode free-tier probe behind ``ccc quota -P``: what it asks, what it records,
and when the hourly agent runs it next.

Zen publishes no meter for its free tier, so one free request is the only measurement
(:func:`command_center.usage.probe_opencode_free`). Since 2026-09-23 (tp#392) three
things surround that request:

* **The target comes from the ladder registry.** ``ai ladders -j`` (the ``ai.py`` CLI)
  names the rungs every routed LLM call walks; the probe asks the model of the FIRST
  ``opencode-free`` rung of the ``cheap`` ladder, because that is the model a cheap call
  would really spend. ``opencode_free_model`` from config.toml is used only when ai.py is
  missing, fails, or names no such rung. There is no fallback ROUTING: the probe asks
  one model, once, and reports what that model did.
* **Every probe is a ledger row** (:mod:`command_center.codex_ledger`, provider
  ``opencode``, seat ``free``, purpose :data:`PURPOSE`), inconclusive ones included, so
  ``ai logs`` shows the hourly spend like any other call.
* **An hourly LaunchAgent runs it** (:func:`command_center.launchd.quota_probe_plist`),
  and the free row says when the next run is due (``next probe in N min``) — only while
  that agent's plist is installed; a row with no scheduler behind it promises nothing.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

import json
import os
import plistlib
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from . import codex_ledger, config, launchd, usage

#: The ledger ``purpose`` of one probe.
PURPOSE = "probe-opencode-free"
#: The registry ladder and tool whose first rung the probe asks.
LADDER = "cheap"
TOOL = "opencode-free"
#: The ``model`` prefix opencode's ``-m`` wants; the registry may or may not spell it.
_ZEN_PREFIX = "opencode/"
#: ``ai ladders -j`` reads one TOML file; anything slower than this is a hung ai.py.
_AI_TIMEOUT_SEC = 20


@dataclass
class ProbeOutcome:
    """One ``ccc quota -P`` run: the verdict, and what it was asked and why."""

    ok: bool | None  # True served, False refused, None inconclusive
    detail: str
    model: str  # bare Zen id (no ``opencode/`` prefix)
    source: str  # "ai ladders" | "config"
    ms: int
    recorded: bool  # the ledger row was written


def _cheap_ladder(payload: Any) -> Any:
    """The ``cheap`` ladder out of ``ai ladders -j``.

    Accepts ``{"ladders": {name: ladder}}``, ``{"ladders": [{"name": …}, …]}`` and the
    bare mapping / list, so the reader does not pin a JSON layout the registry is free to
    wrap in an envelope later.
    """
    ladders = payload.get("ladders", payload) if isinstance(payload, dict) else payload
    if isinstance(ladders, dict):
        return ladders.get(LADDER)
    if isinstance(ladders, list):
        return next(
            (lad for lad in ladders if isinstance(lad, dict) and lad.get("name") == LADDER), None
        )
    return None


def _tool_and_model(rung: Any) -> tuple[str, str]:
    """A rung as ``(tool, model)``: an object with those keys, or the registry's own
    string spelling (``"opencode-free opencode/muse-… [flags]"``)."""
    if isinstance(rung, dict):
        return str(rung.get("tool") or ""), str(rung.get("model") or "")
    if isinstance(rung, str) and len(parts := rung.split()) >= 2:
        return parts[0], parts[1]
    return "", ""


def model_from_ladders(payload: Any) -> str:
    """The model of the first ``opencode-free`` rung of the cheap ladder, without the
    ``opencode/`` prefix; "" when the payload has no such rung."""
    ladder = _cheap_ladder(payload)
    rungs = ladder.get("rungs") if isinstance(ladder, dict) else None
    for rung in rungs if isinstance(rungs, list) else []:
        tool, model = _tool_and_model(rung)
        if tool == TOOL and model:
            return model.removeprefix(_ZEN_PREFIX)
    return ""


def probe_target() -> tuple[str, str]:
    """``(model, source)`` — the registry's answer, else config.toml's (see module doc)."""
    # pylint: disable-next=import-outside-toplevel  # capability-scoped (the extdeps convention)
    from . import external_deps

    exe = external_deps.ai_exe()
    if exe:
        env = {**os.environ, "NO_COLOR": "1"}
        env.pop("FORCE_COLOR", None)
        try:
            proc = subprocess.run(  # noqa: S603
                [exe, "ladders", "-j"],
                capture_output=True,
                text=True,
                timeout=_AI_TIMEOUT_SEC,
                env=env,
                check=False,
            )
            if proc.returncode == 0 and (model := model_from_ladders(json.loads(proc.stdout))):
                return model, "ai ladders"
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return config.load_config().opencode_free_model, "config"


def _ledger_row(outcome: ProbeOutcome) -> dict[str, Any]:
    """The ``ccc record-run`` shape of one probe (validated before it is appended)."""
    label = {True: "ok", False: "refused", None: "inconclusive"}[outcome.ok]
    row: dict[str, Any] = {
        "provider": "opencode",
        "seat": "free",
        "purpose": PURPOSE,
        "outcome": label,
        "ok": outcome.ok is True,
        "ms": outcome.ms,
        "model": _ZEN_PREFIX + outcome.model,
        "caller": "ccc quota -P",
        "note": f"target from {outcome.source}",
    }
    if outcome.ok is not True:
        row["error"] = label
        if outcome.detail:
            row["error_message"] = outcome.detail
    return row


def run(now: int | None = None) -> ProbeOutcome:
    """Probe once: resolve the target, ask it, store the verdict, write the ledger row.

    An inconclusive probe updates NOTHING in the usage cache (a slow network must not
    overwrite a real verdict) but is still a ledger row: it was a physical request.
    """
    now = int(time.time()) if now is None else now
    model, source = probe_target()
    started = time.monotonic()
    ok, detail = usage.probe_opencode_free(model)
    outcome = ProbeOutcome(
        ok, detail, model, source, int((time.monotonic() - started) * 1000), False
    )
    if ok is not None:
        usage.record_opencode_probe(ok, detail, now)
    try:
        outcome.recorded = bool(
            codex_ledger.append_rows([codex_ledger.validate_row(_ledger_row(outcome))])
        )
    except (OSError, ValueError):
        outcome.recorded = False
    return outcome


def installed_interval() -> int:
    """The installed probe agent's ``StartInterval`` in seconds; 0 when it is not installed
    (or its plist is unreadable — a schedule we cannot read is not one we can promise)."""
    try:
        with launchd.quota_probe_plist_path().open("rb") as fh:
            data = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return 0
    interval = data.get("StartInterval") if isinstance(data, dict) else None
    return interval if isinstance(interval, int) and interval > 0 else 0


def last_probe_at(rows: list[dict[str, Any]]) -> int:
    """Epoch of the newest :data:`PURPOSE` ledger row; 0 for none."""
    stamps = [
        codex_ledger._epoch(str(row["ts"]))  # noqa: SLF001
        for row in rows
        if row.get("purpose") == PURPOSE
    ]
    return max(stamps, default=0)


def next_probe_at(last_verdict_at: int = 0) -> int:
    """When the hourly agent runs next: the last probe + the installed interval.

    *last_verdict_at* is the usage cache's own probe stamp, for a probe older than the
    ledger. 0 = no agent installed or no probe ever — the row then says nothing.
    """
    interval = installed_interval()
    if not interval:
        return 0
    last = max(last_probe_at(codex_ledger.read_runs()), last_verdict_at)
    return last + interval if last else 0


def next_probe_note(at: int, now: int) -> str:
    """``next probe in 42 min`` (``next probe due`` once the instant has passed)."""
    if not at:
        return ""
    left = at - now
    return f"next probe in {-(-left // 60)} min" if left > 0 else "next probe due"
