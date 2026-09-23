#!/usr/bin/env python3
"""Which provider + model every ccc LLM action bills right now — ``ccc llm-routing``.

ccc makes a handful of small headless LLM calls of its own (AIM scoring, the done-check,
sub-goal derive/grade, drift detection, parked-session summaries, the short-AIM label).
Every one goes through the single ``llm_custom_command`` route; provider/model selection
belongs to the external purpose ladder rather than ccc.

This module renders the whole picture as one table — the ccc counterpart to
``ai.py routing`` — and names the config key that turns each row off or moves it.

**Rows routed through ``llm_custom_command`` are resolved for real, not hand-waved.** When
that command is ``ai.py``, we ask it (``ai routing -p <purposes>``, one subprocess for all
of them) what each purpose actually runs, and show ai.py's own painted rung + full fallback
ladder — so the answer never drifts from ai.py's resolution rules, and the colours match
``ai routing``. Any other router stays opaque; we say so rather than guess.

Cost discipline: ``build_parser`` runs on EVERY ``ccc`` invocation (``ccc statusline`` fires
on every prompt render), but an epilog is only ever *printed* for ``--help``. So the caller
gates this behind :func:`help_requested` and nothing here — not the config read, not the
subprocess — touches the hot path. :func:`render` itself is defensive too: it swallows any
failure and degrades to a one-line note rather than crashing ``ccc -h``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

# The label used when a row bills the OpenAI Codex / ChatGPT seat. Matching is done
# case-insensitively on "codex" (:func:`bills_codex`) because ai.py spells its own label
# the other way round ("ChatGPT/Codex seat") and either may reach the cost column.
CODEX_COST = "Codex/ChatGPT seat"
# The label for the GitHub Copilot rung (``opencode`` delegating to the seat's model).
COPILOT_COST = "GitHub Copilot seat"

# Longest a rendered cell may get before it is elided. Keeps the table inside a normal
# terminal even when `llm_custom_command` is a long shell one-liner.
_MAX_CELL = 46

# `ai routing -p …` is pure config resolution (no LLM, no network) and measures ~0.3 s.
# The cap is generous but finite: `ccc -h` must never hang on a wedged router.
_AI_QUERY_TIMEOUT_SEC = 5.0

# Executable basenames we know how to interrogate for a live route.
_AI_BINARIES = frozenset({"ai", "ai.py"})

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Every purpose label ccc exports as CCC_LLM_PURPOSE, in table order.
PURPOSES: tuple[str, ...] = (
    "aim-score",
    "aim-met",
    "subgoal-drift",
    "subgoal-derive",
    "subgoal-grade",
    "summary-nextstep",
    "short-aim",
)


@dataclass(frozen=True)
class Row:
    """One ccc LLM action, fully resolved against the current config."""

    action: str  # the ccc command / trigger a user would recognise
    purpose: str  # the CCC_LLM_PURPOSE label exported to the subprocess
    provider: str  # provider + model, as one cell (may carry ANSI colour)
    cost: str  # whose quota this bills
    switch: str  # the config key that turns it off or moves it
    enabled: bool  # whether the feature is on at all right now
    ladder: str = ""  # the full fallback chain, when the route resolver knows one


def help_requested(argv: Sequence[str] | None = None) -> bool:
    """Whether this process was asked for help — the only time an epilog is printed.

    ``build_parser`` is called on every ``ccc`` run, so the routing block must not be
    built (let alone shelled out for) unless it will actually be shown.
    """
    argv = sys.argv[1:] if argv is None else argv
    return any(arg in ("-h", "--help") for arg in argv)


def bills_codex(cost: str) -> bool:
    """Whether a cost label names the Codex seat, whichever way it is spelled."""
    return "codex" in cost.lower()


def _visible_len(text: str) -> int:
    """Length of *text* as printed — colour escapes take no columns."""
    return len(_ANSI_RE.sub("", text))


def _pad(text: str, width: int) -> str:
    """Left-justify *text* to *width* printed columns, ANSI-safe."""
    return text + " " * max(0, width - _visible_len(text))


def _elide(text: str, limit: int = _MAX_CELL) -> str:
    """Collapse plain (uncoloured) *text* to one line of at most *limit* chars."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _ai_binary(command: str) -> str | None:
    """The ``ai.py`` executable *command* starts with, or ``None`` if it is not one."""
    head = command.strip().split()[0] if command.strip() else ""
    return head if head.rsplit("/", maxsplit=1)[-1] in _AI_BINARIES else None


def fetch_routes(command: str, purposes: Sequence[str]) -> dict[str, tuple[str, str, str]]:
    """``purpose -> (first rung, cost, full ladder)`` as ``ai routing -p …`` reports it.

    One subprocess for every purpose. Returns ``{}`` when the router is not ai.py, is not
    executable, fails, or answers in an unexpected shape — the caller then falls back to
    describing the command instead of inventing a route. Colour is requested only when our
    OWN stdout is a terminal, so ``ccc -h | cat`` stays free of escape sequences.
    """
    binary = _ai_binary(command)
    if not binary or not purposes:
        return {}
    exe = binary if os.path.isabs(binary) else shutil.which(binary)
    if not exe:
        return {}
    env = dict(os.environ)
    env.pop("NO_COLOR", None)
    env.pop("FORCE_COLOR", None)
    env["FORCE_COLOR" if sys.stdout.isatty() else "NO_COLOR"] = "1"
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, "routing", "-p", ",".join(purposes)],
            capture_output=True,
            text=True,
            timeout=_AI_QUERY_TIMEOUT_SEC,
            env=env,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    found: dict[str, tuple[str, str, str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            found[parts[0]] = (parts[1], parts[2], parts[3])
    return found


def _dispatch_route(
    cfg: Config, purpose: str, routes: dict[str, tuple[str, str, str]]
) -> tuple[str, str, str]:
    """``(provider_cell, cost, ladder)`` for a call that goes through ``llm.run_model``.

    Mirrors :func:`llm._dispatch`: an empty or failed command fails the call, with no
    provider fallback inside ccc.
    """
    command = cfg.llm_custom_command.strip()
    if not command:
        return ("(router unset)", "nothing — call fails", "")
    if resolved := routes.get(purpose):
        first, cost, ladder = resolved
        return (f"ai.py → {first}", cost, ladder)
    if _ai_binary(command):
        return (f"ai.py → {_elide(command, 24)}", "ai.py (route query failed)", "")
    return (f"llm_custom_command → {_elide(command, 28)}", "external router", "")


def rows(
    cfg: Config | None = None, routes: dict[str, tuple[str, str, str]] | None = None
) -> list[Row]:
    """Every ccc LLM action, resolved against *cfg* (loaded when omitted).

    *routes* is the live ``ai routing`` answer from :func:`fetch_routes`; omit it (or pass
    ``{}``) to describe the router instead of resolving through it — that keeps this
    function pure for tests and for any caller that must not spawn a subprocess.
    """
    if cfg is None:
        from .config import load_config  # lazy: keep module import light

        cfg = load_config()
    routes = routes or {}

    def dispatch(purpose: str) -> tuple[str, str, str]:
        return _dispatch_route(cfg, purpose, routes)

    specs: list[tuple[str, str, tuple[str, str, str], str, bool]] = [
        (
            "score-aim (on /aim set + on turn)",
            "aim-score",
            dispatch("aim-score"),
            "aim_score_on_set · assess_aim_on_turn · llm_custom_command",
            cfg.aim_score_on_set or cfg.assess_aim_on_turn,
        ),
        (
            "assess-aim (is the AIM met?)",
            "aim-met",
            dispatch("aim-met"),
            "assess_aim_on_turn · llm_custom_command",
            cfg.assess_aim_on_turn,
        ),
        (
            "check-drift (sub-goals vs AIM)",
            "subgoal-drift",
            dispatch("subgoal-drift"),
            "drift_check · llm_custom_command",
            cfg.drift_check,
        ),
        (
            "autoprogress (derive sub-goals)",
            "subgoal-derive",
            dispatch("subgoal-derive"),
            "autoprogress · llm_custom_command",
            cfg.autoprogress,
        ),
        (
            "autoprogress (grade sub-goals)",
            "subgoal-grade",
            dispatch("subgoal-grade"),
            "grade_on_turn · llm_custom_command",
            cfg.grade_on_turn,
        ),
        (
            "daemon summary + next step",
            "summary-nextstep",
            dispatch("summary-nextstep"),
            "summarize · llm_custom_command",
            cfg.summarize,
        ),
        (
            "short-AIM label (/aim column)",
            "short-aim",
            dispatch("short-aim"),
            "short_aim · llm_custom_command",
            cfg.short_aim,
        ),
    ]
    return [
        Row(action, purpose, cell, cost, switch, enabled, ladder)
        for action, purpose, (cell, cost, ladder), switch, enabled in specs
    ]


def codex_spenders(all_rows: list[Row]) -> list[Row]:
    """The ENABLED rows that bill the Codex seat — empty is the desirable state."""
    return [r for r in all_rows if r.enabled and bills_codex(r.cost)]


def _table(headers: tuple[str, ...], body: Sequence[tuple[str, ...]]) -> list[str]:
    """Render a box-drawn table, columns auto-sized to PRINTED width (ANSI-safe)."""
    widths = [
        max([_visible_len(headers[i]), *(_visible_len(row[i]) for row in body)])
        for i in range(len(headers))
    ]

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def line(cells: tuple[str, ...]) -> str:
        return "│ " + " │ ".join(_pad(c, w) for c, w in zip(cells, widths, strict=True)) + " │"

    return [
        rule("┌", "┬", "┐"),
        line(headers),
        rule("├", "┼", "┤"),
        *(line(row) for row in body),
        rule("└", "┴", "┘"),
    ]


def _ladder_block(all_rows: list[Row]) -> list[str]:
    """The per-purpose fallback ladders, or nothing when no route resolved one."""
    seen: dict[str, str] = {}
    for row in all_rows:
        if row.ladder:
            seen.setdefault(row.purpose, row.ladder)
    if not seen:
        return []
    return [
        "Fallback ladder per purpose — first rung that succeeds wins (via `ai routing`):",
        *_table(("purpose", "ladder"), list(seen.items())),
    ]


def render(cfg: Config | None = None, *, live: bool = True) -> str:
    """The whole overview as plain text. Never raises — safe inside ``--help``.

    *live* asks ``ai routing`` for the real route of every purpose it owns (one
    subprocess); pass ``False`` for a config-only rendering.
    """
    try:
        resolved = cfg if cfg is not None else _load_cfg()
        routes = fetch_routes(resolved.llm_custom_command, PURPOSES) if live else {}
        all_rows = rows(resolved, routes)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # A broken/hand-edited config must degrade the table, never break `ccc -h`.
        return f"LLM routing: unavailable ({exc.__class__.__name__}) — check config.toml\n"

    body = [
        (r.action, r.purpose, r.provider, r.cost, "on" if r.enabled else "OFF") for r in all_rows
    ]
    lines = [
        "ccc's own LLM calls — which model each action uses now (`ccc llm-routing`):",
        *_table(("ccc action", "purpose", "runs now", "bills", ""), body),
        *_ladder_block(all_rows),
    ]

    spenders = codex_spenders(all_rows)
    if spenders:
        names = ", ".join(r.purpose for r in spenders)
        lines += [
            f"⚠ The external router currently sends these purposes to Codex: {names}.",
            "  Change their ladders in ai.py; ccc has no provider fallback of its own.",
        ]
    else:
        lines.append("✓ No ccc action bills the Codex seat — it is free for /codex-debate.")

    lines += ["", "Change any row in ~/.claude/command-center/config.toml (keys per action):"]
    lines += [f"  {r.purpose:<17} {r.switch}" for r in all_rows]
    lines += [
        "  llm_custom_command  routes EVERY row above through one external command",
        "                      (purpose in $CCC_LLM_PURPOSE); empty/failure fails the call.",
    ]
    return "\n".join(lines) + "\n"


def _load_cfg() -> Config:
    from .config import load_config  # lazy: keep module import light

    return load_config()
