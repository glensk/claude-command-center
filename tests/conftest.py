"""Shared test fixtures — hermetic isolation from the real machine.

The per-tab badge system (:mod:`command_center.tabsymbol`) is filesystem-backed:
``assign``/``read`` persist one small file per iTerm tab under
``~/.cache/iterm-tab-symbol`` (overridable via ``CCC_TAB_SYMBOL_DIR``), and the
hook / daemon / TUI paths push ``"<badge> <leaf>"`` to the *live* iTerm tab whose
``$ITERM_SESSION_ID`` they read from the environment.

Without isolation a test run leaks into the developer's real session:

* ``tabsymbol.assign`` writes into the real cache, claiming a real palette slot.
  With the palette near capacity this forces a reclaim of a real tab's badge,
  reshuffling the cache so live tab **titles** (set on the last ``cd``/seed) no
  longer match the row the command center shows — the exact "tab symbols disagree"
  bug.
* the SessionStart hook reads the *tester's own* ``$ITERM_SESSION_ID`` and pushes
  a ``"<badge> repo"`` title onto the real tab the suite is running in.

This autouse fixture redirects the cache to a throwaway dir and unsets
``ITERM_SESSION_ID`` for every test, so the suite can neither pollute the real
badge cache nor drive real iTerm tabs. Tests that exercise the badge code on
purpose (``test_tabsymbol``, ``test_tui``) still point the cache at their own
``tmp_path`` — that simply re-overrides this default, which is fine.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _pin_claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``CLAUDE_HOME`` under ``tmp_path`` so NO test can touch the real ``~/.claude``.

    ``CLAUDE_HOME`` anchors everything ccc reads and writes: the SQLite store
    (``db_path``), the usage caches, and — critically — the user config
    (``config_path`` → ``~/.claude/command-center/config.toml``). Any test that reached
    ``config.load_config()`` / ``config.save_config()`` without setting ``CLAUDE_HOME``
    itself would read AND potentially overwrite the developer's REAL config. That is not
    hypothetical: the real ``config.toml`` was once silently wiped to defaults by a
    reload-modify-save path (a failed parse fell back to DEFAULTS, which a subsequent
    ``save_config`` then persisted). Individual tests already pin ``CLAUDE_HOME`` per the
    module convention, but a single one that forgot was enough to clobber the real home.

    Making the pin autouse closes that hole globally: every test defaults to a throwaway
    ``tmp_path/claude-home``. Tests that set ``CLAUDE_HOME`` themselves simply re-override
    this default (monkeypatch is last-wins within a test), so the existing per-test pins
    keep working unchanged.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))


@pytest.fixture(autouse=True)
def _pin_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``CODEX_HOME`` under ``tmp_path`` so NO test can read the real ``~/.codex``.

    ``config.codex_home()`` anchors the Codex usage sources: the session rollout files
    AND — since the live usage endpoint landed — ``auth.json``, which holds a real
    ChatGPT OAuth token. A test that reaches the Codex card (the TUI renders it on every
    tick, and its border title decodes the account e-mail out of that file) would
    otherwise read the developer's actual credential store. Pinning it to a throwaway
    dir keeps the suite hermetic; tests that need Codex data set ``CODEX_HOME``
    themselves and simply re-override this default (monkeypatch is last-wins).
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


@pytest.fixture(autouse=True)
def _isolate_tab_symbol_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real badge cache and the real iTerm session."""
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "iterm-tab-symbol"))
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)


@pytest.fixture(autouse=True)
def _isolate_peek_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real iTerm focus probe (peek's ccc-TUI-row fallback).

    ``peek.resolve_peek`` now checks whether the focused tab is the live ccc TUI
    (an AppleScript tty probe) BEFORE the uuid map. On a dev machine with a real
    TUI running that would fire osascript per test; pin the probe to ``None`` —
    tests that exercise the fallback re-patch ``peek._focused_tty`` themselves.
    """
    from command_center import peek as _peek

    monkeypatch.setattr(_peek, "_focused_tty", lambda: None)


@pytest.fixture(autouse=True)
def _pin_single_claude_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the developer's REAL Claude accounts.

    21 test files never set ``CLAUDE_HOME``, so an un-pinned ``load_config()`` would
    read the developer's real ``config.toml`` and could pick up their real
    ``claude_accounts`` — routing usage writes at the actual ``~/.claude`` tree. Pin
    ``config.claude_config_dirs`` to the single default account (``{"private":
    claude_home()}``, which honours each test's tmp ``CLAUDE_HOME``) and clear
    ``CLAUDE_CONFIG_DIR`` so ``_account_from_env`` deterministically falls back to the
    sole account. Also clear ``CCC_HOME`` so ``app_home()`` (now routed through
    ``ccc_home()``) still resolves under each test's tmp ``CLAUDE_HOME`` rather than a
    stray value in the developer's shell. Tests needing multiple accounts override this
    fixture explicitly.
    """
    from command_center import config as _config

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CCC_HOME", raising=False)
    monkeypatch.setattr(_config, "claude_config_dirs", lambda: {"private": _config.claude_home()})


@pytest.fixture(autouse=True)
def _isolate_vault_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the real Obsidian vault.

    The future-job files and the running/done mirrors default to real paths under
    ``~/obsidian/01-llm-tasks/``. A test that reaches ``config.load_config()``
    indirectly (e.g. ``daemon.run_once``, CLI handlers) would otherwise export
    its fixture sessions into the developer's actual vault — which happened once:
    daemon tests isolated the store via ``CLAUDE_HOME`` but ``run_once``'s
    internal ``load_config()`` still pointed the new mirror pass at the real
    dirs, replacing the real mirrors with fixture jobs until the daemon healed
    them. This wrapper rewrites the five vault-path keys on every loaded config;
    tests that build ``Config(...)`` explicitly keep their own values (and the
    resolver-side guard in :mod:`command_center.config` fails loudly for any
    future leak vector this wrapper cannot see).

    ``repo_root`` is blanked for the same reason: it outranks ``$GIT_BASE`` in
    :func:`command_center.repos.repo_root`, so on a machine whose real config sets
    it, every fixture that points ``$GIT_BASE`` at a fake tree (``test_repos``,
    ``test_core``) would silently resolve against the developer's actual repo tree.
    Blanking it makes the ``$GIT_BASE`` monkeypatch the effective knob under test.
    """
    from command_center import config as _config

    real_load = _config.load_config

    def _tmp_vaulted() -> _config.Config:
        cfg = real_load()
        vault = tmp_path / "vault"
        cfg.vault_root = str(vault)
        cfg.future_dir = str(vault / "01-llm-tasks" / "future")
        cfg.delete_dir = str(vault / "01-llm-tasks" / "delete")
        cfg.future_pad = str(vault / "01-llm-tasks" / "new-prompt.md")
        cfg.running_dir = str(vault / "01-llm-tasks" / "running")
        cfg.done_dir = str(vault / "01-llm-tasks" / "done")
        cfg.sessions_dir = str(vault / "01-llm-tasks" / "sessions")
        cfg.repo_root = ""  # let each test's $GIT_BASE monkeypatch govern the repo tree
        return cfg

    monkeypatch.setattr(_config, "load_config", _tmp_vaulted)


@pytest.fixture(autouse=True)
def _allow_headless_start_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the exec-path tests run without a pseudo-terminal.

    ``cmd_start_job`` / ``cmd_resume`` refuse to ``execvp`` claude when this process
    has no TTY (they open a real tab instead) — without that guard a job launched from
    a non-interactive shell degrades into a headless one-shot whose row the daemon then
    prunes, so it vanishes from ccc entirely. Under pytest stdin/stdout are captured, so
    every existing test that asserts on the exec argv would take the tab branch instead.
    Setting the documented opt-out restores the old behaviour suite-wide; the tests that
    exercise the guard itself (``test_start_job_tty.py``) delete this var again.
    """
    monkeypatch.setenv("CCC_START_JOB_HEADLESS", "1")


@pytest.fixture(autouse=True)
def _reset_config_memo() -> None:
    """Drop the ``load_config`` memo before every test.

    The memo is keyed on ``(config path, mtime_ns, size)``, so the per-test ``CLAUDE_HOME``
    pin already keeps tests apart; resetting it here makes that isolation structural
    rather than a side effect of every test getting a distinct ``tmp_path``.
    """
    from command_center import config as _config

    _config.invalidate_config_cache()


# --------------------------------------------------------------------------- #
# Three Codex seats — the fixture the runner / seat-order tests share
#
# `quota._canonical_codex_homes()` hard-codes `default = Path.home()/".codex"`, so a
# seat fixture can only be hermetic under a TEMP $HOME. Everything below therefore
# builds a whole fake account (temp HOME + temp CCC_HOME) and hands back the env a
# subprocess needs; in-process tests additionally patch `Path.home`.
# --------------------------------------------------------------------------- #
def _jwt(email: str) -> str:
    """A syntactically valid, unsigned JWT whose payload carries *email*.

    ``usage.codex_account_email`` decodes ``auth.json``'s ``tokens.id_token`` to name
    the account behind a seat; without three base64url segments it reports nothing and
    every seat renders as "unknown account".
    """

    def seg(data: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'email': email})}.signature"


@dataclass
class SeatFixture:
    """Three configured Codex seats plus the fake-codex control channel."""

    home: Path  # the temp $HOME (default seat lives at $HOME/.codex)
    ccc_home: Path  # the temp $CCC_HOME (config.toml + cooldowns.json)
    seats: dict[str, Path]  # label -> CODEX_HOME
    control: Path  # $FAKE_CODEX_CONTROL (scenario per home)
    log: Path  # $FAKE_CODEX_LOG (one JSON line per fake-codex call)
    cic_config: Path  # $CODEX_IN_CLAUDE_CONFIG (the account pin lives here)
    workdir: Path  # a git-free directory to point `-C` at

    def scenarios(self, **by_label: object) -> None:
        """Script the fake codex per seat: ``fixture.scenarios(private="refuse_quota")``.

        A value may be a bare scenario name or a dict (``{"scenario": …, "reply": …,
        "resets_at": …}``); an unscripted seat answers ``ok``.
        """
        table = {str(self.seats[label]): value for label, value in by_label.items()}
        self.control.write_text(json.dumps(table), encoding="utf-8")

    def reorder(self, *labels: str) -> None:
        """Rewrite ``codex_seat_order`` (and drop the config memo) for this fixture.

        Write tests need ``de`` to lead: it is the only seat whose ``config.toml``
        declares ``hardened-rw``, so a write run on any other seat is refused before
        codex is ever launched.
        """
        from command_center import config as _config

        path = self.ccc_home / "command-center" / "config.toml"
        ranked = ", ".join(f'"{label}"' for label in labels)
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("codex_seat_order")
        ]
        path.write_text("\n".join([*lines, f"codex_seat_order = [{ranked}]"]) + "\n", "utf-8")
        _config.invalidate_config_cache()

    def reset_log(self) -> None:
        """Forget every recorded fake-codex call (a second phase in one test)."""
        self.log.unlink(missing_ok=True)

    def calls(self) -> list[dict]:
        """Every fake-codex invocation so far, in order."""
        try:
            text = self.log.read_text(encoding="utf-8")
        except OSError:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def call_homes(self) -> list[str]:
        """The seat LABELS the fake was invoked under, in order — the hop trace."""
        by_path = {str(path): label for label, path in self.seats.items()}
        return [by_path.get(call["home"], call["home"]) for call in self.calls()]

    def stdin_of(self, index: int) -> str:
        """The prompt the *index*-th (1-based) fake-codex call received on stdin."""
        return Path(f"{self.log}.stdin.{index}").read_text(encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        """A scrubbed environment for a SUBPROCESS run against this fixture."""
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("CODEX_HOME", "CCC_NO_CODEX", "CODEX_IN_CLAUDE_IGNORE_QUOTA")
        }
        env.update(
            {
                "HOME": str(self.home),
                "CCC_HOME": str(self.ccc_home),
                "CLAUDE_HOME": str(self.ccc_home),
                "CODEX_IN_CLAUDE_CONFIG": str(self.cic_config),
                "CODEX_IN_CLAUDE_RUNS_DIR": str(self.home / "runs"),
                "CODEX_IN_CLAUDE_COST_LOG": str(self.home / "cost-history.jsonl"),
                "CODEX_IN_CLAUDE_SLOT_DIR": str(self.home / "slots"),
                "FAKE_CODEX_CONTROL": str(self.control),
                "FAKE_CODEX_LOG": str(self.log),
            }
        )
        env.update(extra)
        return env


_SEAT_CONFIGS = {
    # label: (extra config.toml body, e-mail)
    "default": ("", "team@example.org"),
    "private": ('[mcp_servers.alpha]\ncommand = "true"\n', "private@example.org"),
    "de": (
        '[permissions.hardened-rw]\nextends = ":workspace"\n\n'
        '[mcp_servers.beta]\ncommand = "true"\n\n'
        '[mcp_servers.gamma]\ncommand = "true"\n',
        "de@example.org",
    ),
}


def make_three_seats(tmp_path: Path, order: list[str] | None = None) -> SeatFixture:
    """Build the three-seat account under *tmp_path* (no monkeypatching).

    Each seat gets a DIFFERENT ``config.toml`` on purpose: only ``de`` declares
    ``hardened-rw`` (so a write run on another seat is refused before launch), and the
    three declare different MCP servers — which is what proves the runner rebuilds the
    argv per seat instead of reusing the first seat's flags.
    """
    home = tmp_path / "acct"
    seats = {
        "default": home / ".codex",
        "private": home / "seats" / "private",
        "de": home / "seats" / "de",
    }
    for label, path in seats.items():
        path.mkdir(parents=True, exist_ok=True)
        extra, email = _SEAT_CONFIGS[label]
        (path / "config.toml").write_text(
            'default_permissions = "hardened-ro"\n\n'
            '[permissions.hardened-ro]\nextends = ":read-only"\n\n' + extra,
            encoding="utf-8",
        )
        (path / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {"id_token": _jwt(email), "access_token": "x", "account_id": "a"},
                }
            ),
            encoding="utf-8",
        )
    ccc_home = tmp_path / "ccc"
    (ccc_home / "command-center").mkdir(parents=True, exist_ok=True)
    listed = order if order is not None else ["private", "de", "default"]
    ranked = ", ".join(f'"{label}"' for label in listed)
    (ccc_home / "command-center" / "config.toml").write_text(
        f'codex_home_private = "{seats["private"]}"\n'
        f'codex_homes_extra = ["de={seats["de"]}"]\n'
        f"codex_seat_order = [{ranked}]\n"
        "codex_usage = false\n",
        encoding="utf-8",
    )
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    return SeatFixture(
        home=home,
        ccc_home=ccc_home,
        seats=seats,
        control=tmp_path / "fake-codex-control.json",
        log=tmp_path / "fake-codex-log.jsonl",
        cic_config=tmp_path / "codex-in-claude.json",
        workdir=workdir,
    )


@pytest.fixture()
def three_seats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SeatFixture:
    """The three-seat account, wired into THIS process (temp HOME included).

    ``Path.home()`` is patched as well as ``$HOME``: ``quota._canonical_codex_homes``
    calls it directly, so an env-only pin would still resolve the developer's real
    ``~/.codex`` as the default seat and read their actual credentials.
    """
    from command_center import codex_launch, config, usage

    fixture = make_three_seats(tmp_path)
    for key, value in fixture.env().items():
        if key in (
            "HOME",
            "CCC_HOME",
            "CLAUDE_HOME",
            "CODEX_IN_CLAUDE_CONFIG",
            "CODEX_IN_CLAUDE_RUNS_DIR",
            "CODEX_IN_CLAUDE_COST_LOG",
            "CODEX_IN_CLAUDE_SLOT_DIR",
            "FAKE_CODEX_CONTROL",
            "FAKE_CODEX_LOG",
        ):
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CCC_NO_CODEX", raising=False)
    monkeypatch.delenv("CODEX_IN_CLAUDE_IGNORE_QUOTA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fixture.home))
    monkeypatch.setattr(
        codex_launch,
        "resolve_codex",
        lambda: str(Path(__file__).parent / "fakes" / "fake_codex.py"),
    )
    config.invalidate_config_cache()
    usage._codex_cache.clear()  # noqa: SLF001
    usage._codex_email_cache.clear()  # noqa: SLF001
    return fixture
