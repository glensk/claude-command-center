#!/usr/bin/env python3
"""Replay stored handoff facts snapshots through the facts-only compose path (tp#1835).

For each snapshot id it loads ``<app_home>/handoffs/facts/<sid>/<id>*.json``, writes the
code-generated model part (``handoff_capped.model_part(use_router=False)``) and runs
``handoff_doc.compose_checked(generated=True)``. A generated part must always fit: any
refusal or a text over ``MAX_BYTES`` is a failure. With ``-w`` it also composes a
model-WRITTEN part of that many body bytes and reports whether it fits or the refusal
(informational only: a written part may be refused with the room it has).

Read-only: nothing is written. Snapshots that are absent are skipped (exit 0).

Usage:
  tools/replay_handoff_snapshots.py [-w BYTES] [ID ...]

Arguments:
  ID                    Snapshot id prefixes (default: e328073ccb4c f8010cffdafe).

Options:
  -w, --written BYTES   Also compose a model-written part with this many body bytes.
  -h, --help            Show this help and exit.

Exit status: 0 all present snapshots fit (or none present), 1 a generated part was refused.

Examples:
  uv run tools/replay_handoff_snapshots.py
  uv run tools/replay_handoff_snapshots.py -w 9000 e328073ccb4c
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from command_center import config, handoff_capped, handoff_doc, handoff_facts

DEFAULT_IDS = ("e328073ccb4c", "f8010cffdafe")


def _find(snapshot_id: str) -> Path | None:
    root = config.app_home() / "handoffs" / "facts"
    hits = sorted(root.glob(f"*/{snapshot_id}*.json"))
    return hits[0] if hits else None


def _transcript(doc: dict[str, Any]) -> Path | None:
    for fact in doc.get("facts", []):
        if fact.get("kind") == "transcript":
            path = (fact.get("fields") or {}).get("path")
            return Path(path) if path else None
    return None


def _kb(size: int) -> str:
    return f"{size / 1024:.1f} KB"


def _written(nbytes: int) -> handoff_doc.ModelPart:
    third, rest = divmod(nbytes, len(handoff_doc.MODEL_SECTIONS))
    sizes = [third + rest] + [third] * (len(handoff_doc.MODEL_SECTIONS) - 1)
    text = "".join(
        f"## {title}\n\n{'w' * n}\n\n"
        for title, n in zip(handoff_doc.MODEL_SECTIONS, sizes, strict=True)
    )
    return handoff_doc.validate_model_part(text)


def _replay(snapshot_id: str, path: Path, written: int | None) -> bool:
    doc = json.loads(path.read_text(encoding="utf-8"))
    sid = str(doc.get("session_id") or path.parent.name)
    text = handoff_capped.model_part(
        sid, doc, _transcript(doc), handoff_facts.Scrubber(), use_router=False
    )
    model = handoff_doc.validate_model_part(text)
    model_path = str(path.with_suffix(".model.md"))
    result, composed = handoff_doc.compose_checked(
        model, doc, ref=sid, model_path=model_path, generated=True
    )
    size = len(result.text.encode()) if result.ok else 0
    ok = result.ok and size <= handoff_doc.MAX_BYTES
    if ok:
        print(
            f"✅ {snapshot_id} facts-only: {size} B ({_kb(size)}) at tier {composed.level}"
            f"{', model cut' if composed.model_cut else ''}"
        )
    else:
        print(f"❌ {snapshot_id} facts-only refused: {'; '.join(result.errors)}")
    if written is not None:
        part = _written(written)
        if not part.ok:
            print(f"   {snapshot_id} written {written} B invalid: {'; '.join(part.errors)}")
            return ok
        res, comp = handoff_doc.compose_checked(part, doc, ref=sid, model_path=model_path)
        if res.ok:
            wsize = len(res.text.encode())
            print(
                f"   {snapshot_id} written {written} B: fits, {wsize} B ({_kb(wsize)})"
                f" at tier {comp.level}"
            )
        else:
            print(f"   {snapshot_id} written {written} B refused: {'; '.join(res.errors)}")
    return ok


def main(argv: list[str] | None = None) -> int:
    """Replay the given snapshot ids; 1 when a generated part was refused."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("ids", nargs="*", metavar="ID", help="snapshot id prefixes")
    parser.add_argument(
        "-w", "--written", type=int, metavar="BYTES", help="also compose a written part"
    )
    args = parser.parse_args(argv)
    failed = False
    for snapshot_id in args.ids or DEFAULT_IDS:
        path = _find(snapshot_id)
        if path is None:
            print(f"skipped {snapshot_id}: no snapshot under the app home")
            continue
        failed |= not _replay(snapshot_id, path, args.written)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
