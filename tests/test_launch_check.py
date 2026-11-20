"""The launch credential check's own key, ``launch_secret_check`` (tp#2266).

The check (``futuresync._credential_tripwire``) used to be armed only by a mirror switch.
Retiring the mirrors must not silently disarm it, so ONE formula
(:func:`config.launch_secret_check_armed`) arms the tripwire, ``ccc doctor``'s
"launch secret check" line and its "launch check → scrubber" row: the dedicated key alone
(the mirror arming terms left with the mirrors, B10).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scrubstub import DUMMY_VALUE, stub_scrubber

from command_center import config, doctor, external_deps, futuresync, scrub
from command_center.models import Session

LAUNCH_LABEL = "launch check → scrubber"


def _draft(prompt: str) -> Session:
    return Session(session_id="d", cwd="/repo", aim="Rotate the deploy key", prompt=prompt)


def _features(cfg: config.Config) -> dict[str, doctor.Check]:
    return {c.label: c for c in doctor._section_features(cfg).checks}  # pylint: disable=protected-access


@pytest.mark.parametrize(
    ("overrides", "armed"),
    [
        ({}, False),
        ({"launch_secret_check": True}, True),
    ],
)
def test_arming_formula(overrides: dict[str, bool], armed: bool) -> None:
    cfg = config.Config(**overrides)  # type: ignore[arg-type]
    assert config.launch_secret_check_armed(cfg) is armed


def test_key_is_inert_by_default() -> None:
    assert config.DEFAULTS["launch_secret_check"] is False
    assert "launch_secret_check" in config.INERT_DEFAULT_KEYS
    assert config.Config().launch_secret_check is False


def test_key_is_read_from_config_toml() -> None:
    """``load_config`` keeps the key (an unknown key would be silently dropped)."""
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("launch_secret_check = true\n", encoding="utf-8")
    config.invalidate_config_cache()
    assert config.load_config().launch_secret_check is True


def test_tripwire_armed_by_key_with_all_mirrors_off(tmp_path: Path) -> None:
    """The key alone arms it: a leak in the prompt is reported, the checker was asked."""
    stub = stub_scrubber(tmp_path, "check")
    cfg = config.Config(launch_secret_check=True, mirror_scrub_cmd=stub.scrub_cmd)
    assert not hasattr(cfg, "mirror_running")  # the mirror switches are gone (B10)

    verdict = futuresync._credential_tripwire(cfg, _draft(f"use {DUMMY_VALUE}"))  # pylint: disable=protected-access

    assert verdict is not None and verdict.state == scrub.LEAK
    assert stub.calls == ["check"]
    clean = futuresync._credential_tripwire(cfg, _draft("nothing secret"))  # pylint: disable=protected-access
    assert clean is not None and clean.state == scrub.CLEAN


def test_tripwire_disarmed_with_key_off(tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path, "check")
    cfg = config.Config(mirror_scrub_cmd=stub.scrub_cmd)  # key off, mirrors off
    assert futuresync._credential_tripwire(cfg, _draft(f"use {DUMMY_VALUE}")) is None  # pylint: disable=protected-access
    assert stub.calls == []  # never even resolved


def test_tripwire_armed_key_with_unresolvable_scrubber_is_degraded() -> None:
    """Fail closed: armed but no scrubber → DEGRADED (the launch is refused)."""
    cfg = config.Config(launch_secret_check=True, mirror_scrub_cmd="/nonexistent/x scrub")
    verdict = futuresync._credential_tripwire(cfg, _draft("harmless"))  # pylint: disable=protected-access
    assert verdict is not None and verdict.state == scrub.DEGRADED


def test_doctor_line_armed_and_disarmed() -> None:
    labels = _features(config.Config())
    assert labels["launch secret check: disarmed"].status == doctor.NA
    assert "launch secret check: armed" not in labels

    on = _features(config.Config(launch_secret_check=True, mirror_scrub_cmd=""))
    assert on["launch secret check: armed"].status == doctor.OK
    assert "launch_secret_check = true" in on["launch secret check: armed"].detail


def test_doctor_rendered_text_carries_the_grep_string() -> None:
    """B5's precondition greps ``ccc doctor`` for exactly this substring."""
    section = doctor.Section("Features & dependencies")
    section.checks.append(
        doctor._launch_secret_check_line(config.Config(launch_secret_check=True))  # pylint: disable=protected-access
    )
    assert "launch secret check: armed" in doctor.render(doctor.Report([section]))


def test_doctor_scrubber_fail_with_key_on_and_mirrors_off() -> None:
    """An unresolvable scrubber is ❌ while the key arms the check."""
    cfg = config.Config(launch_secret_check=True, mirror_scrub_cmd="/nonexistent/x scrub")
    assert _features(cfg)[LAUNCH_LABEL].status == doctor.FAIL
    assert doctor.build_report(cfg).exit_code == 1


def test_doctor_scrubber_ok_with_key_on_and_mirrors_off(tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    cfg = config.Config(launch_secret_check=True, mirror_scrub_cmd=stub.scrub_cmd)
    check = _features(cfg)[LAUNCH_LABEL]
    assert check.status == doctor.OK and check.detail == str(stub.path)


def test_doctor_scrubber_disabled_with_everything_off() -> None:
    assert _features(config.Config())[LAUNCH_LABEL].status == doctor.NA


def test_external_deps_hint_names_the_key() -> None:
    hint = external_deps.EXTERNAL_DEPS["secret-broker-client.py"].install_hint
    assert "launch_secret_check" in hint
