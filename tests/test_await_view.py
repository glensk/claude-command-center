"""The AWAITING section: ``-L`` labels (storage + migration), the shared formatter
(``await_view``), the trailing ``ccc ls`` block and the TUI rows.

Everything runs under the suite's tmp ``CLAUDE_HOME``; no source is ever probed.
"""

# pylint: disable=unused-argument  # a `home` argument activates the fixture
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from command_center import await_view
from command_center import store as store_mod
from command_center.adapters.claude import ClaudeAdapter
from command_center.await_store import AWAIT_SCHEMA, AwaitGroup, AwaitSource, SourceSpec
from command_center.store import Store
from command_center.views import ls as ls_view

SID = "bbbbbbbb-1111-2222-3333-444444444444"


PURPOSE = "Waiting for the vendor to confirm the quota fix before closing the ticket."


def _arm(
    store: Store,
    *,
    labels: tuple[str, ...] = ("", "", ""),
    purpose: str = PURPOSE,
    items: tuple[str, ...] = ("zoho#256", "tp#12"),
) -> int:
    now = int(time.time())
    specs = [
        SourceSpec(kind="zoho-reply", spec={"schema_version": 1, "ticket": "209"}),
        SourceSpec(kind="slack-dm", spec={"schema_version": 1, "user_id": "U1AB"}),
        SourceSpec(kind="cmd", spec={"schema_version": 1, "cmd": "gh pr checks 12 --required"}),
    ]
    for spec, label in zip(specs, labels, strict=True):
        spec.label = label
    return store.arm_await(
        SID,
        config_dir="/acct",
        cwd="/work/repo",
        no_codex=False,
        prompt_template="got {event}",
        until_epoch=now + 86400,
        sources=specs,
        now=now,
        purpose=purpose,
        items=items,
    )


@pytest.fixture(name="home")
def home_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    with Store() as store:
        store.ensure(SID, cwd="/work/repo")
        store.update_fields(SID, aim="the vendor answered and the fix is merged")
    return tmp_path


# --------------------------------------------------------------------------- storage
def test_label_is_stored_per_source(home: Path) -> None:
    with Store() as store:
        _arm(store, labels=("vendor reply", "", "ci green"))
        [(_group, sources)] = store.list_awaits(SID)
    assert [s.label for s in sources] == ["vendor reply", "", "ci green"]


def test_label_column_is_added_to_an_older_db(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    old_schema = AWAIT_SCHEMA.replace(
        "notified_at   INTEGER NOT NULL DEFAULT 0,\n    label         TEXT    NOT NULL DEFAULT ''",
        "notified_at   INTEGER NOT NULL DEFAULT 0",
    )
    assert "label" not in old_schema
    conn = sqlite3.connect(db)
    conn.executescript(store_mod._SCHEMA + old_schema)  # noqa: SLF001
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('s1', '/repo')")
    conn.execute(
        "INSERT INTO await_groups (session_id, prompt_template, until_epoch, grace_until_epoch) "
        "VALUES ('s1', '{event}', 9999999999, 9999999999)"
    )
    conn.execute("INSERT INTO await_sources (group_id, kind, spec) VALUES (1, 'cmd', '{}')")
    conn.commit()
    conn.close()
    with Store(db) as store:
        cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(await_sources)")}
        [(_group, [src])] = store.list_awaits("s1")
    assert "label" in cols
    assert src.label == ""


def test_purpose_and_items_columns_are_added_to_an_older_db(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    old_schema = AWAIT_SCHEMA.replace(
        "updated_at         INTEGER NOT NULL DEFAULT 0,\n"
        "    purpose            TEXT    NOT NULL DEFAULT '',\n"
        "    items              TEXT    NOT NULL DEFAULT '[]'\n",
        "updated_at         INTEGER NOT NULL DEFAULT 0\n",
    )
    assert "purpose" not in old_schema and "items " not in old_schema
    conn = sqlite3.connect(db)
    conn.executescript(store_mod._SCHEMA + old_schema)  # noqa: SLF001
    conn.execute("INSERT INTO sessions (session_id, cwd) VALUES ('s1', '/repo')")
    conn.execute(
        "INSERT INTO await_groups (session_id, prompt_template, until_epoch, grace_until_epoch) "
        "VALUES ('s1', '{event}', 9999999999, 9999999999)"
    )
    conn.execute("INSERT INTO await_sources (group_id, kind, spec) VALUES (1, 'cmd', '{}')")
    conn.commit()
    conn.close()
    with Store(db) as store:
        cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(await_groups)")}
        [(group, _sources)] = store.list_awaits("s1")
    assert {"purpose", "items"} <= cols
    assert group.purpose == "" and group.items_list() == []
    assert await_view.purpose_text(group) == await_view.NO_PURPOSE


# --------------------------------------------------------------------------- formatter
def _src(kind: str, spec: str, **kw: Any) -> AwaitSource:
    return AwaitSource(id=1, group_id=1, kind=kind, spec=spec, **kw)


def test_source_text_defaults_and_label() -> None:
    assert await_view.source_text(_src("zoho-reply", '{"ticket": "209"}')) == "zoho #209"
    assert await_view.source_text(_src("slack-dm", '{"user_id": "U1AB"}')) == "slack DM U1AB"
    long_cmd = '{"cmd": "' + "x" * 50 + '"}'
    assert await_view.source_text(_src("cmd", long_cmd)) == "cmd " + "x" * 40 + "…"
    assert await_view.source_text(_src("cmd", '{"cmd": "true"}')) == "cmd true"
    labelled = _src("zoho-reply", '{"ticket": "209"}', label="vendor reply")
    assert await_view.source_text(labelled) == "vendor reply"


def test_summary_state_until_and_next_probe() -> None:
    now = time.time()
    group = AwaitGroup(id=3, session_id=SID, until_epoch=int(now) + 3600, state="armed")
    due = int(now) + 120
    sources = [
        _src("zoho-reply", '{"ticket": "209"}', next_check_at=due),
        _src("cmd", '{"cmd": "true"}', next_check_at=due + 600, state="blocked"),
    ]
    text = await_view.summary(group, sources, now)
    assert text.startswith("zoho #209 · cmd true — armed · until ")
    assert "next " + time.strftime("%H:%M", time.localtime(due)) in text
    blocked = AwaitGroup(id=3, session_id=SID, state="blocked", blocked_reason="no source left")
    text = await_view.summary(blocked, sources, now)
    assert "blocked(no source left)" in text and "next" not in text


def test_detail_lines_describe_the_whole_group(home: Path) -> None:
    with Store() as store:
        gid = _arm(store, labels=("vendor reply", "", ""))
        store.conn.execute(
            "UPDATE await_sources SET fail_count = 2, last_error = 'exit 1: boom' "
            "WHERE kind = 'cmd'"
        )
        store.conn.execute(
            "UPDATE await_sources SET spec = ? WHERE kind = 'cmd'",
            ('{"cmd": "gh pr checks 12 --required && test -f ' + "y" * 60 + '", "cwd": "/w"}',),
        )
        store.conn.commit()
        [entry] = await_view.visible_awaits(store)
    lines = await_view.detail_lines(entry, root="/nowhere")
    fields = dict(lines)
    assert [f for f, _v in lines] == [
        "Purpose", "Related items", "Target session", "Source 1", "Source 2", "Source 3",
        "Until", "Group state", "Armed at", "Delivery channels", "Resume prompt",
    ]  # fmt: skip
    assert fields["Purpose"] == PURPOSE
    assert fields["Related items"] == "zoho#256, tp#12"
    target = fields["Target session"]
    assert SID[:8] in target and "live (idle)" in target and "the vendor answered" in target
    assert fields["Source 1"].startswith("zoho-reply [vendor reply] ticket #209 — armed")
    assert "next probe " in fields["Source 1"] and "fails 0" in fields["Source 1"]
    assert fields["Source 2"].startswith("slack-dm user U1AB — armed")
    assert "y" * 60 in fields["Source 3"]  # the FULL command, never cut
    assert "(in /w)" in fields["Source 3"]
    assert "fails 2 · last error: exit 1: boom" in fields["Source 3"]
    assert fields["Group state"] == f"armed  (group {gid})"
    assert fields["Resume prompt"] == "got {event}"
    assert fields["Armed at"] != "-" and fields["Until"] != "-"


def test_detail_lines_of_a_legacy_blocked_group_without_session() -> None:
    entry = await_view.AwaitEntry(
        AwaitGroup(id=4, session_id=SID, cwd="/x/proj", state="blocked", blocked_reason="gone"),
        [],
        None,
    )
    fields = dict(await_view.detail_lines(entry, root="/nowhere"))
    assert fields["Purpose"] == "(no purpose recorded)"
    assert fields["Related items"] == "—"
    assert "unknown session" in fields["Target session"]
    assert fields["Sources"] == "(none)"
    assert fields["Group state"] == "blocked(gone)  (group 4)"
    assert fields["Armed at"] == "-"


@pytest.mark.parametrize("state", ["delivered", "expired", "disarmed"])
def test_finished_groups_are_hidden(home: Path, state: str) -> None:
    with Store() as store:
        gid = _arm(store)
        assert [e.group.id for e in await_view.visible_awaits(store)] == [gid]
        store.conn.execute("UPDATE await_groups SET state = ? WHERE id = ?", (state, gid))
        store.conn.commit()
        assert await_view.visible_awaits(store) == []


def test_ls_says_no_purpose_recorded_for_a_legacy_group(home: Path) -> None:
    with Store() as store:
        _arm(store, purpose="", items=())
        out = _ls(store)
    tail = out[out.index("AWAITING") :].splitlines()
    assert tail[2] == "    ↳ (no purpose recorded)"


def test_visible_entry_carries_the_session_and_its_aim(home: Path) -> None:
    with Store() as store:
        _arm(store)
        [entry] = await_view.visible_awaits(store)
    assert entry.cwd == "/work/repo"
    assert await_view.aim_text(entry) == "the vendor answered and the fix is merged"


# --------------------------------------------------------------------------- ccc ls
def _ls(store: Store) -> str:
    return ls_view.render(store, ClaudeAdapter())


def test_ls_has_an_awaiting_block_only_while_a_group_is_active(home: Path) -> None:
    with Store() as store:
        assert "AWAITING" not in _ls(store)
        gid = _arm(store, labels=("vendor reply", "", ""))
        out = _ls(store)
        tail = out[out.index("AWAITING") :].splitlines()
        hint = f"AWAITING  {await_view.AWAITING_HINT}"
        assert tail[0] == f"{hint}  Python API: ? · AppleScript: ?"  # never recorded
        assert f"group {gid}" in tail[1] and "vendor reply · slack DM U1AB · cmd gh pr" in tail[1]
        assert "armed · until" in tail[1]
        assert "│ the vendor answered" in tail[1]
        assert tail[2] == f"    ↳ {PURPOSE}  · items: zoho#256, tp#12"
        store.disarm_group(gid, int(time.time()))
        assert "AWAITING" not in _ls(store)


# --------------------------------------------------------------------------- TUI
def test_await_row_cells_format() -> None:
    from command_center.views.tui import _AIM_COL, _FOLDER_COL, _await_row_cells

    now = time.time()
    entry = await_view.AwaitEntry(
        AwaitGroup(id=1, session_id=SID, cwd="/elsewhere/proj", until_epoch=int(now) + 60),
        [_src("zoho-reply", '{"ticket": "7"}', next_check_at=int(now) + 30)],
        None,
    )
    cells = _await_row_cells(entry, now, None)
    assert cells[0].plain == "◷"
    assert cells[_FOLDER_COL].plain.endswith("proj")
    aim = cells[_AIM_COL].plain
    assert "zoho #7 — armed · until" in aim and "│" not in aim  # no session → no AIM


def test_tui_shows_the_section_and_enter_focuses_the_session(home: Path) -> None:
    from command_center.views.tui import _AIM_COL, _FOLDER_COL, CommandCenterApp, SessionTable

    with Store() as store:
        _arm(store, labels=("vendor reply", "", ""))

    def plain(cell: object) -> str:
        return cell.plain if isinstance(cell, Text) else str(cell)

    seen: dict[str, Any] = {}

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            while any(not w.is_finished for w in app.workers):
                await pilot.pause()
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            folders = [plain(table.get_row_at(i)[_FOLDER_COL]) for i in range(table.row_count)]
            seen["folders"] = folders
            header = next(i for i, f in enumerate(folders) if "AWAITING" in f)
            future = next(i for i, f in enumerate(folders) if "FUTURE" in f)
            seen["order"] = header < future
            seen["aim"] = plain(table.get_row_at(header + 1)[_AIM_COL])
            table.move_cursor(row=header + 1)
            await pilot.pause()
            detail = app.query_one("#detail-fields-view")
            seen["detail"] = str(detail.render())
            seen["current"] = app._current  # noqa: SLF001
            await pilot.press("enter")
            await pilot.pause()
            seen["cursor_key"] = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            seen["target"] = SID

    asyncio.run(scenario())
    assert seen["order"], seen["folders"]
    assert "vendor reply · slack DM U1AB" in seen["aim"]
    assert seen["cursor_key"].value == seen["target"]
    # The AWAITING row fills the detail pane (read-only: no session is "current").
    assert seen["current"] is None
    assert f"Purpose: {PURPOSE}" in seen["detail"]
    assert "Related items: zoho#256, tp#12" in seen["detail"]
    assert "Resume prompt: got {event}" in seen["detail"]


def test_tui_has_no_section_without_active_groups(home: Path) -> None:
    from command_center.views.tui import _FOLDER_COL, CommandCenterApp, SessionTable

    folders: list[str] = []

    async def scenario() -> None:
        app = CommandCenterApp()
        async with app.run_test() as pilot:
            while any(not w.is_finished for w in app.workers):
                await pilot.pause()
            await pilot.pause()
            table = app.query_one("#sessions", SessionTable)
            folders.extend(str(table.get_row_at(i)[_FOLDER_COL]) for i in range(table.row_count))

    asyncio.run(scenario())
    assert not any("AWAITING" in f for f in folders)
