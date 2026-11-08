"""A realistic, oversized facts snapshot for the handoff cap tests (tp#1835).

The shape follows a real failing relay: 25 held items with kilobytes of decisions, blocked
reasons, holds (8 released), assumptions and plans, plus long lists of worktrees, plans,
schedules, keep-warm entries, commits and owned files.
"""

from __future__ import annotations

from typing import Any

from command_center.handoff_facts import rec

SID = "sid-big"
RELEASED = frozenset(range(100, 108))  # tp ids whose hold is released (record only)
HELD = tuple(range(100, 125))
QUESTION_KEYS = ("question:100:1", "question:101:1", "question:102:1")
WARNING_KEYS = ("warning:provider-a", "warning:provider-b")
SPEC_PATHS = ("/state/agents/a-relaunched.spec.md", "/state/agents/a-finished.spec.md")


def _item(tp_id: int) -> dict[str, Any]:
    released = tp_id in RELEASED
    return rec(
        "tp_item",
        f"tp_item:{tp_id}",
        "tp ownership",
        0,
        tp_id=tp_id,
        title=f"feat(area{tp_id}): " + "a long ticket title that keeps going " * 2,
        state="waiting_queue" if tp_id % 2 else "work",
        repo=f"area/repo{tp_id % 5}",
        tp_priority="P2",
        plan_path=f"/repo/area/PLAN_item-{tp_id}.md",
        boxes_done=3,
        boxes_total=9,
        next_open_boxes=[{"id": f"B{n}", "text": "box text " * 8} for n in range(4, 9)],
        decisions=[f"decision {n} on tp#{tp_id}: " + "d" * 480 for n in range(20)],
        blocked={
            "background": None,
            "kind": "queue",
            "reason": "waiting for the repo lock held by another session " * 6,
            "since": "2026-11-01T10:00:00+01:00",
            "until": "the other session releases the lock",
        },
        hold={
            "claim": not released,
            "drive": not released,
            "role": False,
            "close_authority": "handoff",
            "id": tp_id,
        },
        assumptions=["the API stays stable", "nobody else edits the plan"],
        start_condition="after the lock is free and the queue ahead drained",
        awaiting_pickup=None,
        candidate=None,
        open_follow_ups=[tp_id + 1000],
    )


def _agents() -> list[dict[str, Any]]:
    relaunch = {"subagent_type": "builder", "prompt": "continue the work " * 120}
    return [
        rec(
            "agent",
            "core:agent:a-relaunched",
            "Subagent continuations",
            0,
            agent_id="a-relaunched",
            agent_type="builder",
            description="build the parser",
            status="running",
            spec_path=SPEC_PATHS[0],
            relaunch=relaunch,
            result_inline="capsule text\n" * 40,
        ),
        rec(
            "agent",
            "core:agent:a-finished",
            "Subagent continuations",
            2,
            agent_id="a-finished",
            agent_type="reviewer",
            description="review the parser",
            status="finished",
            spec_path=SPEC_PATHS[1],
            result_inline="review result\n" * 40,
            result_path="/state/agents/a-finished.result.md",
        ),
    ]


def _shells() -> list[dict[str, Any]]:
    return [
        rec(
            "shell",
            f"core:shell:{n}",
            "Background restarts",
            0,
            command=f"tool watch --target t{n} " + "--flag " * 10,
            cwd=f"/repo/area/repo{n}",
            classification="declared-restartable",
            reason="declared by the consumer",
            description="watcher",
        )
        for n in range(2)
    ]


def _lists() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    where = "Repositories, files, worktrees"
    for n in range(24):
        out.append(
            rec(
                "worktree",
                f"worktree:/repo/area/repo{n}/.worktrees/w{n}",
                where,
                2,
                path=f"/repo/area/repo{n}/.worktrees/w{n}",
                branch=f"branch-{n}",
            )
        )
    for n in range(12):
        out.append(
            rec(
                "plan",
                f"plan:/repo/area/PLAN_{n}.md",
                "tp ownership",
                1,
                state="active",
                reported_unticked=[f"B{k}" for k in range(5)],
            )
        )
    for n in range(11):
        out.append(
            rec(
                "schedule",
                f"schedule:sch_{n:04d}",
                "Open items and questions",
                2,
                sched_id=f"sch_{n:04d}",
                at="2026-11-09T09:00:00",
                title="re-check the queue for the item " * 2,
                tp_id=100 + n,
            )
        )
    for n in range(8):
        out.append(
            rec(
                "keepwarm",
                f"keepwarm:timer:t{n}",
                "Background restarts",
                1,
                label=f"timer{n}",
                state="armed",
                note="the keep-warm timer re-arms itself after each turn " * 2,
            )
        )
    for n in range(7):
        out.append(
            rec(
                "commit",
                f"commit:{n:012x}",
                where,
                3,
                repo="area/repo1",
                subject="fix(area): a commit subject of realistic length " * 2,
            )
        )
    for n in range(6):
        out.append(
            rec("owned_file", f"owned_file:/repo/area/f{n}.py", where, 2, path=f"/repo/f{n}.py")
        )
    out.append(
        rec("todo_list", "todo_list:last", "Current state", 1, text="[x] one\n[~] two\n[ ] three")
    )
    return out


def big_snapshot(held: int = 25, title_bytes: int = 0) -> dict[str, Any]:
    """The oversized snapshot; *held* items, optionally with *title_bytes* long titles."""
    facts: list[dict[str, Any]] = [
        rec(
            "session",
            "core:session",
            "Goal and done-condition",
            0,
            session_id=SID,
            cwd="/repo/area",
            account="work",
            model="opus",
            aim="AIM-BIG: every held item relayed",
        ),
        rec("transcript", "core:transcript", "Current state", 0, path="/state/t.jsonl"),
    ]
    items = [_item(100 + n) for n in range(held)]
    if title_bytes:
        for item in items:
            item["fields"]["title"] = "t" * title_bytes
    facts += items
    facts += [
        rec(
            "question",
            key,
            "Open items and questions",
            0,
            tp_id=100 + n,
            source="plan",
            detail={"text": f"QUESTION-{n}: which way? " + "q" * 400, "open": True},
        )
        for n, key in enumerate(QUESTION_KEYS)
    ]
    facts.append(
        rec(
            "you_line",
            "you_line:1",
            "Open items and questions",
            0,
            index=1,
            decision=True,
            text="YOU-LINE: decide the schema " + "y" * 300,
        )
    )
    facts += [
        rec(
            "warning",
            key,
            "Open items and questions",
            0,
            source=key,
            message=f"WARNING-{n}: provider slow " + "w" * 300,
        )
        for n, key in enumerate(WARNING_KEYS)
    ]
    facts += _agents() + _shells() + _lists()
    return {
        "version": 1,
        "session_id": SID,
        "phase": "finalize",
        "facts": facts,
        "repos": [],
        "warnings": [],
    }
