"""The hourly OpenCode free-tier probe (tp#392, plan C1): target, ledger row, agent, note.

* the target is the first `opencode-free` rung of `ai ladders -j`'s cheap ladder, config's
  `opencode_free_model` only when ai.py is missing / fails / names no such rung;
* every probe — inconclusive ones included — is one run-ledger row;
* `launchd.install()` writes and loads the `<prefix>.ccc-quota-probe` agent beside the
  daemon (StartInterval 3600, not at load);
* the free row says `next probe in N min` only while that agent's plist is installed.

Everything runs under the autouse isolation of `conftest.py` (temp HOME + CCC_HOME), and
`AI_BIN` is pinned per test so the developer's real ai.py is never asked.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path

import pytest

from command_center import cli, codex_ledger, config, launchd, quota, quota_probe, usage

NOW = 1_790_000_000
_LADDERS = {
    "ladders": {
        "cheap": {
            "rungs": [
                {"tool": "agy-gpt", "model": "gpt-oss-120b-medium"},
                {"tool": "opencode-free", "model": "opencode/muse-spark-9-free"},
                {"tool": "opencode-free", "model": "opencode/second-free"},
            ]
        },
        "tierB": {"rungs": [{"tool": "opencode-free", "model": "opencode/wrong-ladder-free"}]},
    }
}


def _fake_ai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """An executable `ai.py` stand-in whose `ladders -j` runs *body* (shell)."""
    exe = tmp_path / "ai.py"
    exe.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("AI_BIN", str(exe))
    return exe


def _no_ai(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_BIN", str(tmp_path / "absent-ai.py"))


# ── the target ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "payload",
    [
        _LADDERS,
        {
            "ladders": [
                {"name": "tierB", "rungs": []},
                {"name": "cheap", **_LADDERS["ladders"]["cheap"]},
            ]
        },
        _LADDERS["ladders"],
        {
            "cheap": {
                "rungs": ["agy gemini-3.8-flash-low", "opencode-free opencode/muse-spark-9-free"]
            }
        },
    ],
)
def test_model_from_ladders_reads_the_first_cheap_opencode_free_rung(payload: object) -> None:
    assert quota_probe.model_from_ladders(payload) == "muse-spark-9-free"


@pytest.mark.parametrize("payload", [{}, [], {"ladders": {"cheap": {"rungs": []}}}, "x", None])
def test_model_from_ladders_without_such_a_rung_is_empty(payload: object) -> None:
    assert quota_probe.model_from_ladders(payload) == ""


def test_target_comes_from_ai_ladders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = tmp_path / "ladders.json"
    payload.write_text(json.dumps(_LADDERS), encoding="utf-8")
    _fake_ai(tmp_path, monkeypatch, f'[ "$1 $2" = "ladders -j" ] && cat "{payload}"')
    assert quota_probe.probe_target() == ("muse-spark-9-free", "ai ladders")


@pytest.mark.parametrize(
    "body",
    [
        "echo 'ai.py: invalid choice: ladders' >&2; exit 2",  # today's ai.py: no such command
        "echo not-json",
        'echo \'{"ladders": {"cheap": {"rungs": []}}}\'',  # no opencode-free rung
    ],
)
def test_target_falls_back_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _fake_ai(tmp_path, monkeypatch, body)
    assert quota_probe.probe_target() == (config.load_config().opencode_free_model, "config")


def test_target_without_ai_py_is_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_ai(tmp_path, monkeypatch)
    assert quota_probe.probe_target()[1] == "config"


# ── the probe and its ledger row ─────────────────────────────────────────────────────
def _stub_probe(monkeypatch: pytest.MonkeyPatch, verdict: tuple[bool | None, str]) -> list[str]:
    asked: list[str] = []

    def fake(model: str = "", timeout: float | None = None) -> tuple[bool | None, str]:
        del timeout
        asked.append(model)
        return verdict

    monkeypatch.setattr(usage, "probe_opencode_free", fake)
    return asked


def test_a_served_probe_writes_one_ledger_row_and_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ai(tmp_path, monkeypatch)
    asked = _stub_probe(monkeypatch, (True, "hallo"))
    outcome = quota_probe.run(NOW)
    assert asked == [config.load_config().opencode_free_model]
    assert outcome.ok is True and outcome.recorded
    (row,) = codex_ledger.read_runs()
    assert row["provider"] == "opencode" and row["seat"] == "free" and row["id"] == "opencode:free"
    assert row["purpose"] == "probe-opencode-free"
    assert row["outcome"] == "ok" and row["ok"] is True and isinstance(row["ms"], int)
    assert row["model"] == "opencode/" + asked[0]
    assert row["caller"] == "ccc quota -P" and row["note"] == "target from config"
    assert row["attempt_id"]
    cached = usage.read_opencode_usage(NOW)
    assert cached is not None and cached.probe is not None and cached.probe.ok is True


def test_an_inconclusive_probe_is_a_row_but_not_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ai(tmp_path, monkeypatch)
    _stub_probe(monkeypatch, (None, "no answer within 150s"))
    quota_probe.run(NOW)
    (row,) = codex_ledger.read_runs()
    assert (row["outcome"], row["ok"], row["error"]) == ("inconclusive", False, "inconclusive")
    assert row["error_message"] == "no answer within 150s"
    cached = usage.read_opencode_usage(NOW)
    assert cached is None or cached.probe is None


def test_cli_probe_reports_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_ai(tmp_path, monkeypatch)
    _stub_probe(monkeypatch, (False, "Rate limit exceeded"))
    assert cli.main(["quota", "-P"]) == quota.EXIT_BLOCKED
    out = capsys.readouterr().out
    assert "(target from config)" in out and "opencode-free refused" in out
    assert codex_ledger.read_runs()[0]["outcome"] == "refused"


# ── the LaunchAgent ──────────────────────────────────────────────────────────────────
def test_probe_label_sits_beside_the_install_prefix() -> None:
    cfg = config.Config(launchd_label="com.example.claude-command-center")
    assert launchd.quota_probe_label(cfg) == "com.example.ccc-quota-probe"
    assert launchd.quota_probe_label(config.Config(launchd_label="solo")) == "solo.ccc-quota-probe"


def test_probe_plist_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _fake_ai(tmp_path, monkeypatch, "exit 0")
    cfg = config.Config(launchd_label="com.test.ccc")
    plist = plistlib.loads(launchd.quota_probe_plist(cfg).encode("utf-8"))
    assert plist["Label"] == "com.test.ccc-quota-probe"
    assert plist["ProgramArguments"][1:] == ["quota", "-P"]
    assert plist["StartInterval"] == 3600
    assert plist["RunAtLoad"] is False, "loading the agent must not spend a request"
    env = plist["EnvironmentVariables"]
    assert env["AI_BIN"] == str(exe), "launchd's PATH rarely reaches ai.py"
    assert env["AI_NO_AUTOCOMMIT"] == "1" and "PATH" in env and "HOME" in env
    assert plist["StandardOutPath"].endswith("quota-probe.log")


def test_probe_plist_without_ai_py_has_no_ai_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ai(tmp_path, monkeypatch)
    plist = plistlib.loads(launchd.quota_probe_plist().encode("utf-8"))
    assert "AI_BIN" not in plist["EnvironmentVariables"]


def test_install_writes_and_loads_both_agents_and_uninstall_removes_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_ai(tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    assert launchd.install() == 0
    daemon, probe = launchd._plist_path(), launchd.quota_probe_plist_path()  # noqa: SLF001
    assert daemon.exists() and probe.exists()
    loaded = [cmd[2] for cmd in calls if cmd[:2] == ["launchctl", "load"]]
    assert loaded == [str(daemon), str(probe)]
    assert launchd.uninstall() == 0
    assert not daemon.exists() and not probe.exists()


# ── `next probe in N min` ────────────────────────────────────────────────────────────
def _probe_row_at(epoch: int) -> None:
    codex_ledger.append_rows(
        [
            codex_ledger.validate_row(
                {
                    "provider": "opencode",
                    "seat": "free",
                    "purpose": quota_probe.PURPOSE,
                    "outcome": "ok",
                    "ok": True,
                    "ms": 900,
                    "ts": codex_ledger._iso(epoch),  # noqa: SLF001
                }
            )
        ]
    )


def _free_row(now: int) -> dict:
    snap = quota.snapshot(now=now)
    return next(p for p in snap["providers"] if p["id"] == "opencode:free")


def test_no_agent_no_promise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(usage._OPENCODE_DB_ENV, "/nonexistent/opencode.db")  # noqa: SLF001
    _probe_row_at(NOW - 600)
    assert "next_probe_at" not in _free_row(NOW)
    assert quota_probe.next_probe_note(0, NOW) == ""


def test_installed_agent_schedules_from_the_last_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(usage._OPENCODE_DB_ENV, "/nonexistent/opencode.db")  # noqa: SLF001
    _no_ai(tmp_path, monkeypatch)
    path = launchd.quota_probe_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(launchd.quota_probe_plist(), encoding="utf-8")
    _probe_row_at(NOW - 600)
    row = _free_row(NOW)
    assert row["next_probe_at"] == NOW - 600 + 3600
    assert cli._quota_next_probe_note(row, NOW) == "next probe in 50 min"  # noqa: SLF001
    assert quota_probe.next_probe_note(NOW - 1, NOW) == "next probe due"
