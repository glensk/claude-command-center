"""Tests for the configurable Codex seat ORDER: storage, resolution, presentation.

The order (`codex_seat_order` in ccc's `config.toml`) is the sequence every Codex
consumer tries the ChatGPT logins in. Three properties are worth guarding, because each
failure mode is silent:

* a seat missing from the configured list is still tried (appended), and a label naming
  no login is reported rather than dropping a real seat to nowhere;
* the account pin is INERT once an explicit order exists — two competing "use this seat"
  knobs is how a run ends up on a seat nobody chose;
* writing the order never eats config keys ccc does not know (`save_config` re-emits only
  `DEFAULTS`), and never leaves a stale pin behind.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest
from conftest import SeatFixture

from command_center import cli, config, quota, usage
from command_center import codex_in_claude as cic

_NOW = 1_800_000_000


def _row(pid: str, label: str, state: str = quota.AVAILABLE) -> quota.ProviderQuota:
    return quota.ProviderQuota(id=pid, kind="codex", state=state, account=label)


def _homes(tmp_path: Path) -> dict[str, Path]:
    return {
        "default": tmp_path / "default",
        "private": tmp_path / "private",
        "de": tmp_path / "de",
    }


# ── pure resolution ───────────────────────────────────────────────────────────────
def test_resolve_seat_order_appends_missing_dedupes_and_reports_unknown(tmp_path: Path) -> None:
    homes = _homes(tmp_path)
    order, unknown = quota.resolve_seat_order(["de", "de", "nope", "private"], homes)
    # configured first (deduped), then every configured login not listed, canonical order
    assert order == ["de", "private", "default"]
    assert unknown == ["nope"]
    assert quota.resolve_seat_order([], homes) == (["default", "private", "de"], [])


def test_codex_seat_candidates_honour_the_order_and_skip_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "codex_seat_order", lambda: ["private", "de", "default"])
    rows = [_row("codex", "default"), _row("codex:private", "private"), _row("codex:de", "de")]
    order = quota.codex_seat_order_labels(_homes(tmp_path))
    assert [c.id for c in quota.codex_seat_candidates(rows, "", order)] == [
        "codex:private",
        "codex:de",
        "codex",
    ]
    # BLOCKED and DISABLED are excluded; UNKNOWN stays runnable (fail-open).
    rows[1] = _row("codex:private", "private", quota.BLOCKED)
    rows[2] = _row("codex:de", "de", quota.UNKNOWN)
    assert [c.id for c in quota.codex_seat_candidates(rows, "", order)] == ["codex:de", "codex"]
    rows[2] = _row("codex:de", "de", quota.DISABLED)
    assert [c.id for c in quota.codex_seat_candidates(rows, "", order)] == ["codex"]


def test_pin_leads_only_while_no_order_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin is a preference; an explicit order is an instruction that outranks it."""
    rows = [_row("codex", "default"), _row("codex:private", "private"), _row("codex:de", "de")]
    order = ["private", "de", "default"]
    monkeypatch.setattr(config, "codex_seat_order", lambda: [])
    assert quota.codex_seat_candidates(rows, "de", order)[0].id == "codex:de"
    monkeypatch.setattr(config, "codex_seat_order", lambda: list(order))
    assert quota.codex_seat_candidates(rows, "de", order)[0].id == "codex:private"


def test_select_codex_account_keeps_its_two_argument_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing callers pass no order: the rows' own sequence ranks them."""
    monkeypatch.setattr(config, "codex_seat_order", lambda: [])
    rows = [_row("codex", "default"), _row("codex:private", "private")]
    assert quota.select_codex_account(rows, "") == "codex"
    assert quota.select_codex_account(rows, "private") == "codex:private"
    assert quota.select_codex_account(rows, "", ["private", "default"]) == "codex:private"
    assert quota.select_codex_account([], "") == ""


# ── per-seat eligibility signals ──────────────────────────────────────────────────
def _seat(tmp_path: Path, *, auth: bool = True) -> Path:
    home = tmp_path / "seat"
    home.mkdir(parents=True, exist_ok=True)
    if auth:
        (home / "auth.json").write_text("{}", encoding="utf-8")
    return home


def test_missing_auth_json_is_unknown_not_blocked(tmp_path: Path) -> None:
    """A seat we cannot measure stays runnable — refusing to try it deletes a rung."""
    row = quota._codex_seat_quota(  # noqa: SLF001
        "codex:private", "private", _seat(tmp_path, auth=False), _NOW, {}
    )
    assert row.state == quota.UNKNOWN
    assert "auth.json" in row.reason


def test_free_plan_is_risky_with_a_note_but_the_state_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plan_type == free` proves nothing about the windows — advisory only."""
    home = _seat(tmp_path)
    windows = usage.Usage(
        captured_at=_NOW,
        five_hour=usage.Window(used_percentage=10.0, resets_at=_NOW + 3600),
        seven_day=None,
        plan_type="free",
    )
    monkeypatch.setattr(usage, "read_codex_live", lambda _h: windows)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n, _h: windows)
    row = quota._codex_seat_quota("codex:private", "private", home, _NOW, {})  # noqa: SLF001
    assert row.state == quota.AVAILABLE
    assert row.risky is True
    assert row.note == "plan free — entitlement unproven"


def test_passed_renewal_date_is_a_note_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "subscription_end_map", lambda: {"codex_private": "2020-01-31"})
    monkeypatch.setattr(usage, "read_codex_live", lambda _h: None)
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n, _h: None)
    row = quota._codex_seat_quota(  # noqa: SLF001
        "codex:private", "private", _seat(tmp_path), _NOW, {}
    )
    assert row.state == quota.UNKNOWN  # no snapshot — unchanged by the note
    assert row.note == "renewal date 2020-01-31 passed"


def test_an_entitlement_block_excludes_the_seat(tmp_path: Path) -> None:
    """A recorded entitlement refusal is a cooldown like any other: not a candidate."""
    entry = {
        "blocked_until": _NOW + 86400,
        "observed_at": _NOW,
        "reason": "codex exec refused: entitlement — usage_not_included",
        "scope": "entitlement",
        "source": "codex-exec",
        "kind": quota.KIND_OBSERVED,
    }
    row = quota._codex_seat_quota(  # noqa: SLF001
        "codex:private", "private", _seat(tmp_path), _NOW, {"codex:private": entry}
    )
    assert row.state == quota.BLOCKED
    assert row.block_scope == "entitlement"
    assert not quota.codex_seat_candidates([row], "", ["private"])


# ── snapshot payload ──────────────────────────────────────────────────────────────
def test_snapshot_carries_the_ranked_seat_order_and_next_attempt(
    three_seats: SeatFixture,
) -> None:
    quota.record_block(
        "codex:private", blocked_until=int(time.time()) + 900, kind=quota.KIND_HOLD, reason="held"
    )
    snap = quota.snapshot()
    rows = snap["codex_seat_order"]
    assert [row["label"] for row in rows] == ["private", "de", "default"]
    assert [row["configured_rank"] for row in rows] == [1, 2, 3]
    assert [row["attempt_rank"] for row in rows] == [None, 1, 2]
    assert snap["codex_next_attempt"] == "codex:de"
    assert snap["best_codex_account"] == snap["codex_next_attempt"]  # v2 alias
    assert set(rows[0]) == {
        "configured_rank",
        "attempt_rank",
        "id",
        "label",
        "email",
        "state",
        "reason",
        "blocked_by",
        "resets_at",
        "windows",
        "note",
        "pinned",
    }
    assert rows[0]["state"] == quota.BLOCKED
    assert rows[1]["email"] == "de@example.org"
    assert "codex_pin" not in snap  # no pin set
    _ = three_seats  # the fixture IS the setup


def test_quota_footer_names_the_ladder_and_the_next_attempt(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    quota.record_block(
        "codex:private",
        blocked_until=int(time.time()) + 900,
        kind=quota.KIND_HOLD,
        reason="private held",
    )
    assert cli.main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "codex seats: 1 private ⛔ (hold) → 2 de ❔ → 3 default ❔" in out
    assert "next attempt: codex:de" in out
    _ = three_seats


def test_quota_footer_reports_none_eligible_with_the_earliest_reset(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    for index, pid in enumerate(("codex:private", "codex:de", "codex")):
        quota.record_block(
            pid, blocked_until=now + 600 + index * 600, kind=quota.KIND_HOLD, reason="held"
        )
    assert cli.main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "next attempt: none eligible (earliest reset" in out
    _ = three_seats


def test_quota_footer_flags_an_ignored_pin_and_a_note(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cic.save_config({"codex_home": str(three_seats.seats["de"]), "codex_home_until": None})
    monkeypatch.setattr(config, "subscription_end_map", lambda: {"codex_private": "2020-01-31"})
    assert cli.main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "pin: de (ignored: explicit order set)" in out
    assert "⚠ private: renewal date 2020-01-31 passed" in out
    assert "change: codex-in-claude order <label…>" in out


# ── the `order` CLI ───────────────────────────────────────────────────────────────
def _order(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {"labels": [], "clear": False, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_order_show_lists_every_seat_ranked(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    quota.record_block(
        "codex:private",
        blocked_until=int(time.time()) + 900,
        kind=quota.KIND_HOLD,
        reason="private held",
    )
    assert cic.cmd_order(_order()) == cic.EX_OK
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0].startswith("1  private")
    assert "⛔ blocked" in lines[0] and "hold: private held" in lines[0] and "unblocks" in lines[0]
    assert lines[1].startswith("2  de") and "← next attempt" in lines[1]
    assert lines[2].startswith("3  default")
    _ = three_seats


def test_order_set_persists_clears_the_pin_and_shows_the_table(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    cic.save_config({"codex_home": str(three_seats.seats["de"]), "codex_home_until": "2099-01-01"})
    assert cic.cmd_order(_order(labels=["de", "default", "private"])) == cic.EX_OK
    out = capsys.readouterr().out
    assert "pin cleared (order is authoritative)" in out
    assert "codex seat order: de → default → private" in out
    assert config.codex_seat_order() == ["de", "default", "private"]
    assert cic.load_config()["codex_home"] is None
    # the other config keys survived the rewrite
    assert config.load_config().codex_home_private == str(three_seats.seats["private"])


def test_order_clear_returns_to_the_canonical_order(three_seats: SeatFixture) -> None:
    assert cic.cmd_order(_order(clear=True)) == cic.EX_OK
    assert not config.codex_seat_order()
    assert quota.codex_seat_order_labels(quota._canonical_codex_homes()) == [  # noqa: SLF001
        "default",
        "private",
        "de",
    ]
    _ = three_seats


def test_order_refuses_an_unknown_label(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo would silently demote a real seat to the end of the order."""
    assert cic.cmd_order(_order(labels=["private", "nope"])) == cic.EX_USAGE
    err = capsys.readouterr().err
    assert "unknown seat label 'nope'" in err
    assert "known: default, private, de" in err
    assert config.codex_seat_order() == ["private", "de", "default"]  # unchanged
    _ = three_seats


def test_order_refuses_to_drop_unknown_config_keys(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """`save_config` re-emits only DEFAULTS keys — refuse rather than delete the rest."""
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + 'my_own_key = "keep me"\n', encoding="utf-8")
    config.invalidate_config_cache()
    assert config.unknown_config_keys() == ["my_own_key"]
    assert cic.cmd_order(_order(labels=["de"])) == cic.EX_USAGE
    assert "refusing to rewrite config.toml" in capsys.readouterr().err
    assert "my_own_key" in path.read_text(encoding="utf-8")


def test_order_json_shape(three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]) -> None:
    cic.save_config({"codex_home": str(three_seats.seats["de"]), "codex_home_until": "2099-01-01"})
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'codex_seat_order = ["private", "de", "default"]',
            'codex_seat_order = ["private", "ghost"]',
        ),
        encoding="utf-8",
    )
    config.invalidate_config_cache()
    assert cic.cmd_order(_order(json=True)) == cic.EX_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["configured"] == ["private", "ghost"]
    assert payload["order"] == ["private", "default", "de"]  # unlisted seats appended
    assert payload["unknown"] == ["ghost"]
    assert payload["next_attempt"] == "codex:private"
    assert [c["rank"] for c in payload["candidates"]] == [1, 2, 3]
    assert payload["pin"] == {"label": "de", "until": "2099-01-01", "active": False}


def test_order_show_reports_unknown_labels(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    path = three_seats.ccc_home / "command-center" / "config.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'codex_seat_order = ["private", "de", "default"]', 'codex_seat_order = ["ghost", "de"]'
        ),
        encoding="utf-8",
    )
    config.invalidate_config_cache()
    assert cic.cmd_order(_order()) == cic.EX_OK
    assert "unknown labels in config: ghost" in capsys.readouterr().out


# ── the `home` CLI under an order ─────────────────────────────────────────────────
def test_home_json_carries_order_candidates_and_pin_state(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    cic.save_config({"codex_home": str(three_seats.seats["de"]), "codex_home_until": "2099-01-01"})
    assert cic.cmd_home(argparse.Namespace(path=None, until=None, clear=False, json=True)) == (
        cic.EX_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["order"] == ["private", "de", "default"]
    assert payload["pin_active"] is False  # an explicit order makes the pin inert
    assert payload["home"] == payload["candidates"][0]["home"] == str(three_seats.seats["private"])
    assert payload["label"] == "private"
    assert payload["source"] == "codex_seat_order"


def test_home_warns_when_a_pin_is_set_under_an_order(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(
        path=str(three_seats.seats["de"]), until=None, clear=False, json=False
    )
    assert cic.cmd_home(args) == cic.EX_OK
    captured = capsys.readouterr()
    assert "IGNORED for selection" in captured.err
    assert "(pin ignored: explicit order set)" in captured.out
    assert cic.pin_active() is False


def test_home_says_no_eligible_seat_when_everything_is_held(
    three_seats: SeatFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    for pid in ("codex:private", "codex:de", "codex"):
        quota.record_block(pid, blocked_until=now + 900, kind=quota.KIND_HOLD, reason="held")
    assert cic.cmd_home(argparse.Namespace(path=None, until=None, clear=False, json=False)) == (
        cic.EX_OK
    )
    assert "(no eligible seat)" in capsys.readouterr().out
    _ = three_seats
