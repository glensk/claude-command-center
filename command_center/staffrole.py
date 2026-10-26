#!/usr/bin/env python3
"""Staff roles: the agent a session adopted and that agent's symbol (tp#1166, tp#1288).

A Claude session that ran ``staff adopt -a <agent>`` works in that llm-staff ROLE. tp records
the binding as ``$TP_CACHE_DIR/session-role/<sid>`` (default ``~/.cache/tp/session-role``),
one line holding the agent name. Each agent's ``agent.yaml`` (``<staff root>/<agent>/``,
staff root ``$TP_STAFF_ROOT`` else ``~/obsidian/01-llm/staff`` — tp's rule) may carry a
``symbol:`` — one emoji that stands for the agent. ccc shows it instead of the per-tab colour
badge on an adopted session's iTerm tab and command-center row, and after the ``🎭`` on its
status-line ``/aim`` row.

Every function here runs on hot paths (status line every second, every TUI row), so they do a
few small reads and stats, never a subprocess or a YAML import, and never raise.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import os
import re

#: tp's ``owner_role`` shape: the only agent names a binding may carry.
_ROLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
#: The top-level ``symbol:`` line of an agent.yaml (quoted or bare, optional comment).
_SYMBOL_LINE_RE = re.compile(r"""^symbol:[ \t]*(?:"([^"\n]*)"|'([^'\n]*)'|([^#\s]+))""", re.M)
#: VS16, ZWJ and the skin-tone modifiers make a glyph wider than its two terminal cells.
_WIDENERS = frozenset({0xFE0F, 0x200D, *range(0x1F3FB, 0x1F400)})

_symbol_cache: dict[str, tuple[int, str | None]] = {}

#: The status-line `/aim` marker of a session that adopted a staff role (tp#1166); the
#: agent's own symbol follows it (`🎭📧`, tp#1288).
ROLE_MARKER = "🎭"


def staff_root() -> str:
    """The llm-staff home: ``$TP_STAFF_ROOT``, else ``~/obsidian/01-llm/staff`` (tp's rule)."""
    root = os.environ.get("TP_STAFF_ROOT") or os.path.join(
        os.path.expanduser("~"), "obsidian", "01-llm", "staff"
    )
    return os.path.expanduser(root)


def _agent_yaml(name: str) -> str:
    return os.path.join(staff_root(), name, "agent.yaml")


def _role_dir() -> str:
    cache = os.environ.get("TP_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "tp")
    return os.path.join(os.path.expanduser(cache), "session-role")


def has_bindings() -> bool:
    """True when tp holds at least one session-role binding (one ``scandir``)."""
    try:
        with os.scandir(_role_dir()) as entries:
            return any(not e.name.startswith(".") for e in entries)
    except OSError:
        return False


def adopted_role(session_id: str) -> str | None:
    """The staff role `tp adopt` bound `session_id` to, or None (tp#1166).

    The binding is tp's own record of a real adopt (a dry run writes none):
    ``$TP_CACHE_DIR/session-role/<sid>`` (default ``~/.cache/tp/session-role/<sid>``), one
    line holding the agent name. The name must have tp's ``owner_role`` shape and name an
    agent that still exists (``<staff root>/<name>/agent.yaml``). One small read and one
    stat, no subprocess: it runs on every status-line render. Never raises.
    """
    if not session_id or "/" in session_id or session_id.startswith("."):
        return None
    path = os.path.join(_role_dir(), session_id)
    try:
        with open(path, encoding="utf-8") as handle:
            name = handle.readline(128).strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not _ROLE_NAME_RE.match(name):
        return None
    if not os.path.isfile(_agent_yaml(name)):
        return None
    return name


def valid_symbol(value: str) -> bool:
    """True for ONE non-ASCII codepoint that is not a width modifier (VS16, ZWJ, skin tone)."""
    return len(value) == 1 and ord(value) > 0x7F and ord(value) not in _WIDENERS


def _parse_symbol(text: str) -> str | None:
    match = _SYMBOL_LINE_RE.search(text)
    if not match:
        return None
    value = next((g for g in match.groups() if g is not None), "").strip()
    return value if valid_symbol(value) else None


def agent_symbol(name: str | None) -> str | None:
    """The ``symbol:`` of agent *name*'s agent.yaml, or None (none or malformed).

    Cached per file mtime. ``tabsymbol.role_symbol`` additionally refuses a symbol equal to
    a per-tab badge, which would make an adopted tab look like an ordinary one.
    """
    if not name or not _ROLE_NAME_RE.match(name):
        return None
    path = _agent_yaml(name)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    cached = _symbol_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as handle:
            symbol = _parse_symbol(handle.read(65536))
    except (OSError, UnicodeDecodeError):
        symbol = None
    _symbol_cache[path] = (mtime, symbol)
    return symbol


def session_symbol(session_id: str | None) -> str | None:
    """The symbol of the agent *session_id* adopted, or None (not adopted / no symbol)."""
    if not session_id:
        return None
    return agent_symbol(adopted_role(session_id))


def agent_symbols() -> frozenset[str]:
    """Every agent's symbol (the dirs under the staff root that hold an agent.yaml)."""
    try:
        names = os.listdir(staff_root())
    except OSError:
        return frozenset()
    found = (agent_symbol(name) for name in names if not name.startswith(("_", ".")))
    return frozenset(s for s in found if s)


def role_marker(session_id: str) -> str:
    """``🎭<agent symbol>`` for an adopted session (bare ``🎭`` without a symbol), else ``""``."""
    role = adopted_role(session_id)
    if not role:
        return ""
    return ROLE_MARKER + (agent_symbol(role) or "")
