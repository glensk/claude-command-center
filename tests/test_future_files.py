"""Unit tests for the future-job file format (parse / serialize / validate / helpers)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from command_center import config, future_files, models
from command_center.future_files import ParsedJob

_UUID = "3a8b7c12-1234-5678-9abc-def012345678"


def _pin_two_accounts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Override the autouse single-account pin with two accounts (private + work)."""
    dirs = {"private": tmp_path / "private", "work": tmp_path / "work"}
    monkeypatch.setattr(config, "claude_config_dirs", lambda: dict(dirs))
    return dirs


def _roundtrip(job: ParsedJob, repo_options: list[str] | None = None) -> ParsedJob:
    text = future_files.serialize(
        session_id=job.session_id,
        aim=job.aim,
        status=job.status,
        repo=job.repo,
        job_type=job.job_type,
        llm_overseer=job.llm_overseer,
        llm_exec=job.llm_exec,
        start_when=job.start_when,
        depends_on=job.depends_on,
        deadline=job.deadline,
        created=job.created,
        prompt=job.prompt,
        repo_options=repo_options,
    )
    return future_files.parse_job_file(text)


# ---- round-trip ----------------------------------------------------------
def test_roundtrip_full_job() -> None:
    job = ParsedJob(
        session_id=_UUID,
        status="ready",
        repo="home/claude-command-center",
        job_type="codex",
        llm_overseer="fable-5",
        llm_exec="sonnet-5",
        start_when="during holidays",
        deadline="2026-07-10",
        created="2026-07-02",
        aim="Migrate Zendesk tickets to Zoho",
        prompt="Do the full migration with retries.",
    )
    assert _roundtrip(job, repo_options=["home/other", "sdsc/zoho"]) == job


def test_roundtrip_minimal_job() -> None:
    job = ParsedJob(
        session_id=_UUID,
        status="draft",  # serialize's default
        repo="",
        job_type="claude",  # serialize's default
        llm_overseer="opus-5",  # serialize's default
        llm_exec="opus-5",  # serialize's default
        start_when="",
        deadline="",
        created="",
        aim="Ship the feature",
        prompt=None,
    )
    assert _roundtrip(job) == job


def test_roundtrip_multiline_aim_and_prompt() -> None:
    job = ParsedJob(
        session_id=_UUID,
        status="draft",
        repo="home/ccc",
        job_type="claude",
        llm_overseer="opus-4.8",
        llm_exec="opus-4.8",
        start_when="",
        deadline="",
        created="",
        aim="Line one of the aim\n\nLine two after a blank",
        prompt="Step 1\n\nStep 2\nStep 3",
    )
    assert _roundtrip(job) == job


def test_empty_prompt_roundtrips_to_none() -> None:
    job = ParsedJob(
        session_id=_UUID,
        status="draft",
        repo="home/ccc",
        job_type="claude",
        llm_overseer="opus-4.8",
        llm_exec="opus-4.8",
        start_when="",
        deadline="",
        created="",
        aim="Ship it",
        prompt=None,
    )
    text = future_files.serialize(
        session_id=job.session_id, aim=job.aim, repo=job.repo, prompt=None
    )
    # The # Prompt section is present but empty in the file, and parses back to None.
    assert "# Prompt" in text
    assert future_files.parse_job_file(text).prompt is None
    # A whitespace-only prompt is likewise treated as empty.
    ws = future_files.serialize(session_id=job.session_id, aim=job.aim, prompt="   \n  ")
    assert future_files.parse_job_file(ws).prompt is None


def test_roundtrip_start_when_with_quotes() -> None:
    job = ParsedJob(
        session_id=_UUID,
        status="draft",
        repo="home/ccc",
        job_type="claude",
        llm_overseer="opus-4.8",
        llm_exec="opus-4.8",
        start_when='he said "go" now',
        deadline="",
        created="",
        aim="do x",
        prompt=None,
    )
    assert _roundtrip(job) == job


# ---- llm_overseer / llm_exec ---------------------------------------------
def test_serialize_emits_llm_keys_and_default_is_opus() -> None:
    # Defaults: serialize with no llm args emits both keys as the active default.
    text = future_files.serialize(session_id=_UUID, aim="x", repo="home/ccc")
    assert 'llm_overseer: "opus-5"' in text
    assert 'llm_exec: "opus-5"' in text
    job = future_files.parse_job_file(text)
    assert job.llm_overseer == "opus-5" and job.llm_exec == "opus-5"


def test_missing_llm_keys_parse_to_default() -> None:
    # A pre-feature file with no llm_* frontmatter keys parses to the active default.
    text = (
        "---\n"
        f'session_id: "{_UUID}"\n'
        'status: "registered"\n'
        'repo: "home/ccc"\n'
        'job_type: "claude"\n'
        "---\n\n## AIM\n\nDo it\n\n## Prompt\n\n"
    )
    job = future_files.parse_job_file(text)
    assert job.llm_overseer == "opus-5" and job.llm_exec == "opus-5"


def test_controls_block_lists_model_selects() -> None:
    text = future_files.serialize(session_id=_UUID, aim="x", repo="home/ccc")
    # Two inlineSelects bound to the model fields, options from LLM_CHOICES. Derive the
    # expected string from LLM_CHOICES rather than hardcoding it, so adding a model does
    # not silently break this test (it did when opus-5 was added).
    llm_opts = ", ".join(f"option({choice})" for choice in models.LLM_CHOICES)
    assert f"inlineSelect({llm_opts}):llm_overseer]" in text
    assert f"inlineSelect({llm_opts}):llm_exec]" in text
    assert "fable-5" not in llm_opts


def test_controls_block_labels_every_select() -> None:
    # Each Controls widget carries a bold label so the boxes are identifiable.
    text = future_files.serialize(session_id=_UUID, aim="x", repo="home/ccc")
    for label, key in [
        ("**status**", ":status]"),
        ("**job type**", ":job_type]"),
        ("**overseer**", ":llm_overseer]"),
        ("**executor**", ":llm_exec]"),
        ("**repo**", ":repo]"),
    ]:
        assert label in text
        assert text.index(label) < text.index(key)


def test_controls_block_lists_combined_repo_options() -> None:
    # ONE repo box with the full <cat>/<repo> options, own repo first, deduped in order.
    text = future_files.serialize(
        session_id=_UUID,
        aim="x",
        repo="home/ccc",
        repo_options=["infra/backup", "home/ccc", "sdsc/backup"],
    )
    assert "inlineSelect(option(home/ccc), option(infra/backup), option(sdsc/backup)):repo]" in text


# ---- start-job button ----------------------------------------------------
def test_serialize_emits_start_button_between_frontmatter_and_aim() -> None:
    text = future_files.serialize(session_id=_UUID, aim="Ship it", repo="home/ccc")
    # The Meta Bind button block, with the exact Shell Commands command id.
    assert "```meta-bind-button" in text
    assert 'label: "▶ Start this job"' in text
    assert "style: primary" in text
    assert f"command: {future_files._START_JOB_COMMAND_ID}" in text
    assert future_files._START_JOB_COMMAND_ID == (
        "obsidian-shellcommands:shell-command-ccc-start-job-from-file"
    )
    # It sits AFTER the frontmatter and BEFORE the AIM section (so the parser ignores it).
    fm_end = text.index("\n---\n", 3) + len("\n---\n")
    assert fm_end < text.index("```meta-bind-button") < text.index("## AIM")


def test_button_is_ignored_by_parser_roundtrip_stable() -> None:
    job = ParsedJob(
        session_id=_UUID,
        status="draft",
        repo="home/ccc",
        job_type="claude",
        llm_overseer="opus-4.8",
        llm_exec="opus-4.8",
        start_when="",
        deadline="",
        created="",
        aim="Ship the feature",
        prompt="run it",
    )
    # The button is emitted but does not leak into any parsed field.
    parsed = _roundtrip(job)
    assert parsed == job
    assert "Start this job" not in parsed.aim
    assert parsed.prompt is not None and "Start this job" not in parsed.prompt


def test_pad_template_has_no_button() -> None:
    # An empty session_id (the capture-pad template) must stay button-less: nothing to launch.
    pad = future_files.serialize(session_id="", aim="", repo="")
    assert "meta-bind-button" not in pad
    assert "Start this job" not in pad
    # A whitespace-only session_id is likewise treated as empty.
    assert "meta-bind-button" not in future_files.serialize(session_id="   ", aim="x")


def test_button_survives_error_block_stable_order() -> None:
    text = future_files.serialize(
        session_id=_UUID, aim="Ship it", status="draft", repo="home/ccc", prompt="run it"
    )
    once = future_files.upsert_error_block(text, ["repo bad", "AIM empty"])
    twice = future_files.upsert_error_block(once, ["repo bad", "AIM empty"])
    assert once == twice  # still a fixed point with the button present
    # Order: frontmatter -> error callout -> button -> AIM.
    assert once.index("ccc-sync-error") < once.index("meta-bind-button") < once.index("## AIM")
    # Clearing the error leaves the button (and user content) intact.
    cleared = future_files.clear_error_block(once)
    assert "ccc-sync-error" not in cleared
    assert "meta-bind-button" in cleared and "Ship it" in cleared


# ---- slugify / display_hash / filename -----------------------------------
def test_slugify_edge_cases() -> None:
    assert (
        future_files.slugify("Migrate Zendesk tickets to Zoho") == "migrate-zendesk-tickets-to-zoho"
    )
    assert (
        future_files.slugify("one two three four five six seven eight")
        == "one-two-three-four-five-six"  # capped at 6 words
    )
    assert future_files.slugify("Café résumé") == "cafe-resume"  # unicode transliterated
    assert future_files.slugify("Fix: the (login) bug!!") == "fix-the-login-bug"  # punctuation
    assert future_files.slugify("") == "job"  # empty fallback
    assert future_files.slugify("@#$% ***") == "job"  # all-symbol fallback
    assert future_files.slugify("a b c d", max_words=2) == "a-b"  # honours max_words


def test_display_hash() -> None:
    assert future_files.DISPLAY_HASH_LEN == 4
    assert future_files.display_hash(_UUID) == "3a8b"
    assert future_files.display_hash(uuid.UUID(_UUID).hex) == "3a8b"
    fallback = future_files.display_hash("zzzz")  # no hex digits at all
    assert len(fallback) == 4 and fallback == "0000"


def test_job_filename() -> None:
    assert future_files.job_filename(_UUID, "Ship the feature") == "ship-the-feature-3a8b.md"


# ---- validate ------------------------------------------------------------
def _valid_job(repo: str) -> ParsedJob:
    return ParsedJob(
        session_id=str(uuid.uuid4()),
        status="ready",
        repo=repo,
        job_type="claude",
        llm_overseer="opus-4.8",
        llm_exec="opus-4.8",
        start_when="",
        deadline="",
        created="",
        aim="Do the thing",
        prompt=None,
    )


def test_validate_ok_and_failures(tmp_path: Path) -> None:
    git_base = tmp_path / "git"
    (git_base / "home" / "ccc").mkdir(parents=True)
    good = _valid_job("home/ccc")
    assert future_files.validate(good, git_base) == []

    assert any(
        "UUID" in e for e in future_files.validate(replace(good, session_id="nope"), git_base)
    )
    assert any("AIM" in e for e in future_files.validate(replace(good, aim="   "), git_base))
    assert any(
        "job_type" in e for e in future_files.validate(replace(good, job_type="wat"), git_base)
    )
    assert any(
        "llm_overseer" in e
        for e in future_files.validate(replace(good, llm_overseer="gpt-9"), git_base)
    )
    assert any(
        "llm_exec" in e for e in future_files.validate(replace(good, llm_exec="bogus"), git_base)
    )
    assert any(
        "repo" in e for e in future_files.validate(replace(good, repo="home/missing"), git_base)
    )
    # A one-level or three-level repo is not <cat>/<repo>.
    assert future_files.validate(replace(good, repo="home"), git_base)
    assert future_files.validate(replace(good, repo="a/b/c"), git_base)


def test_validate_absolute_repo_fallback(tmp_path: Path) -> None:
    git_base = tmp_path / "git"
    git_base.mkdir()
    outside = tmp_path / "elsewhere" / "proj"
    outside.mkdir(parents=True)
    # An absolute path to an existing dir (outside <repo_root>/<cat>/<repo>) is valid.
    assert future_files.validate(_valid_job(str(outside)), git_base) == []
    # An absolute path that does not exist is invalid.
    assert future_files.validate(_valid_job(str(tmp_path / "nope")), git_base)


# ---- repo <-> cwd mapping ------------------------------------------------
def test_cwd_repo_inverse_under_git_base(tmp_path: Path) -> None:
    git_base = tmp_path / "git"
    cwd = git_base / "sdsc" / "zoho"
    cwd.mkdir(parents=True)
    assert future_files.cwd_to_repo(str(cwd), git_base) == "sdsc/zoho"
    assert future_files.repo_to_cwd("sdsc/zoho", git_base) == cwd
    label = future_files.cwd_to_repo(str(cwd), git_base)
    assert future_files.repo_to_cwd(label, git_base) == cwd


def test_cwd_repo_inverse_outside_git_base(tmp_path: Path) -> None:
    git_base = tmp_path / "git"
    git_base.mkdir()
    outside = tmp_path / "vault" / "proj"
    outside.mkdir(parents=True)
    label = future_files.cwd_to_repo(str(outside), git_base)
    assert label == str(outside)  # absolute path string, not <cat>/<repo>
    assert future_files.repo_to_cwd(label, git_base) == outside
    # git_base itself is 0 levels under → absolute-path label.
    assert future_files.cwd_to_repo(str(git_base), git_base) == str(git_base)


# ---- rel_dir_for ---------------------------------------------------------
def test_rel_dir_for(tmp_path: Path) -> None:
    git_base = tmp_path / "git"
    assert future_files.rel_dir_for("home/ccc", git_base) == "home/ccc"
    other = future_files.rel_dir_for("/some/outside/proj", git_base)
    assert other == "other/proj"
    assert not other.startswith("_")  # never underscore-prefixed (scan skips _ dirs)


# ---- obsidian_uri --------------------------------------------------------
def test_obsidian_uri_encodes_spaces_keeps_slashes() -> None:
    uri = future_files.obsidian_uri("future/home/ccc/3a8b my job.md", vault="my-vault")
    assert uri.startswith("obsidian://open?vault=my-vault&file=")
    assert "future/home/ccc" in uri  # slashes preserved
    assert "%20" in uri and " " not in uri  # spaces encoded


def test_vault_name_resolution() -> None:
    # explicit vault_name wins; otherwise it derives from the basename of vault_root.
    assert future_files.vault_name(config.Config(vault_name="notes")) == "notes"
    assert future_files.vault_name(config.Config(vault_name="", vault_root="~/my-vault")) == (
        "my-vault"
    )


# ---- content_hash --------------------------------------------------------
def test_content_hash_stable_and_differs() -> None:
    assert future_files.content_hash("abc") == future_files.content_hash("abc")
    assert future_files.content_hash("abc") != future_files.content_hash("abd")
    assert len(future_files.content_hash("x")) == 64  # sha256 hexdigest


# ---- managed error callout ----------------------------------------------
def test_upsert_error_block_idempotent_and_preserves_content() -> None:
    text = future_files.serialize(
        session_id=_UUID, aim="Ship it", status="draft", repo="home/ccc", prompt="run the thing"
    )
    errors = ["repo 'x' is not valid", "AIM is empty"]
    once = future_files.upsert_error_block(text, errors)
    twice = future_files.upsert_error_block(once, errors)
    assert once == twice  # fixed point — no launchd retrigger loop
    assert 'status: "error"' in once  # frontmatter status flipped
    assert "> [!error]" in once
    assert "repo 'x' is not valid" in once and "AIM is empty" in once
    assert "Ship it" in once and "run the thing" in once  # user content preserved

    cleared = future_files.clear_error_block(once)
    assert "> [!error]" not in cleared and "ccc-sync-error" not in cleared
    assert "Ship it" in cleared and "run the thing" in cleared  # user content still there
    assert future_files.clear_error_block(cleared) == cleared  # clearing again is a no-op


def test_upsert_error_block_replaces_not_duplicates() -> None:
    text = future_files.serialize(session_id=_UUID, aim="x", status="draft", repo="home/ccc")
    once = future_files.upsert_error_block(text, ["first error"])
    updated = future_files.upsert_error_block(once, ["different error"])
    assert updated.count(future_files._ERR_START) == 1  # single managed block
    assert "different error" in updated and "first error" not in updated


def test_roundtrip_start_date() -> None:
    job = ParsedJob(
        session_id=_UUID,
        status="ready",
        repo="home/claude-command-center",
        job_type="claude",
        llm_overseer="opus-4.8",
        llm_exec="opus-4.8",
        start_when="return from Slovenia",
        deadline="",
        created="2026-07-03",
        aim="Re-enable FileVault",
        prompt=None,
        start_date="2026-08-11",
    )
    text = future_files.serialize(
        session_id=job.session_id,
        aim=job.aim,
        status=job.status,
        repo=job.repo,
        job_type=job.job_type,
        llm_overseer=job.llm_overseer,
        llm_exec=job.llm_exec,
        start_when=job.start_when,
        start_date=job.start_date,
        deadline=job.deadline,
        created=job.created,
        prompt=job.prompt,
    )
    parsed = future_files.parse_job_file(text)
    assert parsed.start_date == "2026-08-11"
    assert parsed == job


def test_roundtrip_depends_on() -> None:
    job = ParsedJob(
        session_id=_UUID,
        status="ready",
        repo="home/claude-command-center",
        job_type="claude",
        llm_overseer="opus-5",
        llm_exec="opus-5",
        start_when="",
        deadline="",
        created="2026-07-12",
        aim="Ship the dependent feature",
        prompt=None,
        depends_on="11111111-2222-3333-4444-555555555555",
    )
    text = future_files.serialize(
        session_id=job.session_id,
        aim=job.aim,
        status=job.status,
        repo=job.repo,
        depends_on=job.depends_on,
        created=job.created,
    )
    # Emitted verbatim in the frontmatter, and it round-trips.
    assert 'depends_on: "11111111-2222-3333-4444-555555555555"' in text
    assert future_files.parse_job_file(text).depends_on == job.depends_on
    assert _roundtrip(job) == job


def test_depends_on_key_absent_when_empty() -> None:
    # A dependency-less job emits NO depends_on key (byte-stable for existing files).
    text = future_files.serialize(session_id=_UUID, aim="x", repo="home/ccc")
    assert "depends_on:" not in text
    assert future_files.parse_job_file(text).depends_on == ""


def test_validate_depends_on(tmp_path: Path) -> None:
    (tmp_path / "home" / "repo").mkdir(parents=True)
    job = ParsedJob(
        session_id=_UUID,
        status="ready",
        repo="home/repo",
        job_type="claude",
        llm_overseer="opus-5",
        llm_exec="opus-5",
        start_when="",
        deadline="",
        created="",
        aim="Do it",
        prompt=None,
    )
    # A malformed (non-UUID) dependency is rejected.
    errors = future_files.validate(replace(job, depends_on="not-a-uuid"), tmp_path)
    assert any("depends_on" in e for e in errors)
    # An UNKNOWN but well-formed UUID is ALLOWED (registration order must not matter).
    assert not future_files.validate(
        replace(job, depends_on="99999999-8888-7777-6666-555555555555"), tmp_path
    )
    # No dependency is fine.
    assert not future_files.validate(replace(job, depends_on=""), tmp_path)


def test_validate_rejects_malformed_start_date(tmp_path: Path) -> None:
    (tmp_path / "home" / "repo").mkdir(parents=True)
    job = ParsedJob(
        session_id=_UUID,
        status="ready",
        repo="home/repo",
        job_type="claude",
        llm_overseer="opus-4.8",
        llm_exec="opus-4.8",
        start_when="",
        deadline="",
        created="",
        aim="Do it",
        prompt=None,
        start_date="mid august",
    )
    errors = future_files.validate(job, tmp_path)
    assert any("start_date" in e for e in errors)
    # A valid ISO date (or none at all) passes.
    assert not future_files.validate(replace(job, start_date="2026-08-11"), tmp_path)
    assert not future_files.validate(replace(job, start_date=""), tmp_path)


# ---- done / delete / restore buttons ---------------------------------------
def test_serialize_emits_action_button_row() -> None:
    text = future_files.serialize(session_id=_UUID, aim="Ship it", repo="home/ccc")
    for command_id in (
        future_files._START_JOB_COMMAND_ID,
        future_files._DONE_JOB_COMMAND_ID,
        future_files._DELETE_JOB_COMMAND_ID,
    ):
        assert f"command: {command_id}" in text
    # Three hidden definitions + ONE inline row so the buttons sit side by side.
    assert text.count("hidden: true") == 3
    assert "`BUTTON[start-job, done-job, delete-job]`" in text
    assert future_files._RESTORE_JOB_COMMAND_ID not in text


def test_serialize_deleted_swaps_restore_button_and_stamps_date() -> None:
    text = future_files.serialize(
        session_id=_UUID,
        aim="Ship it",
        repo="home/ccc",
        status="deleted",
        deleted="2026-07-03",
    )
    # A trashed file gets the single restore button — none of the action row.
    assert f"command: {future_files._RESTORE_JOB_COMMAND_ID}" in text
    assert future_files._START_JOB_COMMAND_ID not in text
    assert future_files._DONE_JOB_COMMAND_ID not in text
    assert future_files._DELETE_JOB_COMMAND_ID not in text
    assert "BUTTON[" not in text
    assert 'deleted: "2026-07-03"' in text
    parsed = future_files.parse_job_file(text)
    assert parsed.status == "deleted"
    assert parsed.deleted == "2026-07-03"


def test_deleted_key_absent_for_live_jobs() -> None:
    # No churn for normal files: the key appears only when a job is trashed.
    text = future_files.serialize(session_id=_UUID, aim="x", repo="home/ccc")
    assert "deleted:" not in text
    assert future_files.parse_job_file(text).deleted == ""


# ---- account (launch/billing account) ------------------------------------
def test_controls_block_has_account_select_in_multi_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pin_two_accounts(monkeypatch, tmp_path)
    text = future_files.serialize(session_id=_UUID, aim="x", repo="home/ccc", account="work")
    # The account select lists both configured labels, and sits between executor and repo.
    assert "**account**" in text
    assert "inlineSelect(option(private), option(work)):account]" in text
    assert text.index(":llm_exec]") < text.index(":account]") < text.index(":repo]")
    # Passing account="work" emits the frontmatter key.
    assert 'account: "work"' in text
    assert future_files.parse_job_file(text).account == "work"


def test_controls_block_no_account_select_single_account() -> None:
    # Default (autouse single-account) fixtures → no account select line, no account key.
    text = future_files.serialize(session_id=_UUID, aim="x", repo="home/ccc")
    assert "**account**" not in text
    assert ":account]" not in text
    assert "\naccount:" not in text


def test_validate_rejects_unknown_account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _pin_two_accounts(monkeypatch, tmp_path)
    git_base = tmp_path / "git"
    (git_base / "home" / "ccc").mkdir(parents=True)
    good = _valid_job("home/ccc")
    errs = future_files.validate(replace(good, account="nosuch"), git_base)
    assert any(
        "not a configured account label" in e and "private" in e and "work" in e for e in errs
    )
    # Empty (= the default account) and a configured label are both valid.
    assert future_files.validate(replace(good, account=""), git_base) == []
    assert future_files.validate(replace(good, account="work"), git_base) == []


def test_launch_key_roundtrip_and_requested() -> None:
    text = future_files.serialize(
        session_id="11111111-2222-3333-4444-555555555555", aim="Do the thing"
    )
    assert "launch: false" in text  # bare bool → Obsidian renders a checkbox
    job = future_files.parse_job_file(text)
    assert job.launch == "" and not future_files.launch_requested(job)
    flipped = future_files.parse_job_file(text.replace("launch: false", "launch: true"))
    assert future_files.launch_requested(flipped)
    # Other truthy spellings a mobile editor might produce work too.
    for raw in ("yes", "1", "on", "True"):
        assert future_files.launch_requested(
            future_files.parse_job_file(text.replace("launch: false", f"launch: {raw}"))
        )
