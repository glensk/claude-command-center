"""The hand-set AIM: one naming rule over a session's ``aim_history`` stamps (tp#1991 B2).

Pure rows plus fixture stores (``tmp_path``); placeholder ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import aimprov, thread
from command_center.aimprov import TIER_UNSTAMPED, TIER_USER, HandAim, hand_set_aim
from command_center.store import Store

T0 = 1_800_000_000_000


def test_a_user_row_beats_an_older_launch_row() -> None:
    rows = [("launched", T0, "launch", 1), ("typed", T0 + 1, "user", 2)]
    assert hand_set_aim(rows, "typed") == HandAim("typed", "user", T0 + 1, TIER_USER)


def test_a_newer_launch_row_beats_an_older_user_row() -> None:
    rows = [("typed", T0, "user", 1), ("launched", T0 + 1, "launch", 2)]
    assert hand_set_aim(rows, "launched") == HandAim("launched", "launch", T0 + 1, TIER_USER)


def test_a_newer_tool_row_never_wins() -> None:
    rows = [("typed", T0, "user", 1), ("the tool's", T0 + 5, "tool", 2)]
    hand = hand_set_aim(rows, "the tool's")
    assert hand is not None and hand.text == "typed" and hand.tier == TIER_USER


@pytest.mark.parametrize("stamp", ["relay", None])
def test_relay_and_null_rows_answer_only_without_a_user_or_launch_row(stamp: str | None) -> None:
    unstamped = [("copied", T0, stamp, 1), ("the tool's", T0 + 1, "tool", 2)]
    assert hand_set_aim(unstamped, "x") == HandAim("copied", stamp, T0, TIER_UNSTAMPED)
    with_user = [("typed", T0 - 1, "user", 0), *unstamped]
    hand = hand_set_aim(with_user, "x")
    assert hand is not None and (hand.text, hand.tier) == ("typed", TIER_USER)


def test_only_tool_rows_answer_null_even_with_a_current_aim() -> None:
    rows = [("a", T0, "tool", 1), ("b", T0 + 1, "tool", 2)]
    assert hand_set_aim(rows, "b") is None


def test_no_rows_and_a_current_aim_is_unstamped_without_by_or_at() -> None:
    assert hand_set_aim([], "pre-history goal") == HandAim(
        "pre-history goal", None, None, TIER_UNSTAMPED
    )
    assert hand_set_aim([], "   ") is None
    assert hand_set_aim([], None) is None


def test_ties_on_created_at_break_by_id_and_order_is_irrelevant() -> None:
    rows = [("second", T0, "user", 2), ("first", T0, "user", 1)]
    hand = hand_set_aim(rows, None)
    assert hand is not None and hand.text == "second"


def test_the_carried_anchor_is_skipped_when_later_rows_exist() -> None:
    """A successor's backdated first row (the root's first AIM, stamped ``user``) does not
    beat its own newer ``relay`` row."""
    rows = [("root goal", T0 - 9, "user", 7), ("handed over", T0, "relay", 3)]
    hand = hand_set_aim(rows, "handed over", anchor="root goal")
    assert hand == HandAim("handed over", "relay", T0, TIER_UNSTAMPED)
    assert hand_set_aim(rows, "handed over") == HandAim("root goal", "user", T0 - 9, TIER_USER)
    only = [("root goal", T0, "user", 1)]
    assert hand_set_aim(only, "root goal", anchor="root goal") == HandAim(
        "root goal", "user", T0, TIER_USER
    )


def test_a_multi_line_aim_reports_its_first_line_and_the_clip_is_200() -> None:
    hand = hand_set_aim([("goal line\nmore detail", T0, "user", 1)], None)
    assert hand is not None and hand.text == "goal line\nmore detail"
    assert thread.first_line(hand.text, thread.USER_AIM_MAX) == "goal line"
    long = hand_set_aim([("x" * 400, T0, "user", 1)], None)
    assert long is not None
    clipped = thread.first_line(long.text, thread.USER_AIM_MAX)
    assert clipped is not None and len(clipped) == 200 and clipped.endswith("…")


def test_the_module_is_pure() -> None:
    source = Path(aimprov.__file__).read_text(encoding="utf-8")
    assert "sqlite3" not in source and "open(" not in source and ".store" not in source


# ---- Store.hand_set_aims (batched, chunked) -------------------------------------------


@pytest.fixture(name="store")
def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "ccc.db")


def _row(store: Store, sid: str, aim: str, at: int, by: str | None) -> None:
    """One history row at *at* (epoch ms), the session's current AIM set to it."""
    with store.conn:
        store.conn.execute(
            "INSERT INTO aim_history (session_id, created_at, set_by, aim, score) "
            "VALUES (:sid, :at, :by, :aim, -1)",
            {"sid": sid, "at": at, "by": by, "aim": aim},
        )
    store.update_fields(sid, aim=aim)


def test_store_hand_set_aims_answers_per_tier(store: Store) -> None:
    for sid in ("u", "r", "t", "pre"):
        store.ensure(sid, cwd="/repo")
    _row(store, "u", "typed", T0, "user")
    _row(store, "r", "copied", T0, "relay")
    _row(store, "t", "the tool's", T0, "tool")
    store.update_fields("pre", aim="pre-history goal")
    got = store.hand_set_aims(["u", "r", "t", "pre", "never-seen"])
    assert got["u"] == HandAim("typed", "user", T0, TIER_USER)
    assert got["r"] == HandAim("copied", "relay", T0, TIER_UNSTAMPED)
    assert got["pre"] == HandAim("pre-history goal", None, None, TIER_UNSTAMPED)
    assert "t" not in got and "never-seen" not in got


def test_store_hand_set_aims_skips_a_successors_carried_anchor(store: Store) -> None:
    store.ensure("root", cwd="/repo")
    _row(store, "root", "root goal", T0, "user")
    store.ensure("leaf", cwd="/repo")
    store.update_fields("leaf", handed_off_from="root")
    _row(store, "leaf", "handed over", T0 + 10, "relay")
    assert store.carry_first_aim("leaf", "root")
    hand = store.hand_set_aims(["leaf"])["leaf"]
    assert (hand.text, hand.by, hand.tier) == ("handed over", "relay", TIER_UNSTAMPED)


def test_501_ids_are_read_in_two_chunks_with_one_result(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [f"s{i:04d}" for i in range(501)]
    for i, sid in enumerate(ids):
        store.ensure(sid, cwd="/repo")
        store.conn.execute(
            "INSERT INTO aim_history (session_id, aim, score, created_at, set_by) "
            "VALUES (?,?,?,?,?)",
            (sid, f"goal {i}", -1, T0 + i, "user"),
        )
    store.conn.commit()
    sizes: list[int] = []
    real = thread._chunks  # pylint: disable=protected-access

    def spy(batch_ids: object) -> object:
        for batch in real(batch_ids):  # type: ignore[arg-type]
            sizes.append(len(batch))
            yield batch

    monkeypatch.setattr(thread, "_chunks", spy)
    got = store.hand_set_aims(ids)
    assert sizes == [500, 1, 500, 1]  # sessions, then aim_history: two chunks each
    assert len(got) == 501 and got["s0500"].text == "goal 500"
