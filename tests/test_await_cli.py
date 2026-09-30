"""``ccc await``: arming (baselines, arm-time trust, atomic ``-C``), the management
verbs, the refusals, and the parser's short-option contract.

The probe CLIs are faked (``external_deps.await_exe`` + an injected runner); the
store lives under the suite's tmp ``CLAUDE_HOME``.
"""

# pylint: disable=unused-argument  # an `env` argument activates the fixture
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from command_center import await_cli, cli, external_deps
from command_center.checks import StructuredResult
from command_center.store import Store

SID = "aaaaaaaa-1111-2222-3333-444444444444"
PURPOSE = "waiting for the vendor to confirm the quota fix"


class Runner:
    def __init__(self, answers: dict[str, StructuredResult]) -> None:
        self.answers = answers
        self.calls: list[Any] = []

    def __call__(self, argv: Any, **_kw: Any) -> StructuredResult:
        self.calls.append(argv)
        key = argv[0] if argv[1] != "--whois" else "whois"
        return self.answers[key]


ZOHO_BASE = StructuredResult(
    exit=0,
    stdout=json.dumps(
        {
            "schema_version": 1,
            "ticket": "209",
            "newest_inbound": None,
            "watermark": "17:th9",
            "fired": False,
        }
    ),
)
SLACK_BASE = StructuredResult(
    exit=0,
    stdout=json.dumps(
        {"channel": "D9", "messages": [{"ts": "5.5", "user": "U1AB"}], "has_more": False}
    ),
)


@pytest.fixture(name="env")
def env_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    # The default account's config dir sits in $HOME, so ensure_trusted (config-dir
    # parent) and is_trusted (Path.home()) name the same .claude.json.
    home = Path.home()
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home / ".claude"))
    (home / ".claude.json").write_text("{}")
    with Store() as store:
        store.ensure(SID, cwd=str(repo))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CCC_INTERNAL", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setattr(external_deps, "await_exe", lambda name, needed_for: f"/opt/bin/{name}")
    return {"repo": str(repo), "tmp": tmp_path, "trust": home / ".claude.json"}


def _args(*argv: str) -> argparse.Namespace:
    return cli.build_parser(only="await").parse_args(["await", *argv])


def _run(*argv: str, runner: Runner | None = None) -> int:
    return await_cli.cmd_await(_args(*argv), runner=runner or Runner({}))


def _runner() -> Runner:
    return Runner({"/opt/bin/zoho-api.py": ZOHO_BASE, "/opt/bin/slack_api.py": SLACK_BASE})


# --------------------------------------------------------------------------- parser
def test_every_short_option_is_one_character_and_unique() -> None:
    parser = cli.build_parser(only="await")
    actions = parser._actions  # noqa: SLF001
    sub = next(a for a in actions if isinstance(a, argparse._SubParsersAction))  # noqa: SLF001
    await_parser = sub.choices["await"]
    shorts = [
        opt
        for action in await_parser._actions  # noqa: SLF001
        for opt in action.option_strings
        if not opt.startswith("--")
    ]
    assert all(len(opt) == 2 for opt in shorts), shorts
    assert len(shorts) == len(set(shorts))
    for action in await_parser._actions:  # noqa: SLF001
        if action.option_strings and action.dest != "help":
            assert any(not o.startswith("--") for o in action.option_strings), action.dest


def test_await_is_on_the_hot_path_and_in_the_full_parser() -> None:
    assert "await" in cli._HOT_SUBCOMMANDS  # noqa: SLF001
    assert cli.build_parser().parse_args(["await", "-r"]).run


# --------------------------------------------------------------------------- arming
def test_arm_zoho_and_slack_takes_baselines(env: dict[str, Any], capsys: Any) -> None:
    runner = _runner()
    code = _run(
        "-z",
        "#209",
        "-S",
        "U1AB",
        "-u",
        "3d",
        "-m",
        "got {event}",
        "-P",
        PURPOSE,
        "-j",
        runner=runner,
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert runner.calls == [
        ["/opt/bin/zoho-api.py", "-i", "209"],
        ["/opt/bin/slack_api.py", "--dm", "U1AB", "--json"],
    ]
    with Store() as store:
        [(group, sources)] = store.list_awaits(SID)
        assert group.id == out["group_id"] and group.cwd == env["repo"]
        assert [(s.kind, s.watermark) for s in sources] == [
            ("zoho-reply", "17:th9"),
            ("slack-dm", "5.5"),
        ]
        assert sources[0].spec_dict()["exe"] == "/opt/bin/zoho-api.py"
        assert sources[1].spec_dict()["channel"] == "D9"
    trust = json.loads(env["trust"].read_text())
    assert trust["projects"][str(Path(env["repo"]).resolve())]["hasTrustDialogAccepted"] is True


def test_slack_handle_is_resolved_to_a_member_id(env: dict[str, Any]) -> None:
    runner = _runner()
    runner.answers["whois"] = StructuredResult(exit=0, stdout='{"id": "U1AB", "name": "x"}')
    assert (
        _run("-S", "someone@example.org", "-u", "1d", "-m", "{event}", "-P", PURPOSE, runner=runner)
        == 0
    )
    assert runner.calls[0] == ["/opt/bin/slack_api.py", "--whois", "someone@example.org", "--json"]
    assert runner.calls[1] == ["/opt/bin/slack_api.py", "--dm", "U1AB", "--json"]


def test_cmd_source_needs_no_baseline(env: dict[str, Any]) -> None:
    runner = Runner({})
    assert (
        _run(
            "-x",
            "test -f done",
            "-i",
            "300",
            "-u",
            "1d",
            "-m",
            "{event}",
            "-P",
            PURPOSE,
            runner=runner,
        )
        == 0
    )
    assert not runner.calls
    with Store() as store:
        [(_g, [src])] = store.list_awaits(SID)
    assert src.spec_dict()["cwd"] == env["repo"] and src.interval_sec == 300


def test_a_failed_baseline_writes_nothing(env: dict[str, Any], capsys: Any) -> None:
    runner = _runner()
    runner.answers["/opt/bin/slack_api.py"] = StructuredResult(exit=1, stderr="invalid_auth")
    assert (
        _run("-z", "209", "-S", "U1AB", "-u", "1d", "-m", "{event}", "-P", PURPOSE, runner=runner)
        == 1
    )
    assert "baseline failed" in capsys.readouterr().err
    with Store() as store:
        assert store.list_awaits(SID, include_inactive=True) == []


def test_dry_run_writes_nothing_and_grants_no_trust(env: dict[str, Any]) -> None:
    assert (
        _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, "-n", runner=_runner()) == 0
    )
    with Store() as store:
        assert store.list_awaits(SID, include_inactive=True) == []
    assert json.loads(env["trust"].read_text()) == {}


def test_a_second_arm_is_refused(env: dict[str, Any], capsys: Any) -> None:
    assert _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, runner=_runner()) == 0
    assert _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, runner=_runner()) == 1
    assert "already has an active await group" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (("-u", "1d", "-m", "{event}", "-P", PURPOSE), "at least one source"),
        (("-z", "209", "-m", "{event}", "-P", PURPOSE), "-u/--until"),
        (("-z", "209", "-u", "1d", "-m", "no placeholder", "-P", PURPOSE), "{event}"),
        (("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, "-i", "10"), ">= 60"),
        (("-z", "209", "-u", "2001-01-01", "-m", "{event}", "-P", PURPOSE), "not in the future"),
        (("-z", "209", "-u", "999d", "-m", "{event}", "-P", PURPOSE), "days out"),
        (("-z", "209", "-u", "tomorrow", "-m", "{event}", "-P", PURPOSE), "cannot read"),
        (("-z", "abc", "-u", "1d", "-m", "{event}", "-P", PURPOSE), "not a ticket number"),
        (("-r", "-z", "209"), "cannot be combined"),
        (("-l", "-C"), "cannot be combined"),
    ],
)
def test_usage_refusals(
    env: dict[str, Any], capsys: Any, argv: tuple[str, ...], needle: str
) -> None:
    assert _run(*argv, runner=_runner()) == 2
    assert needle in capsys.readouterr().err


def test_missing_dependency_exits_3(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from extdeps import MissingExternalDependency  # pylint: disable=import-outside-toplevel

    def missing(name: str, needed_for: str) -> str:
        raise MissingExternalDependency(external_deps.EXTERNAL_DEPS[name], needed_for)

    monkeypatch.setattr(external_deps, "await_exe", missing)
    assert _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE) == 3


# --------------------------------------------------------------------------- -C
def test_close_arms_both_atomically(env: dict[str, Any]) -> None:
    assert (
        _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, "-C", runner=_runner()) == 0
    )
    with Store() as store:
        session = store.get(SID)
        assert session is not None and session.close_requested_at > 0 and session.close_token
        assert store.active_await(SID) is not None


def test_close_refused_for_a_foreign_session(env: dict[str, Any], capsys: Any) -> None:
    with Store() as store:
        store.ensure("other", cwd=env["repo"])
    code = _run(
        "-s",
        "other",
        "-z",
        "209",
        "-u",
        "1d",
        "-m",
        "{event}",
        "-P",
        PURPOSE,
        "-C",
        runner=_runner(),
    )
    assert code == 2 and "CALLING" in capsys.readouterr().err
    with Store() as store:
        assert store.active_await("other") is None


def test_close_refused_when_headless(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    assert (
        _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, "-C", runner=_runner()) == 2
    )
    with Store() as store:
        assert store.active_await(SID) is None


def test_close_refused_with_a_pending_switch(env: dict[str, Any], capsys: Any) -> None:
    with Store() as store:
        store.update_fields(SID, switch_requested_at=123, switch_config_dir="/x")
    assert (
        _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, "-C", runner=_runner()) == 2
    )
    assert "switch is pending" in capsys.readouterr().err
    with Store() as store:
        session = store.get(SID)
        assert session is not None and session.switch_requested_at == 123  # never cleared


# --------------------------------------------------------------------------- verbs
def _armed(env: dict[str, Any]) -> int:
    assert _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, runner=_runner()) == 0
    with Store() as store:
        group = store.active_await(SID)
        assert group is not None
        return group.id


def test_list_shows_groups_and_sources(env: dict[str, Any], capsys: Any) -> None:
    gid = _armed(env)
    capsys.readouterr()
    assert _run("-l") == 0
    out = capsys.readouterr().out
    assert f"group {gid}" in out and "zoho-reply" in out
    assert _run("-l", "-A", "-j") == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == gid


def test_disarm_and_all(env: dict[str, Any]) -> None:
    gid = _armed(env)
    assert _run("-d", str(gid)) == 0
    assert _run("-d", str(gid)) == 1  # not active any more
    _armed(env)
    assert _run("-d", "all") == 0
    with Store() as store:
        assert store.active_await(SID) is None


def test_retry_needs_a_blocked_group(env: dict[str, Any]) -> None:
    gid = _armed(env)
    assert _run("-R", str(gid)) == 1
    with Store() as store:
        store.block_group(gid, "x", 1, from_states=("armed",))
    assert _run("-R", str(gid)) == 0


def test_run_is_silent_when_idle(env: dict[str, Any], capsys: Any) -> None:
    assert _run("-r") == 0
    assert capsys.readouterr().out == ""


def test_run_dry_run_probes_nothing(env: dict[str, Any], capsys: Any) -> None:
    _armed(env)
    runner = Runner({})
    capsys.readouterr()
    assert _run("-r", "-n", "-j", runner=runner) == 0
    assert not runner.calls
    assert json.loads(capsys.readouterr().out)["would_probe"] == []  # not due yet


def test_parse_until_forms() -> None:
    now = 1_800_000_000.0
    assert await_cli.parse_until("30m", now) == int(now) + 1800
    assert await_cli.parse_until("2w", now) == int(now) + 14 * 86400
    day = await_cli.parse_until("2027-01-20", now)
    assert day - await_cli.parse_until("2027-01-20T00:00", now) == 86399


def test_an_unstamped_single_account_session_snapshots_the_default_dir(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import accounts  # pylint: disable=import-outside-toplevel

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")  # armed from OUTSIDE the session
    assert (
        _run("-s", SID, "-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, runner=_runner())
        == 0
    )
    with Store() as store:
        group = store.active_await(SID)
    assert group is not None and group.config_dir == str(accounts.default_config_dir())


# --------------------------------------------------------------------------- -L labels
def test_labels_go_to_zoho_then_slack_then_cmd(env: dict[str, Any], capsys: Any) -> None:
    code = _run(
        "-x", "test -f done", "-S", "U1AB", "-z", "209",
        "-L", "vendor\nreply", "-L", "boss DM",
        "-u", "1d", "-m", "{event}", "-P", PURPOSE, runner=_runner(),
    )  # fmt: skip
    assert code == 0
    with Store() as store:
        [(_g, sources)] = store.list_awaits(SID)
    # -z first, then -S, then -x (argparse keeps no order across options); control
    # characters are flattened, and a source without a label keeps ''.
    assert [(s.kind, s.label) for s in sources] == [
        ("zoho-reply", "vendor reply"),
        ("slack-dm", "boss DM"),
        ("cmd", ""),
    ]
    capsys.readouterr()
    assert _run("-l") == 0
    assert "[vendor reply]" in capsys.readouterr().out
    assert _run("-l", "-j") == 0
    labels = [s["label"] for s in json.loads(capsys.readouterr().out)[0]["sources"]]
    assert labels == ["vendor reply", "boss DM", ""]


def test_more_labels_than_sources_is_refused(env: dict[str, Any], capsys: Any) -> None:
    code = _run(
        "-z",
        "209",
        "-L",
        "a",
        "-L",
        "b",
        "-u",
        "1d",
        "-m",
        "{event}",
        "-P",
        PURPOSE,
        runner=_runner(),
    )
    assert code == 2 and "2 -L label(s) for 1 source(s)" in capsys.readouterr().err
    with Store() as store:
        assert store.list_awaits(SID, include_inactive=True) == []


def test_label_with_a_verb_is_refused(env: dict[str, Any], capsys: Any) -> None:
    assert _run("-l", "-L", "x") == 2
    assert "cannot be combined" in capsys.readouterr().err


# --------------------------------------------------------------------------- -P / -T
def test_arming_without_a_purpose_is_refused(env: dict[str, Any], capsys: Any) -> None:
    assert _run("-z", "209", "-u", "1d", "-m", "{event}", runner=_runner()) == 2
    assert "-P/--purpose" in capsys.readouterr().err
    with Store() as store:
        assert store.list_awaits(SID, include_inactive=True) == []


@pytest.mark.parametrize("purpose", ["too short", "  vendor\n\n reply  ", "\t" * 30])
def test_a_too_short_purpose_is_refused(env: dict[str, Any], capsys: Any, purpose: str) -> None:
    code = _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", purpose, runner=_runner())
    assert code == 2
    assert "-P/--purpose" in capsys.readouterr().err
    with Store() as store:
        assert store.list_awaits(SID, include_inactive=True) == []


def test_purpose_and_items_are_stored_and_listed(env: dict[str, Any], capsys: Any) -> None:
    code = _run(
        "-z", "209", "-u", "1d", "-m", "{event}",
        "-P", "  waiting for the vendor\n  to confirm the quota fix " + "x" * 500,
        "-T", "Zoho # 256", "-T", "SD-69829", "-T", "zoho#256", "-T", "https://example.org/a#1",
        runner=_runner(),
    )  # fmt: skip
    assert code == 0
    with Store() as store:
        [(group, _s)] = store.list_awaits(SID)
    assert group.purpose.startswith("waiting for the vendor to confirm the quota fix x")
    assert len(group.purpose) == await_cli.MAX_PURPOSE_CHARS
    assert group.items_list() == ["zoho#256", "SD-69829", "https://example.org/a#1"]
    capsys.readouterr()
    assert _run("-l") == 0
    out = capsys.readouterr().out
    assert "    purpose: waiting for the vendor to confirm the quota fix" in out
    assert "    items: zoho#256, SD-69829, https://example.org/a#1" in out
    assert _run("-l", "-j") == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["purpose"] == group.purpose
    assert row["items"] == ["zoho#256", "SD-69829", "https://example.org/a#1"]


def test_list_shows_no_purpose_for_a_legacy_group(env: dict[str, Any], capsys: Any) -> None:
    assert _run("-z", "209", "-u", "1d", "-m", "{event}", "-P", PURPOSE, runner=_runner()) == 0
    with Store() as store:
        store.conn.execute("UPDATE await_groups SET purpose = '', items = '[]'")
        store.conn.commit()
    capsys.readouterr()
    assert _run("-l") == 0
    out = capsys.readouterr().out
    assert "purpose: (no purpose recorded)" in out and "items:" not in out


def test_purpose_or_item_with_a_verb_is_refused(env: dict[str, Any], capsys: Any) -> None:
    assert _run("-l", "-P", PURPOSE) == 2
    assert _run("-d", "all", "-T", "tp#1") == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_every_help_example_that_arms_carries_a_purpose() -> None:
    parser = cli.build_parser(only="await")
    sub = next(
        a
        for a in parser._actions  # noqa: SLF001
        if isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
    )
    text = sub.choices["await"].description or ""
    examples = text.split("examples:\n", 1)[1]
    # one example = a `ccc await` line plus its `\`-continued lines
    blocks = examples.replace("\\\n", " ").splitlines()
    arming = [
        b for b in blocks if "ccc await" in b and any(f" {o} " in b for o in ("-z", "-S", "-x"))
    ]
    assert arming and all(" -P " in b for b in arming), arming
