#!/usr/bin/env python3
"""Best-effort summary + next-step generation for parked sessions.

The backend is pluggable; the default shells out to headless ``claude -p`` (free
with the user's subscription, no API key needed). It NEVER raises — any failure
returns ``(None, None)`` so the daemon degrades gracefully and simply keeps the
previous summary / next step.

Recursion guard: the subprocess runs with ``CCC_INTERNAL=1`` (so ``cc-hook.sh``
skips and we don't create junk session rows) and ``AI_NO_AUTOCOMMIT=1`` (so the
auto-commit Stop hook doesn't fire).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import codex_launch

if TYPE_CHECKING:
    from .config import Config

_LOG = logging.getLogger(__name__)

# Bounded wall-clock for one score-ladder rung. Deliberately ABOVE ai.py's inner
# DEFAULT_PROMPT_TIMEOUT (150s): ai.py must time out first so it can log + classify
# the failure in `ai logs`; an outer kill at the same instant would SIGKILL it
# mid-log and the row would vanish.
_LADDER_TIMEOUT_SEC = 180

# The rungs allowed in ``config.score_backends``. Validated at USE time (run_ladder), not
# at config load, so an unknown rung degrades to a skip+warning instead of a load error.
LADDER_BACKENDS: tuple[str, ...] = ("copilot", "gemini", "codex", "claude", "custom")

_PROMPT = """You are summarizing a parked AI coding session for a dashboard.
Session goal (done when): {aim}

Recent transcript (oldest first, most recent last):
{tail}

Reply with STRICT minified JSON and nothing else:
{{"summary":"<= one sentence on the current state","next_step":"1-3 short lines, \
each starting with '- ', the concrete next actions toward the goal"}}"""


def concise_note(text: str | None, limit: int = 160) -> str:
    """Collapse *text* to one <=limit-char line for the ``CCC_LLM_NOTE`` label.

    The note rides into every headless LLM subprocess's environment so an external
    router (``llm_custom_command`` / the ``custom`` score rung) can log which
    session/goal a call served; it must be a single short line (no embedded newlines,
    bounded length). Returns ``""`` for falsy input.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _block_text(content: object) -> str:
    """Flatten a Claude message ``content`` (str, or list of typed blocks) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            parts.append(f"[tool:{block.get('name', '')}]")
        # thinking / tool_result blocks are intentionally skipped (noise / bulk)
    return " ".join(p for p in parts if p)


def _lines_to_turns(lines: list[str]) -> list[str]:
    """Parse transcript JSONL *lines* to ``[role] text`` snippets (user/assistant only)."""
    out: list[str] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("type") not in ("user", "assistant"):
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role", rec["type"])
        snippet = _block_text(message.get("content")).strip()
        if snippet:
            out.append(f"[{role}] {snippet[:400]}")
    return out


def _read_transcript_tail(path: Path, max_chars: int = 6000) -> str:
    """Extract a compact, human-readable tail of a session transcript JSONL.

    Claude Code transcript records carry text under ``message.content``: a plain
    string for user turns, a list of typed blocks for assistant turns.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(_lines_to_turns(text.splitlines()[-200:]))[-max_chars:]


def read_transcript_delta(path: Path, offset: int, max_chars: int = 8000) -> tuple[str, int]:
    """Return ``(delta_text, new_offset)`` for transcript content added since *offset*.

    Reads from byte *offset* to end-of-file (transcripts are append-only JSONL),
    keeps only user / final-assistant text (no tool_use/tool_result/thinking),
    and advances the offset to the current file size so the next pass reads only
    what is genuinely new. A truncated/rotated file (size < offset) resets to 0.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ("", offset)
    start = 0 if size < offset else offset
    try:
        with path.open("rb") as handle:
            mid_line = False
            if start > 0:
                handle.seek(start - 1)
                # If the byte before `start` isn't a newline we landed mid-line.
                mid_line = handle.read(1) != b"\n"
            else:
                handle.seek(0)
            raw = handle.read()
    except OSError:
        return ("", offset)
    text = raw.decode("utf-8", errors="replace")
    # Drop a leading partial line only if we actually seeked into the middle of one.
    if mid_line and "\n" in text:
        text = text.split("\n", 1)[1]
    delta = "\n".join(_lines_to_turns(text.splitlines()))[-max_chars:]
    return (delta, size)


def summarize(
    aim: str | None, transcript_path: Path | None, model: str, *, note: str = ""
) -> tuple[str | None, str | None]:
    """Return ``(summary, next_step)`` for a session, or ``(None, None)`` on failure.

    *note* is the session's first/original AIM (already made concise) — exported as
    ``CCC_LLM_NOTE`` so an external router can log which session the call served.
    """
    tail = _read_transcript_tail(transcript_path) if transcript_path else ""
    if not tail:
        return (None, None)
    raw = _dispatch(
        _PROMPT.format(aim=aim or "(none set)", tail=tail),
        model,
        purpose="summary-nextstep",
        note=note,
    )
    return _parse(raw) if raw else (None, None)


def run_model(prompt: str, model: str, *, purpose: str = "", note: str = "") -> str | None:
    """Public, never-raising headless LLM call. Returns stdout or ``None``.

    Shared by :mod:`command_center.autoprogress` so it inherits the same recursion
    guard (``CCC_INTERNAL=1``), no-autocommit guard, and timeout as summaries.

    *purpose* is the per-action label (``aim-score`` / ``aim-met`` / ``subgoal-drift`` /
    ``subgoal-derive`` / ``subgoal-grade`` / ``summary-nextstep`` / ``short-aim``) and
    *note* is the session's first AIM — both are exported to the subprocess env as
    ``CCC_LLM_PURPOSE`` / ``CCC_LLM_NOTE`` so a configured ``llm_custom_command``
    router can log or route each call per action. They are metadata only: they never
    change what is generated.
    """
    return _dispatch(prompt, model, purpose=purpose, note=note)


def _dispatch(prompt: str, model: str, purpose: str = "", note: str = "") -> str | None:
    """Route a headless call through ``llm_custom_command``, or fall back to ``claude -p``.

    ccc's own headless calls (score-aim / drift / aim-met / summaries / short-aim)
    should be routable OFF the user's Claude account: when ``llm_custom_command`` is
    configured it runs with the full prompt on stdin and ``CCC_LLM_PURPOSE`` /
    ``CCC_LLM_NOTE`` in its env (the same contract as the ``custom`` score rung), so a
    user can point every call at their own multi-provider router without ccc depending
    on any private tool. An empty command — the default — or a failed run degrades to
    the pinned headless ``claude -p`` (:func:`_run_claude`).
    """
    from .config import load_config  # lazy: keep module import light

    command = load_config().llm_custom_command
    if command.strip():
        out = run_custom(prompt, command, purpose=purpose, note=note)
        if out is not None:
            return out
    return _run_claude(prompt, model, purpose=purpose, note=note)


def run_codex(prompt: str, model: str = "") -> str | None:
    """Headless ``codex exec`` text generation. Returns the final message, or ``None``.

    Shells out to OpenAI's Codex CLI instead of ``claude`` so cheap derived text (the
    short-AIM label) costs Codex/ChatGPT quota, not Claude tokens. The launch goes through
    the one policy in :mod:`command_center.codex_launch`: the named ``hardened-ro``
    permission profile (never ``--sandbox``, which forces the legacy sandbox and drops the
    profile's deny rules), a throwaway empty ``-C`` root so this text call can neither be
    confused with — nor write into — a repo, ``--ephemeral`` so it leaves no session file,
    and one ``-c mcp_servers.<name>.enabled=false`` per configured server so no MCP
    handshake can hang it. It deliberately does NOT pass ``--ignore-user-config``: that
    would also drop the permission profiles this run depends on. The final assistant
    message is captured cleanly via ``--output-last-message`` (no event noise). Never
    raises: any failure (no ``codex`` on PATH, a refused launch, non-zero exit, timeout)
    returns ``None`` so callers fall back to the full AIM. Empty *model* lets Codex pick
    its default model.
    """
    # Bill the seat the shared selector picks (pin/holds-aware) instead of whatever
    # CODEX_HOME the ambient environment happens to carry — ccc's own short calls used
    # to silently spend the default (team) seat even while it was pinned away or held.
    try:
        from .codex_in_claude import codex_exec_env  # lazy: keep module import light

        env = codex_exec_env()
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        env = dict(os.environ)
    env["CCC_INTERNAL"] = "1"
    env["AI_NO_AUTOCOMMIT"] = "1"
    codex_home = Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else None
    try:
        codex_bin = codex_launch.resolve_codex()
        perm_args = codex_launch.permission_args(False, codex_home=codex_home)
        mcp_args = codex_launch.mcp_disable_args(codex_home)
    except codex_launch.CodexLaunchError:
        return None
    out_path = ""
    workdir = ""
    try:
        handle, out_path = tempfile.mkstemp(prefix="ccc-codex-", suffix=".txt")
        os.close(handle)
        workdir = tempfile.mkdtemp(prefix="ccc-codex-cwd-")
        cmd = [
            codex_bin,
            "exec",
            *perm_args,
            *mcp_args,
            "--ephemeral",
            "--skip-git-repo-check",
            "-C",
            workdir,
            "--color",
            "never",
            "--output-last-message",
            out_path,
        ]
        if model:
            cmd += ["-m", model]
        cmd.append(prompt)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, env=env, check=False
        )
        if result.returncode != 0:
            return None
        text = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        return text or None
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _guarded_env(purpose: str = "", note: str = "") -> dict[str, str]:
    """Env for a headless helper subprocess (the same guards the claude/codex runners set).

    ``CCC_INTERNAL=1`` so ``cc-hook.sh`` skips (no junk session rows / no recursion) and
    ``AI_NO_AUTOCOMMIT=1`` so the auto-commit Stop hook does not fire. A non-empty
    *purpose* / *note* is exported as ``CCC_LLM_PURPOSE`` / ``CCC_LLM_NOTE`` so an
    external command (``llm_custom_command``, the ``custom`` score rung, or any wrapper
    around the CLIs below) can log or route each call per action.
    """
    env = dict(os.environ)
    env["CCC_INTERNAL"] = "1"
    env["AI_NO_AUTOCOMMIT"] = "1"
    if purpose:
        env["CCC_LLM_PURPOSE"] = purpose
    if note:
        env["CCC_LLM_NOTE"] = note
    return env


def run_copilot(
    prompt: str,
    model: str,
    timeout: int = _LADDER_TIMEOUT_SEC,
    *,
    purpose: str = "",
    note: str = "",
) -> str | None:
    """Headless GitHub Copilot (via opencode) text generation. Returns stdout, or ``None``.

    Shells out to ``opencode run -m github-copilot/<model> <prompt>`` so the score call costs
    the user's Copilot seat rather than Claude tokens. Never raises: no ``opencode`` on PATH,
    an empty *model* (no Copilot slug to form), a non-zero exit, or a timeout all return
    ``None`` so the ladder falls through to the next rung.
    """
    if not model or not shutil.which("opencode"):
        return None
    try:
        result = subprocess.run(
            ["opencode", "run", "-m", f"github-copilot/{model}", prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_guarded_env(purpose, note),
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def run_gemini(
    prompt: str,
    model: str = "",
    timeout: int = _LADDER_TIMEOUT_SEC,
    *,
    purpose: str = "",
    note: str = "",
) -> str | None:
    """Headless Google Gemini (gemini-cli ``-p``) text generation. Returns stdout, or ``None``.

    ``gemini -p <prompt>`` (plus ``-m <model>`` when *model* is set; empty lets the CLI pick its
    own default). Never raises: no ``gemini`` on PATH, a non-zero exit (e.g. an ineligible
    account), or a timeout return ``None`` so the ladder falls through to the next rung.
    """
    if not shutil.which("gemini"):
        return None
    cmd = ["gemini", "-p", prompt]
    if model:
        cmd += ["-m", model]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_guarded_env(purpose, note),
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def run_custom(
    prompt: str,
    command: str,
    timeout: int = _LADDER_TIMEOUT_SEC,
    *,
    purpose: str = "",
    note: str = "",
) -> str | None:
    """User escape-hatch backend: run *command* via the shell, *prompt* on stdin.

    The command must print the model's raw text response on stdout; a non-zero exit (or an
    empty *command*) fails so the caller falls through. This lets a user route the call
    through their own multi-provider router without ccc depending on any private tool.
    *purpose* / *note* ride into the command's env as ``CCC_LLM_PURPOSE`` /
    ``CCC_LLM_NOTE`` — per-action metadata the router can log or route on. Never
    raises. ``shell=True`` is intentional — the command is the user's own configured hook.
    """
    if not command.strip():
        return None
    try:
        result = subprocess.run(
            command,
            shell=True,  # noqa: S602  # deliberate: the command is the user's own escape hatch
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_guarded_env(purpose, note),
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _run_backend(
    name: str, prompt: str, cfg: Config, purpose: str = "", note: str = ""
) -> str | None:
    """Dispatch ONE named score-ladder rung. ``None`` on unavailable/failed/unknown."""
    if name == "claude":
        # Via run_model so a configured llm_custom_command (the universal escape hatch)
        # covers this rung too; without one it is the pinned headless `claude -p`.
        return run_model(prompt, cfg.score_model or cfg.llm_model, purpose=purpose, note=note)
    if name == "codex":
        # codex resolves its own model; its CLI has no label support (labels dropped).
        return run_codex(prompt)
    if name == "copilot":
        return run_copilot(prompt, cfg.copilot_model, purpose=purpose, note=note)
    if name == "gemini":
        return run_gemini(prompt, cfg.gemini_model, purpose=purpose, note=note)
    if name == "custom":
        return run_custom(prompt, cfg.score_custom_command, purpose=purpose, note=note)
    return None


def run_ladder(
    prompt: str,
    cfg: Config,
    backends: list[str] | None = None,
    *,
    purpose: str = "",
    note: str = "",
) -> tuple[str, str] | None:
    """Run *prompt* through the score-backend fallback ladder.

    Tries each rung in *backends* (default :attr:`cfg.score_backends`) in order and returns
    ``(backend_name, raw_text)`` for the FIRST rung that yields non-empty output, else
    ``None``. Unknown rung names are skipped with a stderr warning (validated here, not at
    config load). *purpose* / *note* are exported to every rung's subprocess env as
    ``CCC_LLM_PURPOSE`` / ``CCC_LLM_NOTE`` (metadata for a custom router; the codex rung
    has no label support). Never raises — every runner already degrades to ``None``.
    """
    rungs = cfg.score_backends if backends is None else backends
    for name in rungs:
        if name not in LADDER_BACKENDS:
            print(f"ccc: unknown score backend {name!r} — skipping", file=sys.stderr)
            continue
        raw = _run_backend(name, prompt, cfg, purpose, note)
        if raw and raw.strip():
            _LOG.debug("score ladder: %s served", name)
            return (name, raw)
        _LOG.debug("score ladder: %s failed (no output)", name)
    return None


def _run_claude(prompt: str, model: str, *, purpose: str = "", note: str = "") -> str | None:
    """Headless ``claude -p`` — env PINNED to ``cfg.llm_account`` (never ambient).

    Pinning the account means a call made from a session running under a different
    Claude account still bills the configured ``llm_account`` (default: the default
    account), not whatever ``CLAUDE_CONFIG_DIR`` the parent happened to export. On a
    single-account setup this is a no-op (the pin just unsets the var).
    """
    if not shutil.which("claude"):
        return None
    from . import accounts, config

    llm_config_dir = accounts.account_config_dir(config.load_config().llm_account)
    env = accounts.launch_env(llm_config_dir, base=_guarded_env(purpose, note))
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _parse(raw: str) -> tuple[str | None, str | None]:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return (None, None)
    try:
        data = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return (None, None)
    summary = data.get("summary")
    next_step = data.get("next_step")
    return (
        summary if isinstance(summary, str) and summary.strip() else None,
        next_step if isinstance(next_step, str) and next_step.strip() else None,
    )
