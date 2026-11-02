"""tp#1366: an AIM's words and its work-item run (`fix decide: tp#1363, #1364`)."""

from __future__ import annotations

import pytest

from command_center import aimtext


@pytest.mark.parametrize(
    ("aim", "words", "ids"),
    [
        ("fix decide: tp#1363", "fix decide", (1363,)),
        ("fix decide: tp#1363, #1364, #1366", "fix decide", (1363, 1364, 1366)),
        ("tp#1363, #1364, #1366: fix decide", "fix decide", (1363, 1364, 1366)),
        ("tp 889 merge vaultwarden items", "merge vaultwarden items", (889,)),
        ("review the hook of tp#5: tp#9", "review the hook of tp#5", (9,)),
        (
            "Close tp#90 (launchd tab path): ccc open-job",
            "Close tp#90 (launchd tab path): ccc open-job",
            (),
        ),
        ("tp#7", "", (7,)),
        ("tp#7, #7: twice", "twice", (7,)),
        ("plain words", "plain words", ()),
        ("", "", ()),
        (None, "", ()),
        ("first line: tp#1\nsecond line", "first line", (1,)),
    ],
)
def test_split_reads_the_trailing_run_then_the_leading_one(
    aim: str | None, words: str, ids: tuple[int, ...]
) -> None:
    assert aimtext.split(aim) == (words, ids)
    assert aimtext.words(aim) == words


def test_ids_text_is_the_short_list() -> None:
    assert aimtext.ids_text((1236, 1177)) == "#1236, #1177"
    assert aimtext.ids_text(()) == ""


def test_clip_keeps_the_item_run_whole() -> None:
    aim = "rewrite the whole status line renderer so it never blocks: tp#1363"
    clipped = aimtext.clip(aim, 40)
    assert clipped.endswith("…: tp#1363") and len(clipped) <= 40
    assert aimtext.clip("short: tp#1", 40) == "short: tp#1"
    assert aimtext.clip("x" * 50, 10) == "x" * 9 + "…"
    # a run too wide to keep room for the words is cut like any text
    wide = "words: tp#1" + "".join(f", #{n}" for n in range(100, 120))
    assert aimtext.clip(wide, 20) == wide[:19] + "…"


def test_b9_strip_tail_run_and_is_run_only() -> None:
    """Only a one-line AIM ending in ``: tp#N`` with words before it is stripped (tp#1566)."""
    assert aimtext.strip_tail_run("fix decide: tp#1363, #1364") == "fix decide"
    assert aimtext.strip_tail_run("ship it — : tp#7") == "ship it"
    for aim in (
        "tp#5",
        ": tp#5",
        "tp#5: lead form",
        "plain words",
        "",
        None,
        "a: tp#1\nmore",
    ):
        assert aimtext.strip_tail_run(aim) is None, aim
    assert aimtext.is_run_only("tp#5") and aimtext.is_run_only(": tp#5, #6")
    assert not aimtext.is_run_only("fix: tp#5") and not aimtext.is_run_only("plain")
