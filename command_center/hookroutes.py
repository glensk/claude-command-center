#!/usr/bin/env python3
"""Which FOREIGN hook commands also reach ``ccc hook`` — one answer, two callers.

Claude Code runs every entry wired on an event, and the installer preserves foreign
hooks by contract (:mod:`command_center.install`): a hand-wired forwarder script whose
body is ``ccc hook "$1"`` therefore sits happily next to ccc's own entries and every
event it is wired on runs ccc **twice**. This module is the single place that answers
"does this foreign command ALSO spawn ``ccc hook``?" — read by the ``install-hooks``
preflight (which refuses) and by ``ccc doctor`` (which reports it).

Analysis is text-based but bounded and read-only: each command is tokenized with
:mod:`shlex`, one explicit indirection into a script under ``$HOME`` is followed, and a
wrapper of the shape ``run.sh "label" -- <inner command>`` (whose body execs ``"$@"``,
so the wrapper itself mentions nothing) contributes its inner command as ONE extra
piece. Anything else — a path outside ``$HOME``, a command a shell builds at runtime —
is reported as *indeterminate* rather than guessed at, and never refuses an install.

Deliberately imports neither :mod:`command_center.install` nor
:mod:`command_center.doctor`: the caller passes the commands and the ownership
predicate, so there is no cycle in either direction.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import re
import shlex
from collections.abc import Callable, Iterable
from pathlib import Path

#: Shell interpreters whose first non-option argument is the script they run.
_SHELL_RUNNERS = frozenset({"bash", "sh", "zsh"})
#: Tokens that mean "the ccc binary" in a hand-written or generated command.
_CCC_BIN_REFS = frozenset({"ccc", "$CCC_BIN", "${CCC_BIN}"})
#: What a ccc subcommand looks like (anything else after `ccc` is not one we can name).
_SUBCOMMAND = re.compile(r"^[a-z][a-z-]*$")
#: A script token no static reader can resolve to a file (expansion, glob).
_DYNAMIC = re.compile(r"[$`*?]")


def _is_ccc_ref(token: str) -> bool:
    """Whether *token* refers to the ccc binary (a name, a path to it, or $CCC_BIN)."""
    return token in _CCC_BIN_REFS or token.endswith("/ccc")


def _shell_lines(text: str) -> tuple[list[list[str]], bool]:
    """*text*'s runnable lines, tokenized, plus "a line could not be tokenized".

    Line-based and shell-aware rather than a regex: comment lines are skipped (a
    `# ccc install-statusline` in a header is documentation, not a spawn) and each
    remaining line is tokenized with :mod:`shlex`, so quoting is honoured.
    """
    lines: list[list[str]] = []
    unparsable = False
    for raw in text.splitlines():
        line = raw.lstrip()
        if not line or line.startswith("#"):
            continue
        try:
            lines.append(shlex.split(line, comments=True))
        except ValueError:  # unbalanced quotes: this line stays unknown
            unparsable = True
    return lines, unparsable


def _ccc_ref_positions(text: str) -> list[tuple[list[str], int]]:
    """Every ``(tokens, index)`` in *text* where a token refers to the ccc binary."""
    lines, _unparsable = _shell_lines(text)
    return [
        (tokens, index)
        for tokens in lines
        for index, token in enumerate(tokens)
        if _is_ccc_ref(token)
    ]


def _ccc_calls(text: str) -> tuple[list[str], bool]:
    """The ``ccc <subcommand>`` calls in *text*, plus "a call site was indeterminate".

    A ccc reference whose next token is not a literal subcommand (``ccc "$cmd"``, a
    flag, end of line) is reported as indeterminate instead of guessed at — as is a line
    :func:`_shell_lines` could not tokenize at all.
    """
    lines, indeterminate = _shell_lines(text)
    names: list[str] = []
    for tokens in lines:
        for index, token in enumerate(tokens):
            if not _is_ccc_ref(token):
                continue
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            if _SUBCOMMAND.match(following):
                names.append(following)
            else:
                indeterminate = True
    return names, indeterminate


def _unnamed_ccc_ref(text: str) -> bool:
    """Does *text* invoke ccc without naming the subcommand (``ccc "$1"``)?

    Narrower than :func:`_ccc_calls`' indeterminate flag on purpose: a line no shell
    tokenizer can read (an apostrophe in a Python docstring) says nothing about ccc, and
    counting it would leave every hook script written in Python "unresolved".
    """
    return any(
        not _SUBCOMMAND.match(tokens[index + 1] if index + 1 < len(tokens) else "")
        for tokens, index in _ccc_ref_positions(text)
    )


def _script_candidate(command: str) -> tuple[str, bool]:
    """The ONE script token *command* would run, plus "the command is unparsable".

    The token only — no filesystem question is asked here, so both readers of it
    (:func:`_script_behind`, which opens it, and :func:`_hides_a_call`, which judges how
    suspicious an unresolved one is) start from the same single indirection rule.
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return "", True
    if not tokens:
        return "", False
    head = tokens[0]
    if Path(head).name in _SHELL_RUNNERS:
        arguments = [t for t in tokens[1:] if not t.startswith("-")]
        return (arguments[0] if arguments else ""), False
    if not _is_ccc_ref(head) and ("/" in head or head.startswith("~")):
        return head, False
    return "", False


def _script_behind(command: str) -> tuple[Path | None, bool]:
    """The ONE script *command* runs and may be read, plus "resolution failed".

    Follows a single explicit indirection — ``bash|sh|zsh <path> …`` or a bare
    executable path — and only inside ``$HOME``: this is a read-only probe, not a
    crawler, so a script elsewhere (or an unreadable one) is reported as indeterminate
    rather than opened or guessed at.
    """
    candidate, unparsable = _script_candidate(command)
    if unparsable:
        return None, True
    if not candidate:
        return None, False
    path = Path(candidate).expanduser()
    try:
        inside_home = path.resolve().is_relative_to(Path.home().resolve())
    except OSError:
        return None, True
    return (path, False) if inside_home else (None, True)


def _foreign_ccc_calls(command: str) -> tuple[list[str], bool]:
    """``ccc`` calls in a foreign *command* and in the one script it may run."""
    names, indeterminate = _ccc_calls(command)
    script, unresolved = _script_behind(command)
    indeterminate = indeterminate or unresolved
    if script is not None:
        try:
            body = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return names, True
        more, more_indeterminate = _ccc_calls(body)
        names.extend(more)
        indeterminate = indeterminate or more_indeterminate
    return names, indeterminate


# --------------------------------------------------------------------------- #
# foreign routes to `ccc hook`
# --------------------------------------------------------------------------- #
def _hides_a_call(command: str) -> bool:
    """Whether what could NOT be read in *command* might itself spawn ``ccc``.

    :func:`_script_behind` declines to open anything outside ``$HOME`` and cannot know a
    path a shell builds at runtime; both come back as "unresolved", but they are not
    equally suspicious. A literal path that does not exist runs nothing at all (a plain
    ``/my/commit.sh`` hook is not a mystery worth reporting), while a dynamic construct
    (``bash -c "$X"``) or a real file this process may not read could hold anything.
    """
    candidate, unparsable = _script_candidate(command)
    if unparsable:
        return True
    if not candidate:
        return False
    if _DYNAMIC.search(candidate):
        return True
    try:
        return Path(candidate).expanduser().exists()
    except (OSError, ValueError):
        return True


def _display_name(command: str) -> str:
    """How *command* is named in a report: its script's basename, else its first token."""
    script, _unresolved = _script_behind(command)
    if script is not None:
        return script.name
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        tokens = command.split()
    return Path(tokens[0]).name if tokens else command.strip()


def _command_pieces(command: str) -> list[str]:
    """*command*, plus the inner command of a ``… -- <inner>`` wrapper (ONE level).

    A wrapper that execs ``"$@"`` (``run-stop-hook.sh "label" -- /path/cc-hook.sh
    stop``) says nothing about the hook it runs: its own body is generic, and the real
    command sits behind the ``--`` separator. That inner command is analysed as a
    command in its own right — one bounded extra piece, never recursion.
    """
    pieces = [command]
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return pieces
    if "--" in tokens:
        inner = tokens[tokens.index("--") + 1 :]
        if inner:
            pieces.append(shlex.join(inner))
    return pieces


def _piece_texts(piece: str) -> tuple[list[str], bool]:
    """The texts to read for *piece* (itself + its one script), plus "something is unread"."""
    texts = [piece]
    script, unresolved = _script_behind(piece)
    if script is None:
        return texts, unresolved and _hides_a_call(piece)
    try:
        texts.append(script.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return texts, True
    return texts, False


def _piece_route(piece: str) -> tuple[bool, bool]:
    """(*piece* spawns ``ccc hook``, *piece* might spawn one unseen).

    The second flag is deliberately narrower than :func:`_foreign_ccc_calls`'
    "indeterminate": it takes an unnamed ccc reference (``ccc "$1"``) or a script that
    could not be opened at all, and nothing else. A hook whose command and script
    mention ccc nowhere cannot be a second path to ``ccc hook``, however little of its
    source this reader could tokenize.
    """
    texts, unread = _piece_texts(piece)
    if any("hook" in _ccc_calls(text)[0] for text in texts):
        return True, False
    return False, unread or any(_unnamed_ccc_ref(text) for text in texts)


def foreign_hook_routes(
    commands: Iterable[str], owned: Callable[[str], bool]
) -> tuple[list[str], list[str]]:
    """Foreign hook *commands* that also reach ``ccc hook`` — ``(offenders, unknown)``.

    *owned* is the caller's "this entry is ccc's own" predicate
    (``install._is_ccc_hook_command``); every command it accepts is skipped, since ccc's
    own wiring is not a duplicate path to itself. Both returned lists hold DISPLAY names
    (see :func:`_display_name`), deduped in first-seen order: *offenders* are the
    commands proven to spawn ``ccc hook`` (each event they are wired on runs ccc twice),
    *unknown* the ones whose route could not be resolved — informational only, they must
    never refuse an install.
    """
    offenders: dict[str, None] = {}
    unknown: dict[str, None] = {}
    for command in commands:
        if not command.strip() or owned(command):
            continue
        hits: list[str] = []
        indeterminate = False
        for piece in _command_pieces(command):
            spawns, maybe = _piece_route(piece)
            if spawns:
                hits.append(_display_name(piece))
            indeterminate = indeterminate or maybe
        if hits:
            offenders.update(dict.fromkeys(hits))
        elif indeterminate:
            unknown.setdefault(_display_name(command), None)
    return list(offenders), list(unknown)
