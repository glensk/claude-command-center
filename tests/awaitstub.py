"""Shared fixtures for the ``ccc await`` tests: a seeded store and a canned
``zoho-api.py -i`` answer, so the test modules do not each re-declare them."""

from __future__ import annotations

import json
from pathlib import Path

from command_center.checks import StructuredResult
from command_center.store import Store


def seeded_store(tmp_path: Path, cwd: str = "/repo") -> Store:
    """A fresh ``state.db`` under *tmp_path* holding the one session row ``s1``."""
    store = Store(tmp_path / "state.db")
    store.ensure("s1", cwd=cwd)
    return store


def zoho_result(
    *,
    fired: bool,
    when: str = "2027-01-15T08:00:00.000Z",
    sender: str = "r@x.org",
    summary: str = "yes",
) -> StructuredResult:
    """A successful ``zoho-api.py -i 209`` probe whose newest inbound thread is ``9``."""
    return StructuredResult(
        exit=0,
        stdout=json.dumps(
            {
                "schema_version": 1,
                "ticket": "209",
                "newest_inbound": {"id": "9", "time": when, "from": sender, "summary": summary},
                "watermark": "2:9",
                "fired": fired,
            }
        ),
    )
