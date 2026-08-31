#!/usr/bin/env python3
"""Which provider + model every ccc LLM action bills right now — ``ccc llm-routing``.

ccc makes a handful of small headless LLM calls of its own (AIM scoring, the done-check,
sub-goal derive/grade, drift detection, parked-session summaries, the short-AIM label).
Every one of them bills *somebody's* quota, and which one is spread across half a dozen
config keys (``score_backends``, ``short_aim_backend``, ``llm_custom_command``,
``llm_account``, the per-action ``*_model`` overrides). That made "what is spending my
Codex seat?" a source-reading exercise — on 2026-08-31 the answer turned out to be a
single key (``short_aim_backend``) burning ~17.6k Codex tokens per ten-word label.

This module renders the whole picture as one table — the ccc counterpart to
``ai.py routing`` — and names the config key that turns each row off or moves it.

Help-safe by construction: every value comes from :func:`config.load_config` plus one
``shutil.which``; never a subprocess, a network call or an LLM call. So the same table is
embedded in ``ccc --help``, and :func:`render` swallows any config error and degrades to a
one-line note rather than crashing ``-h`` on a hand-edited config.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

# The label used wherever a row bills the OpenAI Codex / ChatGPT seat. Kept as one
# constant because :func:`codex_spenders` matches on it — that is the whole point of the
# table for a user who wants the Codex window reserved for adversarial plan debate.
CODEX_COST = "Codex/ChatGPT seat"

# Longest a rendered cell may get before it is elided. Keeps the table inside a normal
# terminal even when `llm_custom_command` is a long shell one-liner.
_MAX_CELL = 46


@dataclass(frozen=True)
class Row:
    """One ccc LLM action, fully resolved against the current config."""

    action: str  # the ccc command / trigger a user would recognise
    purpose: str  # the CCC_LLM_PURPOSE label exported to the subprocess
    provider: str  # provider + model, as one "tool → model" cell
    cost: str  # whose quota this bills
    switch: str  # the config key that turns it off or moves it
    enabled: bool  # whether the feature is on at all right now


def _elide(text: str, limit: int = _MAX_CELL) -> str:
    """Collapse *text* to one line of at most *limit* chars (``…`` when cut)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _router_cost(command: str) -> str:
    """Name whose quota an ``llm_custom_command`` spends, as far as we can tell.

    We cannot resolve another tool's routing from here, so the honest answer points at
    that tool's own overview when we recognise it (``ai.py``) and stays vague otherwise.
    """
    head = command.strip().split()[0] if command.strip() else ""
    base = head.rsplit("/", maxsplit=1)[-1]
    if base in {"ai", "ai.py"}:
        return "whatever `ai routing` says"
    return "external router"


def _dispatch_route(cfg: Config, model: str) -> tuple[str, str]:
    """``(provider_cell, cost)`` for a call that goes through :func:`llm.run_model`.

    Mirrors :func:`llm._dispatch`: a non-empty ``llm_custom_command`` takes the call and
    only a failure inside it falls back to the pinned headless ``claude -p``.
    """
    command = cfg.llm_custom_command.strip()
    if command:
        return (f"llm_custom_command → {_elide(command, 28)}", _router_cost(command))
    account = cfg.llm_account or "default"
    return (f"claude -p → {model or '(claude default)'}", f"Claude subscription ({account})")


def _rung(name: str, cfg: Config) -> tuple[str, str]:
    """``(provider_cell, cost)`` for one ``score_backends`` rung name."""
    if name == "claude":
        return _dispatch_route(cfg, cfg.score_model or cfg.llm_model)
    if name == "codex":
        return ("codex exec → (codex default)", CODEX_COST)
    if name == "copilot":
        return (f"opencode → {cfg.copilot_model or '(default)'}", "Copilot/EPFL seat")
    if name == "gemini":
        return (f"gemini → {cfg.gemini_model or '(default)'}", "Gemini quota")
    if name == "custom":
        command = cfg.score_custom_command.strip()
        return (
            f"score_custom_command → {_elide(command, 24)}" if command else "custom (unset)",
            _router_cost(command) if command else "nothing — rung is unset",
        )
    return (f"unknown rung {name!r}", "skipped at run time")


def _short_aim_route(cfg: Config) -> tuple[str, str]:
    """``(provider_cell, cost)`` for the short-AIM label generator.

    ``auto`` resolves exactly as :func:`short_aim.resolve_backend` does — codex when the
    CLI is on ``PATH``, else claude — so the table shows what would REALLY run, not the
    literal config word.
    """
    backend = cfg.short_aim_backend
    resolved = ("codex" if shutil.which("codex") else "claude") if backend == "auto" else backend
    suffix = " (auto)" if backend == "auto" else ""
    if resolved == "claude":
        provider, cost = _dispatch_route(cfg, cfg.short_aim_model)
        return (provider + suffix, cost)
    return (f"codex exec → {cfg.short_aim_model or '(codex default)'}{suffix}", CODEX_COST)


def rows(cfg: Config | None = None) -> list[Row]:
    """Every ccc LLM action, resolved against *cfg* (loaded when omitted)."""
    if cfg is None:
        from .config import load_config  # lazy: keep module import light

        cfg = load_config()

    ladder = [_rung(name, cfg) for name in cfg.score_backends] or [("(no rungs configured)", "—")]
    score_provider = "  →  ".join(cell for cell, _ in ladder)
    score_cost = " → ".join(dict.fromkeys(cost for _, cost in ladder))
    aim_met = _dispatch_route(cfg, cfg.assess_aim_model or cfg.llm_model)
    drift = _dispatch_route(cfg, cfg.drift_model or cfg.llm_model)
    cheap = _dispatch_route(cfg, cfg.llm_model)
    short = _short_aim_route(cfg)

    return [
        Row(
            "score-aim (on /aim set + on turn)",
            "aim-score",
            score_provider,
            score_cost,
            "score_backends · score_model · aim_score_on_set · assess_aim_on_turn",
            cfg.aim_score_on_set or cfg.assess_aim_on_turn,
        ),
        Row(
            "assess-aim (is the AIM met?)",
            "aim-met",
            aim_met[0],
            aim_met[1],
            "assess_aim_on_turn · assess_aim_model",
            cfg.assess_aim_on_turn,
        ),
        Row(
            "check-drift (sub-goals vs AIM)",
            "subgoal-drift",
            drift[0],
            drift[1],
            "drift_check · drift_model",
            cfg.drift_check,
        ),
        Row(
            "autoprogress (derive sub-goals)",
            "subgoal-derive",
            cheap[0],
            cheap[1],
            "autoprogress · llm_model",
            cfg.autoprogress,
        ),
        Row(
            "autoprogress (grade sub-goals)",
            "subgoal-grade",
            cheap[0],
            cheap[1],
            "grade_on_turn · llm_model",
            cfg.grade_on_turn,
        ),
        Row(
            "daemon summary + next step",
            "summary-nextstep",
            cheap[0],
            cheap[1],
            "summarize · llm_model",
            cfg.summarize,
        ),
        Row(
            "short-AIM label (/aim column)",
            "short-aim",
            short[0],
            short[1],
            "short_aim · short_aim_backend · short_aim_model",
            cfg.short_aim,
        ),
    ]


def codex_spenders(all_rows: list[Row]) -> list[Row]:
    """The ENABLED rows that bill the Codex seat — empty is the desirable state."""
    return [r for r in all_rows if r.enabled and CODEX_COST in r.cost]


def _table(headers: tuple[str, ...], body: Sequence[tuple[str, ...]]) -> list[str]:
    """Render a box-drawn table, columns auto-sized to content."""
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in body)) if body else len(headers[i])
        for i in range(len(headers))
    ]

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def line(cells: tuple[str, ...]) -> str:
        return "│ " + " │ ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)) + " │"

    out = [rule("┌", "┬", "┐"), line(headers), rule("├", "┼", "┤")]
    for row in body:
        out.append(line(row))
    out.append(rule("└", "┴", "┘"))
    return out


def render(cfg: Config | None = None) -> str:
    """The whole overview as plain text. Never raises — safe inside ``--help``."""
    try:
        all_rows = rows(cfg)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # A broken/hand-edited config must degrade the table, never break `ccc -h`.
        return f"LLM routing: unavailable ({exc.__class__.__name__}) — check config.toml\n"

    body = [
        (
            r.action,
            r.purpose,
            r.provider,
            r.cost,
            "on" if r.enabled else "OFF",
        )
        for r in all_rows
    ]
    lines = [
        "ccc's own LLM calls — which model each action uses now (`ccc llm-routing`):",
        *_table(("ccc action", "purpose", "provider → model now", "bills", ""), body),
    ]

    spenders = codex_spenders(all_rows)
    if spenders:
        names = ", ".join(r.purpose for r in spenders)
        lines += [
            f"⚠ Spending the Codex seat: {names}. Every `codex exec` re-sends ~17.6k tokens of",
            "  AGENTS.md + plugin catalogue before your prompt, so a ten-word label costs a full",
            "  prompt. To keep the Codex window for /codex-debate, set"
            ' short_aim_backend = "claude".',
        ]
    else:
        lines.append("✓ No ccc action bills the Codex seat — it is free for /codex-debate.")

    lines += [
        "",
        "Change any row in ~/.claude/command-center/config.toml (keys per action):",
    ]
    lines += [f"  {r.purpose:<17} {r.switch}" for r in all_rows]
    lines += [
        "  llm_custom_command  routes EVERY claude-backed row above through one external",
        "                      command (purpose in $CCC_LLM_PURPOSE) — the escape hatch off",
        "                      the Claude subscription; empty = pinned `claude -p`.",
        "  llm_account         which Claude seat the pinned `claude -p` bills.",
    ]
    return "\n".join(lines) + "\n"
