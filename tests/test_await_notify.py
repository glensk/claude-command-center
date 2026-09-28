"""``ccc await`` notifications: routed through ``cfg.notify``, one per state change
(``notified_at``), and CONTENT-FREE — no sender, snippet or probe stderr ever reaches a
desktop/Slack notification, whatever path produced it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from command_center import await_delivery, await_eval, config
from command_center.await_eval import PassReport
from command_center.await_store import GRACE_SEC, SourceSpec
from command_center.checks import StructuredResult
from command_center.store import Store

NOW = 1_800_000_000
SECRET = "Customer-Secret-9f3a"  # stands in for anything an outside party wrote
ZOHO = "/bin/zoho-api.py"


class Notes:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def __call__(self, title: str, message: str) -> None:
        self.messages.append((title, message))

    def assert_content_free(self) -> None:
        for title, message in self.messages:
            assert SECRET not in title and SECRET not in message, message
            assert "req@" not in message


def _fired_result() -> StructuredResult:
    return StructuredResult(
        exit=0,
        stdout=json.dumps(
            {
                "schema_version": 1,
                "ticket": "209",
                "newest_inbound": {
                    "id": "9",
                    "time": "2027-01-15T08:00:00.000Z",
                    "from": f"req@{SECRET}.org",
                    "summary": SECRET,
                },
                "watermark": "2:9",
                "fired": True,
            }
        ),
    )


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state.db")
    store.ensure("s1", cwd=str(tmp_path))
    return store


def _arm(store: Store, until: int = NOW + 3600) -> int:
    return store.arm_await(
        "s1",
        config_dir="",
        cwd=str(Path(store.path).parent),
        no_codex=False,
        prompt_template="{event}",
        until_epoch=until,
        sources=[SourceSpec(kind="zoho-reply", spec={"exe": ZOHO, "ticket": "209"})],
        now=NOW,
    )


def _runner(result: StructuredResult) -> Any:
    return lambda _argv, **_kw: result


def test_config_notifier_uses_the_configured_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str, list[str]]] = []
    base = config.load_config()
    monkeypatch.setattr(
        config, "load_config", lambda: config.Config(**{**vars(base), "notify": ["slack"]})
    )
    from command_center import notify  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(notify, "notify", lambda t, m, c: sent.append((t, m, c)))
    await_eval.config_notifier()("ccc await", "hello")
    assert sent == [("ccc await", "hello", ["slack"])]


def test_fired_and_resumed_notice_is_content_free(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _arm(store)
    notes = Notes()
    await_eval.run_pass(
        store, now=NOW + 120, runner=_runner(_fired_result()), notifier=notes, deliver=False
    )
    report = PassReport()
    # Preflight fails here (no transcript): the BLOCKED notice must be content-free too.
    await_delivery.deliver_pending(
        store, now=NOW + 121, report=report, notifier=notes, discover=lambda: []
    )
    assert report.blocked_groups
    assert len(notes.messages) == 1
    notes.assert_content_free()


def test_source_and_group_block_notices_are_content_free(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    notes = Notes()
    leak = StructuredResult(exit=4, stderr=f"auth failed for {SECRET}")
    await_eval.run_pass(store, now=NOW + 120, runner=_runner(leak), notifier=notes, deliver=False)
    assert len(notes.messages) == 2
    notes.assert_content_free()
    # The error IS kept for `ccc await -l` (sanitized), just never notified.
    assert SECRET in store.await_sources_of(gid)[0].last_error


def test_expiry_and_disarm_notices_once_each(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _arm(store, until=NOW + 60)
    notes = Notes()
    for _ in range(3):
        await_eval.run_pass(
            store, now=NOW + 60 + GRACE_SEC, runner=_runner(_fired_result()), notifier=notes
        )
    assert [m for _t, m in notes.messages if "expired" in m] and len(notes.messages) == 1
    store.ensure("s2", cwd=str(tmp_path))
    store.arm_await(
        "s2",
        config_dir="",
        cwd=str(tmp_path),
        no_codex=False,
        prompt_template="{event}",
        until_epoch=NOW + 3600,
        sources=[SourceSpec(kind="cmd", spec={"cmd": "false"})],
        now=NOW,
    )
    store.update_fields("s2", done=True)
    for _ in range(2):
        await_eval.run_pass(
            store, now=NOW + 10, runner=_runner(StructuredResult(exit=1)), notifier=notes
        )
    assert len([m for _t, m in notes.messages if "disarmed" in m]) == 1
    notes.assert_content_free()


def test_a_reblocked_group_notifies_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gid = _arm(store)
    notes = Notes()
    bad = _runner(StructuredResult(exit=2))
    await_eval.run_pass(store, now=NOW + 120, runner=bad, notifier=notes, deliver=False)
    before = len(notes.messages)
    assert store.retry_group(gid, NOW + 200) == "armed"
    await_eval.run_pass(store, now=NOW + 200, runner=bad, notifier=notes, deliver=False)
    assert len(notes.messages) == 2 * before  # the retry reset both notice stamps
