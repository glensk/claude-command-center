"""Peer names that follow the AIM (tp#1989 B4): the name rule, clipping and dedupe."""

from __future__ import annotations

import pytest

from command_center import session_names
from command_center.models import Session
from command_center.session_names import (
    PEER_NAME_MAX,
    aim_peer_words,
    clip_words,
    dedupe_peer_name,
    peer_name,
)


def _session(name: str = "", origin: str = "") -> Session:
    return Session(session_id="s1", canonical_name=name, canonical_name_origin=origin)


@pytest.mark.parametrize(
    ("aim", "expected"),
    [
        ("decide relay information", "decide relay information"),
        ("fix decide: tp#1363, #1364", "fix decide"),
        ("tp#1989: canary #42 words", "canary #42 words"),
        ("tp1989: canary #42", "tp1989: canary #42"),
        ("first line\nsecond line", "first line"),
        ("tp#1989", None),
        ("", None),
        (None, None),
        ("   ", None),
        ("a\tb  \x07c", "a b c"),
    ],
)
def test_peer_name_is_the_aim_words_as_typed(aim: str | None, expected: str | None) -> None:
    assert peer_name(_session(), aim) == expected


def test_peer_name_none_session_uses_the_aim() -> None:
    assert peer_name(None, "ship it") == "ship it"


def test_manual_canonical_name_wins() -> None:
    assert peer_name(_session("my name", "manual"), "some aim") == "my name"


def test_non_manual_canonical_name_does_not_win() -> None:
    assert peer_name(_session("old auto", "llm"), "some aim") == "some aim"


def test_clip_at_a_word_boundary() -> None:
    aim = "decide relay information and a lot more words that go on and on"
    got = peer_name(_session(), aim)
    assert got is not None and len(got) <= PEER_NAME_MAX
    assert got == "decide relay information and a lot more"
    assert aim.startswith(got) and aim[len(got)] == " "


def test_clip_drops_a_dangling_separator() -> None:
    assert clip_words("alpha beta: gamma delta", 11) == "alpha beta"


def test_clip_cuts_a_single_long_word() -> None:
    assert clip_words("x" * 60, PEER_NAME_MAX) == "x" * PEER_NAME_MAX


def test_clip_keeps_a_short_text() -> None:
    assert clip_words("short", PEER_NAME_MAX) == "short"


def test_naming_rule_is_one_function() -> None:
    assert aim_peer_words("fix decide: tp#7") == "fix decide"
    assert session_names.peer_name.__doc__ and "aim_peer_words" in session_names.peer_name.__doc__


def test_dedupe_free_name_is_kept() -> None:
    assert dedupe_peer_name("ship it", {"other"}) == "ship it"


def test_dedupe_appends_2_then_3_casefolded() -> None:
    assert dedupe_peer_name("Ship It", {"ship it"}) == "Ship It 2"
    assert dedupe_peer_name("ship it", {"ship it", "ship it 2"}) == "ship it 3"


def test_dedupe_keeps_a_suffixed_name_within_the_max() -> None:
    base = clip_words("word " * 20, PEER_NAME_MAX)
    got = dedupe_peer_name(base, {base.casefold()})
    assert got.endswith(" 2") and len(got) <= PEER_NAME_MAX
