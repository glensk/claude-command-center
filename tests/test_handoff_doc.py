"""Handoff document validation (tp#1300): structure, size, control characters, scrub."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from scrubstub import DUMMY_VALUE, PLACEHOLDER, stub_scrubber

from command_center import handoff_doc


def _doc(**override: str) -> str:
    """A complete handoff: every required section with a one-line body."""
    parts = ["# Handoff", ""]
    for title in handoff_doc.REQUIRED_SECTIONS:
        parts += [f"## {title}", "", override.get(title, f"text for {title}"), ""]
    return "\n".join(parts)


def test_template_has_every_required_section_and_fails_unfilled() -> None:
    """The shipped template names every section; unfilled (comments only) it is empty."""
    template = handoff_doc.template()
    found = handoff_doc.sections(template)
    for title in handoff_doc.REQUIRED_SECTIONS:
        assert " ".join(title.lower().split()) in found
    errors = handoff_doc.check_structure(template)
    assert len(errors) == len(handoff_doc.REQUIRED_SECTIONS)
    assert all(e.startswith("empty section") for e in errors)


def test_valid_document_is_scrubbed_and_digested(tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    text = _doc(**{"Current state": f"token was {DUMMY_VALUE}"})
    doc = handoff_doc.validate(text, scrub_policy=stub.scrub_cmd)
    assert doc.ok, doc.errors
    assert DUMMY_VALUE not in doc.text and PLACEHOLDER in doc.text
    assert doc.labels == ("test.key",)
    assert doc.sha256 == hashlib.sha256(doc.text.encode("utf-8")).hexdigest()


def test_missing_and_empty_sections_are_named(tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    text = _doc(**{"First steps": "<!-- only a comment -->"}).replace("## tp ownership", "## x")
    doc = handoff_doc.validate(text, scrub_policy=stub.scrub_cmd)
    assert not doc.ok
    assert "missing section: ## tp ownership" in doc.errors
    assert any(e.startswith("empty section: ## First steps") for e in doc.errors)
    assert stub.calls == []  # rejected before any scrubber call


def test_oversize_is_rejected(tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    text = _doc(**{"Current state": "x" * (handoff_doc.MAX_BYTES + 1)})
    doc = handoff_doc.validate(text, scrub_policy=stub.scrub_cmd)
    assert not doc.ok and "the cap is" in doc.errors[0]


@pytest.mark.parametrize("bad", ["\x00", "\x1b[31m", "\r", "\x7f", "\x85"])
def test_control_characters_are_rejected(tmp_path: Path, bad: str) -> None:
    stub = stub_scrubber(tmp_path)
    doc = handoff_doc.validate(_doc(**{"Current state": f"a{bad}b"}), scrub_policy=stub.scrub_cmd)
    assert not doc.ok and doc.errors[0].startswith("control character")


def test_newline_and_tab_are_allowed(tmp_path: Path) -> None:
    stub = stub_scrubber(tmp_path)
    doc = handoff_doc.validate(_doc(**{"Current state": "a\tb\nc"}), scrub_policy=stub.scrub_cmd)
    assert doc.ok, doc.errors


def test_malformed_utf8_is_rejected() -> None:
    doc = handoff_doc.validate(_doc().encode("utf-8") + b"\xff\xfe")
    assert not doc.ok and "not valid UTF-8" in doc.errors[0]


@pytest.mark.parametrize("mode", ["exit3", "empty", "badutf8"])
def test_withheld_scrub_rejects(tmp_path: Path, mode: str) -> None:
    stub = stub_scrubber(tmp_path, mode)
    doc = handoff_doc.validate(_doc(), scrub_policy=stub.scrub_cmd)
    assert not doc.ok and doc.errors[0].startswith("credential scrub withheld")


def test_no_scrubber_configured_fails_closed() -> None:
    doc = handoff_doc.validate(_doc(), scrub_policy="")
    assert not doc.ok and "no scrubber configured" in doc.errors[0]
