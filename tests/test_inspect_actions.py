"""``ccc inspect -j`` additive fields: cwd, account, structured actions, Decision briefs."""

from __future__ import annotations

from pathlib import Path

import pytest
from bridgestub import SID, World, ask, assistant, question, run_cli, tool_result, turn_end, user

from command_center import config
from command_center.adapters import claude_bridge as cb
from command_center.bridge_inspect import account_fields

BRIEF = """\
**Decision brief**
Background: the build cache lives on one laptop and is lost on every wipe.
Building: a faster CI pipeline for the example repo.

Fit: unblocks the release; nothing else waits on it.
Recommendation: shared bucket, because it survives a wipe."""

REPLY = f"""\
All done for now.

## Decision briefs

**D1 Cache backend**
Background: builds take nine minutes.
Building: a faster pipeline.
Fit: unblocks tp#42 B3.
Recommendation: local disk, fastest.

**D2 Release timing**
Background: the migration touches stored data.
Recommendation: wait for the second review.

## To-do list

### tp#42 example pipeline — done when: green · ~17:00
1. You [decision]: Which cache backend? a) Local disk b) Shared bucket (brief D1)
2. Me, after 1: switch the build to the chosen backend → close tp#42
3. You [sudo]: install the runner service on the build host
### tp#77 release — waits [albert] · ~fri
4. You [decision]: Release today or wait for review (tp#78)? (brief D2)
- You [UI | Login]: approve the OAuth consent screen once
Session done when: tp#42 closed · ~17:30

## Notes
5. You [decision]: not part of the to-do list
{""}"""


def test_todo_actions_cover_every_you_line_with_group_tp_ids_and_briefs() -> None:
    actions = cb.todo_actions(REPLY)
    assert [(a["kind"], a["tp_id"], a["brief_ref"]) for a in actions] == [
        ("decision", 42, "D1"),
        ("sudo", 42, None),
        ("decision", 78, "D2"),  # a tp#N on the line beats the group heading
        ("ui|login", 77, None),
    ]
    assert actions[0]["text"] == "Which cache backend? a) Local disk b) Shared bucket"
    assert actions[0]["brief"].startswith("**D1 Cache backend**\nBackground: builds")
    assert actions[0]["brief"].endswith("Recommendation: local disk, fastest.")
    assert actions[2]["brief"] == (
        "**D2 Release timing**\nBackground: the migration touches stored data.\n"
        "Recommendation: wait for the second review."
    )
    assert actions[1]["brief"] is None and actions[3]["brief"] is None


def test_todo_actions_without_a_todo_list_is_empty() -> None:
    assert not cb.todo_actions("no list here\n\n1. You [decision]: hidden?")


def test_brief_ref_without_a_matching_entry_is_null() -> None:
    reply = "## To-do list\n1. You [decision]: A or B? (brief D9)\n"
    assert cb.todo_actions(reply) == [
        {"kind": "decision", "text": "A or B?", "tp_id": None, "brief_ref": "D9", "brief": None}
    ]


def test_todo_questions_see_group_headings_and_drop_the_brief_marker() -> None:
    questions = cb.todo_questions(REPLY)
    assert [q.text for q in questions] == [
        "Which cache backend?",
        "Release today or wait for review (tp#78)?",
    ]
    assert [o["label"] for o in questions[0].options] == ["Local disk", "Shared bucket"]


def test_decision_brief_stops_before_the_question() -> None:
    text = f"Some context first.\n\n{BRIEF}\n\nWhich backend do you want?"
    assert cb.decision_brief(text) == BRIEF


def test_decision_brief_last_block_wins_and_is_capped_head_first() -> None:
    long_bg = "x" * 5000
    text = f"**Decision brief**\nBackground: old\n\n**Decision brief**\nBackground: {long_bg}"
    brief = cb.decision_brief(text)
    assert brief is not None and len(brief) == cb.BRIEF_MAX
    assert brief.startswith("**Decision brief**\nBackground: xxx") and brief.endswith("…")
    assert cb.decision_brief("no brief") is None


def test_pending_ask_brief_comes_from_the_text_right_before_the_call() -> None:
    q = question("Which?", ["a", "b"])
    records = [
        user("go"),
        assistant(f"{BRIEF}\n\nPick one.", stop=None),
        ask("toolu_1", [q]),
        tool_result("toolu_1"),
        assistant("Second question, no brief this time.", stop=None),
        ask("toolu_2", [q]),
    ]
    ins = cb.inspect(records)
    assert ins.anchor == "toolu_2" and ins.brief is None  # the old brief is not reused
    ins = cb.inspect(records[:3])
    assert ins.anchor == "toolu_1" and ins.brief == BRIEF


def test_account_fields_unknown_is_null_and_known_is_labelled(tmp_path: Path) -> None:
    assert account_fields("") == {"config_dir": None, "account": None}
    default = str(config.claude_home())
    label = next(iter(config.claude_config_dirs()))
    got = account_fields(default)
    assert got["account"] == label
    assert got["config_dir"] == str(Path(default).expanduser().resolve())
    other = tmp_path / "elsewhere"
    assert account_fields(str(other))["account"] == "elsewhere"


def test_cli_exposes_brief_for_a_pending_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path, status="waiting")
    world.write(
        user("decide"), assistant(BRIEF, stop=None), ask("toolu_9", [question("Q?", ["a", "b"])])
    )
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-N", "-j"])
    assert code == 0 and env is not None and env["schema_version"] == 1
    data = env["data"]
    assert data["brief"] == BRIEF
    assert data["decision"]["source"] == "ask_user_question"
    assert data["actions"] == [] and data["cwd"] == "/repo"


def test_cli_exposes_actions_for_an_idle_todo_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(tmp_path)
    world.write(user("work"), *turn_end(REPLY))
    code, env, _err = run_cli(monkeypatch, capsys, world, ["inspect", "-s", SID, "-N", "-j"])
    assert code == 0 and env is not None
    data = env["data"]
    assert data["brief"] is None
    assert [a["kind"] for a in data["actions"]] == ["decision", "sudo", "decision", "ui|login"]
    assert data["actions"][0]["brief_ref"] == "D1"
    assert len(data["decision"]["questions"]) == 2


def test_backticked_heading_and_decision_line_parse() -> None:
    reply = "## `To-do list`\n1. `You [decision]: A or B? (brief D9)`\n"
    assert cb.todo_actions(reply) == [
        {"kind": "decision", "text": "A or B?", "tp_id": None, "brief_ref": "D9", "brief": None}
    ]


@pytest.mark.parametrize(
    "line",
    [
        "You [decision]: Alpha or Beta?",
        "`You [decision]: Alpha or Beta?`",
        "1. You [decision]: Alpha or Beta?",
        "2) You [decision]: Alpha or Beta?",
        "3. `You [decision]: Alpha or Beta?`",
        "- You [decision]: Alpha or Beta?",
        "* `You [decision]: Alpha or Beta?`",
        "4. - You [decision]: Alpha or Beta?",
    ],
)
def test_todo_questions_accept_every_todo_action_prefix(line: str) -> None:
    reply = f"## To-do list\n{line}\n"
    questions = cb.todo_questions(reply)
    assert [q.text for q in questions] == ["Alpha or Beta?"]
    assert [o["label"] for o in questions[0].options] == ["Alpha", "Beta"]
    assert [a["kind"] for a in cb.todo_actions(reply)] == ["decision"]


@pytest.mark.parametrize(
    "line",
    [
        "You [sudo]: install the runner",
        "1. Me, after 1: You [decision]: not a decision line",
        "Note: You [decision]: inline, not a line prefix",
    ],
)
def test_todo_questions_ignore_non_decision_lines(line: str) -> None:
    assert not cb.todo_questions(f"## To-do list\n{line}\n")


def test_one_decision_line_gets_the_free_brief() -> None:
    reply = f"Done.\n\n{BRIEF}\n\n## To-do list\n`You [decision]: Alpha or Beta?`\n"
    actions = cb.todo_actions(reply)
    assert [(a["kind"], a["brief_ref"], a["brief"]) for a in actions] == [("decision", None, BRIEF)]


def test_titled_briefs_pair_with_decision_lines_by_number() -> None:
    q1 = "**Decision brief (Q1: cache backend)**\nBackground: one.\nRecommendation: alpha."
    q2 = "**Decision brief (Q2)**\nBackground: two.\nRecommendation: gamma."
    # written in reverse order: the Q<n> in the title decides, not the order
    reply = (
        f"{q2}\n\n{q1}\n\n## To-do list\n"
        "1. You [decision]: Alpha or Beta?\n2. You [sudo]: restart the runner\n"
        "3. You [decision]: Gamma or Delta?\n"
    )
    actions = cb.todo_actions(reply)
    assert [(a["kind"], a["brief_ref"], a["brief"]) for a in actions] == [
        ("decision", None, q1),
        ("sudo", None, None),
        ("decision", None, q2),
    ]


def test_untitled_briefs_pair_by_order_when_counts_match() -> None:
    first = "**Decision brief**\nBackground: one."
    second = "**Decision brief**\nBackground: two."
    reply = (
        f"{first}\n\n{second}\n\n## To-do list\n"
        "1. You [decision]: Alpha or Beta?\n2. You [decision]: Gamma or Delta?\n"
    )
    assert [a["brief"] for a in cb.todo_actions(reply)] == [first, second]


def test_two_decision_lines_and_one_free_brief_attach_nothing() -> None:
    reply = (
        f"{BRIEF}\n\n## To-do list\n"
        "1. You [decision]: Alpha or Beta?\n2. You [decision]: Gamma or Delta?\n"
    )
    assert [a["brief"] for a in cb.todo_actions(reply)] == [None, None]


def test_section_brief_wins_over_a_free_brief() -> None:
    reply = f"{BRIEF}\n\n{REPLY}"
    actions = cb.todo_actions(reply)
    assert actions[0]["brief"].startswith("**D1 Cache backend**")
    assert actions[2]["brief"].startswith("**D2 Release timing**")


def test_an_earlier_picker_brief_is_not_reused_for_the_final_line() -> None:
    q = question("Which?", ["a", "b"])
    records = [
        user("go"),
        assistant(f"{BRIEF}\n\nPick one.", stop=None),
        ask("toolu_1", [q]),
        tool_result("toolu_1"),
        *turn_end("Done.\n\n## To-do list\n1. You [decision]: Alpha or Beta?\n"),
    ]
    ins = cb.inspect(records)
    assert BRIEF in ins.last_reply  # the old brief is in the turn, not in the final part
    assert [(a["kind"], a["brief"]) for a in ins.actions] == [("decision", None)]


def test_free_brief_in_the_final_part_reaches_the_inspection() -> None:
    reply = f"Done.\n\n{BRIEF}\n\n## To-do list\nYou [decision]: Alpha or Beta?\n"
    ins = cb.inspect([user("go"), *turn_end(reply)])
    assert [a["brief"] for a in ins.actions] == [BRIEF]


def test_paragraph_brief_keeps_all_seven_fields() -> None:
    brief = (
        "**Decision brief**\n"
        "Background: the cache is lost on every wipe.\n\n"
        "**Building**: a faster pipeline.\n\n"
        "Fit: unblocks the release.\n\n"
        "Recommendation: shared bucket.\n\n"
        "Why now: the next wipe is on Friday.\n\n"
        "**Undo:** switch the setting back.\n\n"
        "- Checked: the bucket quota fits."
    )
    reply = f"{brief}\n\nWhich one do you want?\n\n## To-do list\nYou [decision]: Alpha or Beta?\n"
    assert cb.decision_brief(reply) == brief
    assert [a["brief"] for a in cb.todo_actions(reply)] == [brief]
