"""Fakes for the voice-bridge tests (``ccc inspect/send/answer/delivery/events``).

:class:`World` is one live Claude session in one iTerm tab, entirely fake: a registry
entry, a ``ps`` table, the tab's iTerm variables, a JSONL transcript under ``tmp_path``
and a store DB. ``world.deps()`` returns the :class:`BridgeDeps` the commands run with;
``on_text`` / ``on_keys`` hooks let a test play Claude Code (append the records a real
session would write when the bytes arrive). Nothing here touches iTerm or a real store.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from command_center.bridge_target import BridgeDeps
from command_center.models import LiveSession
from command_center.snapshot import PsRow
from command_center.store import Store
from command_center.tab_titles import ItermPane, uuid_of

SID = "00000000-0000-4000-8000-00000000b001"
TAB = "w0t1p0:AAAA-BBBB-CCCC"
PID = 4100
TTY = "/dev/ttys041"

_counter = itertools.count(1)


def _uuid() -> str:
    return f"00000000-0000-4000-8000-{next(_counter):012d}"


def user(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "isSidechain": False,
        "message": {"role": "user", "content": text},
        "uuid": _uuid(),
        "origin": {"kind": "human"},
        **extra,
    }


def enqueue(text: str) -> dict[str, Any]:
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


def dequeue() -> dict[str, Any]:
    return {"type": "queue-operation", "operation": "dequeue"}


def queued(text: str) -> dict[str, Any]:
    return {
        "type": "attachment",
        "isSidechain": False,
        "uuid": _uuid(),
        "attachment": {"type": "queued_command", "prompt": text, "commandMode": "prompt"},
    }


def assistant(text: str = "", *, stop: str | None = "end_turn", msg: str = "") -> dict[str, Any]:
    content = [{"type": "text", "text": text}] if text else []
    return {
        "type": "assistant",
        "isSidechain": False,
        "uuid": _uuid(),
        "message": {
            "id": msg or _uuid(),
            "role": "assistant",
            "content": content,
            "stop_reason": stop,
        },
    }


def tool_use(name: str, tool_id: str, inp: dict[str, Any], msg: str = "") -> dict[str, Any]:
    return {
        "type": "assistant",
        "isSidechain": False,
        "uuid": _uuid(),
        "message": {
            "id": msg or _uuid(),
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}],
            "stop_reason": "tool_use",
        },
    }


def ask(tool_id: str, questions: list[dict[str, Any]], msg: str = "") -> dict[str, Any]:
    return tool_use("AskUserQuestion", tool_id, {"questions": questions}, msg)


def question(text: str, labels: Sequence[str], multi: bool = False) -> dict[str, Any]:
    return {
        "question": text,
        "header": text[:8],
        "multiSelect": multi,
        "options": [{"label": lbl, "description": f"about {lbl}"} for lbl in labels],
    }


def answer(
    tool_id: str, questions: list[dict[str, Any]], answers: dict[str, str], *, error: bool = False
) -> dict[str, Any]:
    return {
        "type": "user",
        "isSidechain": False,
        "uuid": _uuid(),
        "message": {
            "role": "user",
            "content": [
                {"tool_use_id": tool_id, "type": "tool_result", "content": "ok", "is_error": error}
            ],
        },
        "toolUseResult": {"questions": questions, "answers": answers},
    }


def tool_result(tool_id: str) -> dict[str, Any]:
    return {
        "type": "user",
        "isSidechain": False,
        "uuid": _uuid(),
        "message": {
            "role": "user",
            "content": [{"tool_use_id": tool_id, "type": "tool_result", "content": "x"}],
        },
    }


def stop_summary() -> dict[str, Any]:
    return {"type": "system", "subtype": "stop_hook_summary", "uuid": _uuid()}


def turn_duration() -> dict[str, Any]:
    return {"type": "system", "subtype": "turn_duration", "uuid": _uuid()}


def turn_end(text: str = "done") -> list[dict[str, Any]]:
    return [assistant(text), stop_summary(), turn_duration()]


def task_notice() -> dict[str, Any]:
    return enqueue("<task-notification>\n<task-id>b1</task-id>\n</task-notification>")


@dataclass
class World:  # pylint: disable=too-many-instance-attributes  # one fake machine
    """One fake live session + tab + transcript + store."""

    tmp: Path
    status: str = "idle"
    kind: str = "interactive"
    alive: bool = True
    tab_tty: str = TTY
    job_pid: int = PID
    tab_reachable: bool = True
    #: Every iTerm pane ``(uuid, tty)`` besides THE tab (whose tty is ``tab_tty``).
    other_panes: list[tuple[str, str]] = field(default_factory=list)
    #: False: THE tab is not in iTerm's pane list (closed).
    tab_listed: bool = True
    #: True: the transcript seam answers ``None`` while the file does not exist (as the
    #: real adapter does for a session that has not been prompted yet).
    transcript_resolves_missing: bool = False
    registered: bool = True
    #: The ``claude agents --json`` roster (background jobs without a registry entry).
    agents: list[Any] = field(default_factory=list)
    on_text: Callable[[World, str], None] | None = None
    on_keys: Callable[[World, list[str]], None] | None = None
    send_result: tuple[str, str] = ("python-api", "sent")
    keys_result: str = "sent"
    llm_answer: str | None = None
    texts: list[str] = field(default_factory=list)
    keys: list[list[str]] = field(default_factory=list)
    llm_calls: list[str] = field(default_factory=list)
    clock_ms: int = 1_800_000_000_000
    mono: float = 0.0

    def __post_init__(self) -> None:
        self.config_dir = self.tmp / "claude"
        self.transcript = self.config_dir / "projects" / "-repo" / f"{SID}.jsonl"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.write_text("", encoding="utf-8")
        self.db = self.tmp / "state.db"
        with Store(self.db) as store:
            store.ensure(SID, cwd="/repo")
            store.update_fields(SID, iterm_session_id=TAB, config_dir=str(self.config_dir))

    # ------------------------------------------------------------------ transcript

    def write(self, *records: dict[str, Any] | list[dict[str, Any]]) -> None:
        flat: list[dict[str, Any]] = []
        for rec in records:
            flat.extend(rec if isinstance(rec, list) else [rec])
        with self.transcript.open("a", encoding="utf-8") as handle:
            for rec in flat:
                handle.write(json.dumps(rec) + "\n")

    # ------------------------------------------------------------------ fakes

    def live(self) -> LiveSession:
        return LiveSession(
            pid=PID,
            session_id=SID,
            cwd="/repo",
            kind=self.kind,
            raw_status=self.status,
            alive=self.alive,
            config_dir=str(self.config_dir),
        )

    def discover(self) -> list[LiveSession]:
        return [self.live()] if self.registered else []

    def ps(self) -> dict[int, Any]:
        return {
            1: PsRow(0, "", "S", "launchd"),
            PID: PsRow(1, TTY.removeprefix("/dev/"), "S", "claude"),
            PID + 1: PsRow(PID, TTY.removeprefix("/dev/"), "S", "node mcp-server"),
            9999: PsRow(1, TTY.removeprefix("/dev/"), "S", "-zsh"),
        }

    def transcript_for(self, cwd: str, session_id: str, config_dir: str) -> Path | None:
        del cwd, config_dir
        if session_id != SID:
            return None
        if self.transcript_resolves_missing and not self.transcript.is_file():
            return None
        return self.transcript

    def panes(self) -> list[ItermPane] | None:
        if not self.tab_reachable:
            return None
        listed = [ItermPane(uuid_of(TAB), self.tab_tty, "tab")] if self.tab_listed else []
        return listed + [ItermPane(u, t, "other") for u, t in self.other_panes]

    def tab_vars(self, iterm_session_id: str) -> dict[str, str] | None:
        if not self.tab_reachable:
            return None
        assert uuid_of(iterm_session_id) == uuid_of(TAB)
        return {"tty": self.tab_tty, "jobPid": str(self.job_pid)}

    def send_text(self, iterm_session_id: str, text: str) -> tuple[str, str]:
        assert uuid_of(iterm_session_id) == uuid_of(TAB)
        self.texts.append(text)
        if self.on_text is not None:
            self.on_text(self, text)
        return self.send_result

    def send_keys(self, iterm_session_id: str, keys: Sequence[str]) -> str:
        assert uuid_of(iterm_session_id) == uuid_of(TAB)
        self.keys.append(list(keys))
        if self.on_keys is not None:
            self.on_keys(self, list(keys))
        return self.keys_result

    def llm(self, prompt: str, note: str, timeout: float) -> str | None:
        del note, timeout
        self.llm_calls.append(prompt)
        return self.llm_answer

    def sleep(self, seconds: float) -> None:
        self.mono += seconds

    def monotonic(self) -> float:
        return self.mono

    def now_ms(self) -> int:
        return self.clock_ms

    def store(self) -> Store:
        return Store(self.db)

    def deps(self) -> BridgeDeps:
        return BridgeDeps(
            discover=self.discover,
            agents=lambda: list(self.agents),
            transcript=self.transcript_for,
            tab_vars=self.tab_vars,
            ps_table=self.ps,
            panes=self.panes,
            send_text=self.send_text,
            send_keys=self.send_keys,
            store=self.store,
            llm=self.llm,
            lock_dir=lambda: self.tmp / "ccc" / "locks",
            sleep=self.sleep,
            monotonic=self.monotonic,
            now_ms=self.now_ms,
        )


def run_cli(
    monkeypatch: Any, capsys: Any, world: World, argv: list[str], stdin: str | None = None
) -> tuple[int, dict[str, Any] | None, str]:
    """Run ``ccc <argv>`` against *world*; ``(exit, envelope|None, stderr)``."""
    import io
    import sys

    from command_center import bridge_target, cli

    monkeypatch.setattr(bridge_target, "default_deps", world.deps)

    class _Stdin(io.StringIO):
        def isatty(self) -> bool:
            return False

    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", _Stdin(stdin))
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = int(exc.code or 0)
    out = capsys.readouterr()
    lines = [line for line in out.out.splitlines() if line.strip()]
    env = json.loads(lines[-1]) if lines and lines[-1].startswith("{") else None
    return code, env, out.err
