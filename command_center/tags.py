#!/usr/bin/env python3
"""Typed, colored ``@tags`` for next-step / blocked text.

A tag has a *type* (e.g. ``people``, ``place``, ``status``) and each type maps to
a Rich style. Definitions live in ``~/.claude/command-center/tags.toml`` and are
seeded with sensible defaults on first use:

    @susi → people (yellow)        @home/@work/@galaxus/@amazon → place (blue)
    @waiting → status (green)

Unknown tags render in a warning style so an accidental, unassigned tag is
visually obvious (and can then be added with ``ccc tag add``).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import tomllib
from functools import lru_cache
from pathlib import Path

from . import config

UNKNOWN_STYLE = "bold red"  # flags an @tag that is not in the registry

_DEFAULT_TYPES: dict[str, str] = {
    "people": "yellow",
    "place": "#5599ff",
    "status": "bold green",
}
_DEFAULT_TAGS: dict[str, str] = {
    "susi": "people",
    "home": "place",
    "work": "place",
    "galaxus": "place",
    "amazon": "place",
    "waiting": "status",
}


def _path() -> Path:
    return config.app_home() / "tags.toml"


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(type_map: name->style, tag_map: name->type)``; seed defaults if absent."""
    path = _path()
    if not path.exists():
        save(dict(_DEFAULT_TYPES), dict(_DEFAULT_TAGS))
        return (dict(_DEFAULT_TYPES), dict(_DEFAULT_TAGS))
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return (dict(_DEFAULT_TYPES), dict(_DEFAULT_TAGS))
    type_map = {str(k): str(v) for k, v in (data.get("types") or {}).items()}
    tag_map = {str(k).lower(): str(v) for k, v in (data.get("tags") or {}).items()}
    return (type_map or dict(_DEFAULT_TYPES), tag_map or dict(_DEFAULT_TAGS))


def save(type_map: dict[str, str], tag_map: dict[str, str]) -> None:
    """Persist the tag registry to ``tags.toml`` and refresh the cache."""
    config.app_home().mkdir(parents=True, exist_ok=True)
    lines = ["[types]"]
    lines += [f'{name} = "{style}"' for name, style in sorted(type_map.items())]
    lines += ["", "[tags]"]
    lines += [f'{name} = "{type_name}"' for name, type_name in sorted(tag_map.items())]
    _path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    _load.cache_clear()


def tag_style(name: str) -> str | None:
    """Rich style for a tag *name* (without ``@``), or ``None`` if unknown."""
    type_map, tag_map = _load()
    type_name = tag_map.get(name.lower())
    return type_map.get(type_name) if type_name else None


def known_tags() -> list[str]:
    """All defined tags as ``@name`` tokens (for autocomplete)."""
    _type_map, tag_map = _load()
    return sorted(f"@{name}" for name in tag_map)


def types() -> dict[str, str]:
    """The defined types as ``name -> Rich style``."""
    return dict(_load()[0])


def add_tag(name: str, type_name: str) -> None:
    type_map, tag_map = _load()
    if type_name not in type_map:
        raise ValueError(f"unknown type {type_name!r}; define it with `ccc tag type` first")
    tag_map[name.lstrip("@").lower()] = type_name
    save(type_map, tag_map)


def add_type(name: str, style: str) -> None:
    type_map, tag_map = _load()
    type_map[name] = style
    save(type_map, tag_map)
