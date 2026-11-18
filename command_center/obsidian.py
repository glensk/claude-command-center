#!/usr/bin/env python3
"""``ccc obsidian-setup`` — seed the vault's capture pad, mirror folders & dashboards.

A default (no-flag) run is **content-only and offline**: it creates the mirror folders
derived from config (``running_dir`` / ``done_dir`` / ``sessions_dir``), seeds the capture
pad when it is served (:func:`futuresync.pad_on`), and renders the generified Obsidian
dashboards from the templates under ``assets/obsidian/`` (parameterising the queried
folder paths + the absolute ``ccc`` binary from config, not hardcoded values). Future jobs
live in ccc's store (tp#2266): setup seeds no ``future/`` / ``delete/`` folders, no
``future.md`` / ``delete.md`` dashboards and no job-button shellcommands — a rerun strips
the four retired ccc entries from an existing obsidian-shellcommands ``data.json``. Every
generated dashboard carries a ``ccc_generated: true`` frontmatter marker so reruns /
``--uninstall`` only ever overwrite or remove files ccc itself wrote. All writes are
atomic + backed up; ``--dry-run`` writes nothing.

``--install-plugins`` is the ONLY networked path: a consent-gated bootstrap that downloads
the three pinned community plugins (manifest ``assets/obsidian/plugins.json``), verifies
each file's sha256 (a mismatch aborts + rolls back), writes ``plugins/<id>/`` and enables
them in ``community-plugins.json``.

Pure helpers (``render_dashboard`` / ``strip_shellcommands`` / ``load_plugins_manifest``)
carry the logic and are unit-tested; :func:`run_setup` wires them to the filesystem.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import copy
import json
import os
import sys
import time
from importlib.resources import files
from pathlib import Path

from . import config, futuresync, install
from .future_files import (
    _DELETE_JOB_COMMAND_ID,
    _DONE_JOB_COMMAND_ID,
    _RESTORE_JOB_COMMAND_ID,
    _START_JOB_COMMAND_ID,
)

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
#: Frontmatter marker line every generated dashboard carries (rerun/uninstall guard).
MARKER_KEY = "ccc_generated"

#: obsidian-shellcommands command-id prefix (``<plugin>:shell-command-<entry-id>``).
_SHELLCMD_PREFIX = "obsidian-shellcommands:shell-command-"

#: The obsidian command ids of the four RETIRED job-button shell commands (open-job /
#: done-job / delete-job / restore-job ``--file``; tp#2266). Setup no longer merges them;
#: it strips them from an existing ``data.json`` (the entry id is the command id with the
#: plugin prefix stripped).
_RETIRED_SHELLCMD_IDS: tuple[str, ...] = (
    _START_JOB_COMMAND_ID,
    _DONE_JOB_COMMAND_ID,
    _DELETE_JOB_COMMAND_ID,
    _RESTORE_JOB_COMMAND_ID,
)

#: The three community plugins obsidian-setup depends on (ids match community-plugins.json).
REQUIRED_PLUGINS: tuple[str, ...] = (
    "obsidian-meta-bind-plugin",
    "obsidian-shellcommands",
    "dataview",
)


# --------------------------------------------------------------------------- #
# small shared IO helpers (atomic + timestamped backup)
# --------------------------------------------------------------------------- #
def _utc_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.ccc-tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _backup(path: Path) -> Path | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    backup = path.with_name(f"{path.name}.ccc-backup-{_utc_stamp()}")
    try:
        backup.write_bytes(data)
    except OSError:
        return None
    return backup


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# path / token derivation
# --------------------------------------------------------------------------- #
def resolve_vault(cfg: config.Config, root: str | None) -> Path:
    """The target vault: the ``--root`` override else ``vault_root`` config, expanded."""
    return Path(root or cfg.vault_root).expanduser()


def _vault_rel(path: Path, vault: Path) -> str:
    """*path* relative to *vault* as a posix string (absolute path if not under it)."""
    try:
        return path.expanduser().resolve().relative_to(vault.resolve()).as_posix()
    except (ValueError, OSError):
        return path.expanduser().as_posix()


def task_dirs(cfg: config.Config) -> list[Path]:
    """The mirror folders obsidian-setup creates (expanded).

    No ``future_dir`` / ``delete_dir``: future jobs live in ccc's store (tp#2266); the
    capture pad is seeded on its own (see :func:`_setup_folders`).
    """
    return [
        Path(cfg.running_dir).expanduser(),
        Path(cfg.done_dir).expanduser(),
        Path(cfg.sessions_dir).expanduser(),
    ]


# --------------------------------------------------------------------------- #
# dashboards
# --------------------------------------------------------------------------- #
def _asset_text(*parts: str) -> str:
    node = files("command_center") / "assets"
    for part in parts:
        node = node / part
    return node.read_text(encoding="utf-8")


def render_dashboard(template_name: str, cfg: config.Config, vault: Path, ccc: str) -> str:
    """Render one ``assets/obsidian/<template_name>`` with the config-derived tokens."""
    text = _asset_text("obsidian", template_name)
    tokens = {
        "{{CCC_BIN}}": ccc,
        "{{RUNNING_FOLDER}}": _vault_rel(Path(cfg.running_dir).expanduser(), vault),
    }
    for token, value in tokens.items():
        text = text.replace(token, value)
    return text


def dashboard_targets(cfg: config.Config) -> list[tuple[str, Path]]:
    """``(template_name, destination_path)`` for each dashboard (running + parked).

    Both live one level above their queried folder, so they never mirror themselves.
    The ``future.md`` / ``delete.md`` job dashboards are retired (tp#2266).
    """
    running_dir = Path(cfg.running_dir).expanduser()
    return [
        ("running.md.tmpl", running_dir.parent / "running.md"),
        ("parked.md.tmpl", running_dir.parent / "parked.md"),
    ]


def has_marker(text: str) -> bool:
    """Whether *text*'s leading YAML frontmatter carries the ``ccc_generated`` marker."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if line.split(":", 1)[0].strip() == MARKER_KEY:
            return True
    return False


# --------------------------------------------------------------------------- #
# obsidian-shellcommands data.json: strip the retired job-button entries
# --------------------------------------------------------------------------- #
def _entry_id(command_id: str) -> str:
    """The data.json entry ``id`` for an obsidian command id (strip the plugin prefix)."""
    return (
        command_id[len(_SHELLCMD_PREFIX) :]
        if command_id.startswith(_SHELLCMD_PREFIX)
        else command_id
    )


def retired_entry_ids() -> set[str]:
    """The data.json entry ids of the four retired ccc job-button shell commands."""
    return {_entry_id(cmd_id) for cmd_id in _RETIRED_SHELLCMD_IDS}


def strip_shellcommands(data: dict) -> dict:
    """*data* without ccc's retired job-button entries; foreign entries are preserved.

    Idempotent and otherwise lossless: only entries whose fixed ``id`` is one of
    :func:`retired_entry_ids` are dropped, every other key and entry is kept as is.
    """
    result = copy.deepcopy(data) if isinstance(data, dict) else {}
    existing = result.get("shell_commands")
    if not isinstance(existing, list):
        return result
    ours = retired_entry_ids()
    result["shell_commands"] = [
        e for e in existing if not (isinstance(e, dict) and e.get("id") in ours)
    ]
    return result


def _shellcommands_dir(vault: Path) -> Path:
    return vault / ".obsidian" / "plugins" / "obsidian-shellcommands"


# --------------------------------------------------------------------------- #
# plugins manifest
# --------------------------------------------------------------------------- #
def load_plugins_manifest() -> dict:
    """Parse the pinned ``assets/obsidian/plugins.json`` bootstrap manifest."""
    return json.loads(_asset_text("obsidian", "plugins.json"))


# --------------------------------------------------------------------------- #
# default (offline) setup
# --------------------------------------------------------------------------- #
def _refuse_no_vault(vault: Path) -> int:
    print(
        f"ccc obsidian-setup: vault_root does not exist: {vault}\n"
        "  set it first (edit ~/.claude/command-center/config.toml or run `ccc init`),\n"
        "  or pass an existing vault with -r/--root.",
        file=sys.stderr,
    )
    return 1


def _setup_folders(cfg: config.Config, *, dry_run: bool) -> None:
    print("folders:")
    for path in task_dirs(cfg):
        exists = path.is_dir()
        print(f"  {'exists' if exists else 'create'}  {path}")
        if not dry_run and not exists:
            path.mkdir(parents=True, exist_ok=True)
    # The capture pad is the one future-job surface left in the vault (tp#2266): seeded
    # only when it is served (future_files OR future_pad_enabled — futuresync.pad_on).
    if not futuresync.pad_on(cfg):
        return
    pad = Path(cfg.future_pad).expanduser()
    print(f"  {'exists' if pad.exists() else 'create'}  {pad}  (capture pad)")
    if not dry_run and not pad.exists():
        futuresync.reset_pad(cfg)


def _setup_dashboards(cfg: config.Config, vault: Path, ccc: str, *, dry_run: bool) -> None:
    print("dashboards:")
    for template_name, dest in dashboard_targets(cfg):
        rendered = render_dashboard(template_name, cfg, vault, ccc)
        existing = _read(dest)
        if existing is None:
            print(f"  create  {dest}")
            if not dry_run:
                _atomic_write(dest, rendered)
        elif existing == rendered:
            print(f"  ok      {dest} (up to date)")
        elif has_marker(existing):
            print(f"  update  {dest} (ccc-generated; backed up)")
            if not dry_run:
                _backup(dest)
                _atomic_write(dest, rendered)
        else:
            print(f"  keep    {dest} (not ccc-generated — refusing to overwrite)")


def _uninstall_dashboards(cfg: config.Config, *, dry_run: bool) -> None:
    print("dashboards:")
    for _template_name, dest in dashboard_targets(cfg):
        existing = _read(dest)
        if existing is None:
            continue
        if has_marker(existing):
            print(f"  remove  {dest}")
            if not dry_run:
                try:
                    dest.unlink()
                except OSError:
                    pass
        else:
            print(f"  keep    {dest} (not ccc-generated)")


def _strip_retired_shellcommands(vault: Path, *, dry_run: bool) -> bool:
    """Drop ccc's retired job-button entries from an existing obsidian-shellcommands
    ``data.json`` (backed up). Creates nothing; a missing, unreadable or already-clean
    file is left alone. Returns whether ``.obsidian`` was touched."""
    data_path = _shellcommands_dir(vault) / "data.json"
    raw = _read(data_path)
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    stripped = strip_shellcommands(data)
    if stripped == data:
        return False
    dropped = len(data.get("shell_commands", [])) - len(stripped.get("shell_commands", []))
    print("shellcommands:")
    print(f"  strip   {data_path} ({dropped} retired ccc job-button entries; backed up)")
    if not dry_run:
        _backup(data_path)
        _atomic_write(data_path, json.dumps(stripped, indent=2, ensure_ascii=False) + "\n")
    return True


def run_setup(
    *,
    root: str | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    install_plugins: bool = False,
    yes: bool = False,
) -> int:
    """Seed (or tear down) the vault's ccc mirror folders, capture pad and dashboards."""
    cfg = config.load_config()
    vault = resolve_vault(cfg, root)
    if not vault.is_dir():
        return _refuse_no_vault(vault)
    ccc = install.ccc_binary()

    touched_obsidian = False

    if install_plugins and not uninstall:
        rc = run_install_plugins(cfg, vault, yes=yes, dry_run=dry_run)
        if rc != 0:
            return rc
        touched_obsidian = True

    if uninstall:
        _uninstall_dashboards(cfg, dry_run=dry_run)
    else:
        _setup_folders(cfg, dry_run=dry_run)
        _setup_dashboards(cfg, vault, ccc, dry_run=dry_run)
    if _strip_retired_shellcommands(vault, dry_run=dry_run):
        touched_obsidian = True

    if touched_obsidian and not dry_run:
        print("\nNote: reload Obsidian (Cmd+R) so the plugins re-read their config.")
    if dry_run:
        print("\n[dry-run] nothing was written.")
    return 0


# --------------------------------------------------------------------------- #
# plugin bootstrap (--install-plugins) — the only networked path
# --------------------------------------------------------------------------- #
def _default_downloader(url: str) -> bytes:
    import urllib.request  # pylint: disable=import-outside-toplevel

    req = urllib.request.Request(url, headers={"User-Agent": "ccc-obsidian-setup"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        return bytes(resp.read())


def _sha256(data: bytes) -> str:
    import hashlib  # pylint: disable=import-outside-toplevel

    return hashlib.sha256(data).hexdigest()


def _archive_obsidian(vault: Path) -> Path | None:
    """Tar-gz ``<vault>/.obsidian`` to a timestamped sibling; return the archive path."""
    import tarfile  # pylint: disable=import-outside-toplevel

    dot = vault / ".obsidian"
    if not dot.is_dir():
        return None
    archive = vault / f".obsidian.ccc-backup-{_utc_stamp()}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(dot, arcname=".obsidian")
    return archive


def _enable_plugins(community_path: Path, ids: list[str]) -> tuple[str, str]:
    """Return ``(old_text, new_text)`` for community-plugins.json with *ids* enabled."""
    raw = _read(community_path)
    current: list[str] = []
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                current = [str(x) for x in loaded]
        except (json.JSONDecodeError, ValueError):
            current = []
    merged = list(current)
    for plugin_id in ids:
        if plugin_id not in merged:
            merged.append(plugin_id)
    return raw or "", json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


def run_install_plugins(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements,too-many-return-statements
    cfg: config.Config,  # pylint: disable=unused-argument
    vault: Path,
    *,
    yes: bool = False,
    dry_run: bool = False,
    downloader=None,
) -> int:
    """Consent-gated download+verify+write of the three pinned community plugins."""
    downloader = downloader or _default_downloader
    manifest = load_plugins_manifest()
    plugins = manifest.get("plugins", [])

    print("install-plugins: pinned community plugins")
    for plugin in plugins:
        files_desc = ", ".join(f["name"] for f in plugin.get("files", []))
        print(f"  {plugin['id']} {plugin['version']}  ({files_desc})")

    if dry_run:
        print("\n[dry-run] would download + verify the files above and enable them.")
        return 0

    if not yes:
        if not sys.stdin.isatty():
            print(
                "ccc obsidian-setup --install-plugins: refusing to run non-interactively "
                "without -y/--yes.",
                file=sys.stderr,
            )
            return 3
        print(
            "\nThis downloads and enables Obsidian community plugins. "
            "obsidian-shellcommands can execute ARBITRARY shell commands from within your "
            "vault. Only proceed if you trust this setup."
        )
        answer = input("Proceed with plugin install? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted.")
            return 1

    # 1. Backup .obsidian up front (durable, user-restorable).
    archive = _archive_obsidian(vault)
    if archive:
        print(f"backed up .obsidian → {archive}")

    # 2. Download + verify EVERYTHING before writing anything (a mismatch aborts clean).
    fetched: dict[str, dict[str, bytes]] = {}
    for plugin in plugins:
        fetched[plugin["id"]] = {}
        for spec in plugin.get("files", []):
            try:
                data = downloader(spec["url"])
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                print(f"download failed for {spec['url']}: {exc}", file=sys.stderr)
                return 1
            digest = _sha256(data)
            if digest != spec["sha256"]:
                print(
                    f"sha256 MISMATCH for {plugin['id']}/{spec['name']}:\n"
                    f"  expected {spec['sha256']}\n  got      {digest}\n"
                    "aborting — nothing was written.",
                    file=sys.stderr,
                )
                return 1
            fetched[plugin["id"]][spec["name"]] = data
    print(f"verified sha256 for {sum(len(v) for v in fetched.values())} files")

    # 3. Write plugin files + enable them, with a transactional rollback on any failure.
    prior: dict[Path, bytes | None] = {}

    def _write_bytes(path: Path, data: bytes) -> None:
        prior[path] = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    community_path = vault / ".obsidian" / "community-plugins.json"
    try:
        for plugin in plugins:
            dest_dir = vault / ".obsidian" / "plugins" / plugin["id"]
            for name, data in fetched[plugin["id"]].items():
                _write_bytes(dest_dir / name, data)
            print(f"  wrote plugins/{plugin['id']}/")
        _old, new_text = _enable_plugins(community_path, [p["id"] for p in plugins])
        prior[community_path] = community_path.read_bytes() if community_path.exists() else None
        community_path.parent.mkdir(parents=True, exist_ok=True)
        community_path.write_text(new_text, encoding="utf-8")
        print("  enabled in community-plugins.json")
    except OSError as exc:
        print(f"write failed ({exc}); rolling back.", file=sys.stderr)
        for path, content in prior.items():
            try:
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
            except OSError:
                pass
        return 1

    print(
        "\nplugins installed. In Obsidian: turn OFF Restricted Mode "
        "(Settings → Community plugins) and reload (Cmd+R) to load them."
    )
    return 0
