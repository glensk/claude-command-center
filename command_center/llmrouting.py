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
    cfg: Config, model: str, purpose: str, routes: dict[str, tuple[str, str, str]]
) -> tuple[str, str, str]:
    """``(provider_cell, cost, ladder)`` for a call that goes through ``llm.run_model``.

    Mirrors :func:`llm._dispatch`: a non-empty ``llm_custom_command`` takes the call and
    only a failure inside it falls back to the pinned headless ``claude -p``.
    """
    command = cfg.llm_custom_command.strip()
    if not command:
        account = cfg.llm_account or "default"
        return (
            f"claude -p → {model or '(claude default)'}",
            f"Claude subscription ({account})",
            "",
        )
    if resolved := routes.get(purpose):
        first, cost, ladder = resolved
        return (f"ai.py → {first}", cost, ladder)
    if _ai_binary(command):
        return (f"ai.py → {_elide(command, 24)}", "ai.py (route query failed)", "")
    return (f"llm_custom_command → {_elide(command, 28)}", "external router", "")


def _codex_seat_label() -> str:
    """Which Codex seat ccc's own codex calls would bill right now (selector-resolved).

    ``llm.run_codex`` routes through :func:`codex_in_claude.codex_exec_env`, so showing
    a hardcoded "codex default" here could lie about the billed account whenever a pin
    or hold is active. Display-only: any failure degrades to the old wording.
    """
    try:
        from . import codex_in_claude, quota

        home = codex_in_claude._codex_home()  # noqa: SLF001
        for label, path in quota._canonical_codex_homes().items():  # noqa: SLF001
            if path.expanduser().resolve() == home.expanduser().resolve():
                return label
        return str(home)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return "codex default"


def _rung(
    name: str, cfg: Config, purpose: str, routes: dict[str, tuple[str, str, str]]
) -> tuple[str, str, str]:
    """``(provider_cell, cost, ladder)`` for one ``score_backends`` rung name."""
    if name == "claude":
        return _dispatch_route(cfg, cfg.score_model or cfg.llm_model, purpose, routes)
    if name == "codex":
        return (f"codex exec → ({_codex_seat_label()} seat)", CODEX_COST, "")
    if name == "copilot":
        return (f"opencode → {cfg.copilot_model or '(default)'}", COPILOT_COST, "")
    if name == "gemini":
        return (f"gemini → {cfg.gemini_model or '(default)'}", "Gemini quota", "")
    if name == "custom":
        command = cfg.score_custom_command.strip()
        return (
            f"score_custom_command → {_elide(command, 24)}" if command else "custom (unset)",
            "external router" if command else "nothing — rung is unset",
            "",
        )
    return (f"unknown rung {name!r}", "skipped at run time", "")


def _short_aim_route(cfg: Config, routes: dict[str, tuple[str, str, str]]) -> tuple[str, str, str]:
    """``(provider_cell, cost, ladder)`` for the short-AIM label generator.

    ``auto`` resolves exactly as :func:`short_aim.resolve_backend` does — codex when the
    CLI is on ``PATH``, else claude — so the table shows what would REALLY run, not the
    literal config word.
    """
    backend = cfg.short_aim_backend
    resolved = ("codex" if shutil.which("codex") else "claude") if backend == "auto" else backend
    suffix = " (auto)" if backend == "auto" else ""
    if resolved == "claude":
        cell, cost, ladder = _dispatch_route(cfg, cfg.short_aim_model, "short-aim", routes)
        return (cell + suffix, cost, ladder)
    return (f"codex exec → {cfg.short_aim_model or '(codex default)'}{suffix}", CODEX_COST, "")


def _score_row(cfg: Config, routes: dict[str, tuple[str, str, str]]) -> tuple[str, str, str]:
    """``(provider_cell, cost, ladder)`` for the whole ``score_backends`` ladder."""
    cells = [_rung(name, cfg, "aim-score", routes) for name in cfg.score_backends]
    if not cells:
        return ("(no rungs configured)", "—", "")
    return (
        "  →  ".join(cell for cell, _, _ in cells),
        " → ".join(dict.fromkeys(cost for _, cost, _ in cells)),
        next((lad for _, _, lad in cells if lad), ""),
    )


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

    def dispatch(model: str, purpose: str) -> tuple[str, str, str]:
        return _dispatch_route(cfg, model, purpose, routes)

    specs: list[tuple[str, str, tuple[str, str, str], str, bool]] = [
        (
            "score-aim (on /aim set + on turn)",
            "aim-score",
            _score_row(cfg, routes),
            "score_backends · score_model · aim_score_on_set · assess_aim_on_turn",
            cfg.aim_score_on_set or cfg.assess_aim_on_turn,
        ),
        (
            "assess-aim (is the AIM met?)",
            "aim-met",
            dispatch(cfg.assess_aim_model or cfg.llm_model, "aim-met"),
            "assess_aim_on_turn · assess_aim_model",
            cfg.assess_aim_on_turn,
        ),
        (
            "check-drift (sub-goals vs AIM)",
            "subgoal-drift",
            dispatch(cfg.drift_model or cfg.llm_model, "subgoal-drift"),
            "drift_check · drift_model",
            cfg.drift_check,
        ),
        (
            "autoprogress (derive sub-goals)",
            "subgoal-derive",
            dispatch(cfg.llm_model, "subgoal-derive"),
            "autoprogress · llm_model",
            cfg.autoprogress,
        ),
        (
            "autoprogress (grade sub-goals)",
            "subgoal-grade",
            dispatch(cfg.llm_model, "subgoal-grade"),
            "grade_on_turn · llm_model",
            cfg.grade_on_turn,
        ),
        (
            "daemon summary + next step",
            "summary-nextstep",
            dispatch(cfg.llm_model, "summary-nextstep"),
            "summarize · llm_model",
            cfg.summarize,
        ),
        (
            "short-AIM label (/aim column)",
            "short-aim",
            _short_aim_route(cfg, routes),
            "short_aim · short_aim_backend · short_aim_model",
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
            f"⚠ Spending the Codex seat: {names}. Every `codex exec` re-sends ~17.6k tokens of",
            "  AGENTS.md + plugin catalogue before your prompt, so a ten-word label costs a full",
            "  prompt. To keep the Codex window for /codex-debate, set"
            ' short_aim_backend = "claude".',
        ]
    else:
        lines.append("✓ No ccc action bills the Codex seat — it is free for /codex-debate.")

    lines += ["", "Change any row in ~/.claude/command-center/config.toml (keys per action):"]
    lines += [f"  {r.purpose:<17} {r.switch}" for r in all_rows]
    lines += [
        "  llm_custom_command  routes EVERY claude-backed row above through one external",
        "                      command (purpose in $CCC_LLM_PURPOSE) — the escape hatch off",
        "                      the Claude subscription; empty = pinned `claude -p`.",
        "  llm_account         which Claude seat the pinned `claude -p` bills.",
    ]
    return "\n".join(lines) + "\n"


def _load_cfg() -> Config:
    from .config import load_config  # lazy: keep module import light

    return load_config()
