# Hooks & the status line

`ccc` captures session state through Claude Code's hook system and renders live progress
in the status line. `ccc init` wires both; you can also install them individually and
inspect them with `ccc doctor`.

```commands
ccc install-hooks [-n] [-u]            # merge ccc's hook entries into settings.json
ccc install-statusline [-c] [-n] [-u]  # wire the status line (-c chains an existing one)
ccc doctor                             # read-only: which hooks + statusline are wired
```

`install-hooks` is **idempotent and non-destructive**: it replaces only ccc's own entries
in place, never touches foreign hooks, backs up `settings.json` before writing, and writes
symlink-safely (through a stow symlink to its real target). `-n/--dry-run` prints a unified
diff; `-u/--uninstall` removes only ccc-owned entries.

## What each hook does

| Event               | ccc does                                                                          |
| :------------------ | :-------------------------------------------------------------------------------- |
| **SessionStart**    | registers the session, seeds its AIM from `$CLAUDE_SESSION_AIM`, badges its tab   |
| **UserPromptSubmit** | nags to set an AIM if missing; nudges to sharpen a vague AIM / re-align sub-goals / tick finished items |
| **PreToolUse** (`Edit\|Write\|MultiEdit\|NotebookEdit`) | acquires the cross-session file lock on the target file (or denies + queues) |
| **PostToolUse**     | forwards the session's live `TodoWrite`/Task list into ccc; nudges the lock holder to hand off when a peer waits |
| **Stop**            | end-of-turn: spawns the detached progress grader / AIM-met assessment (when enabled); the commit + `release-locks` floor |
| **release-locks**   | drops every file lock the session holds (wired *after* any commit step — see below) |
| **SessionEnd**      | final reconcile so the row parks cleanly                                          |
| **PreCompact**      | preserves state across a context compaction                                       |
| **SubagentStop**    | keeps sub-agent activity from being mistaken for the main turn ending             |

Headless `claude -p` runs never create rows: the hooks bail when
`CLAUDE_CODE_ENTRYPOINT` says `sdk-*`, and the adapter skips live registry entries whose
`entrypoint` starts with `sdk`. This matters because a `claude -p` spawned *from inside* a
real session inherits that session's AIM and cwd; without the guard every such run would
leak a duplicate row.

## The Stop-hook ordering contract

The `release-locks` hook must run **after** anything that commits the turn's work, so a
waiting session never starts on uncommitted changes. `ccc install-hooks` places its
`release-locks` entry **last** in the `Stop` chain for exactly this reason.

**If you have your own commit automation** (an auto-commit-on-Stop hook of your own), make
sure it is registered *before* ccc's `release-locks` entry in `settings.json`. The
invariant is: *commit the files → then release the locks*. `ccc handoff <file>` is the one
release path that commits first itself (commit → push → release), so it is always safe;
the automatic Stop-time release relies on your commit step running earlier in the chain.

## The status line

`ccc`'s status line adds, under Claude Code's own line:

- the `/aim (1):` anchor row — the done-condition **as you first typed it**, dimmed, shown
  whenever the current revision is no longer the first, so a sharpened AIM never hides your
  original goal;
- the `/aim (N):` row — the current AIM (or its short label), its running index, the
  concreteness score chip (red when vague), and a compact progress bar;
- a `Status:` + `/next-step:` row;
- a blue `●` drift warning when the impartial checker has flagged one;
- a one-line `done/total` + checkbox strip of the session's live todos.

The main status line also opens with the tab's coloured **badge** and closes with a
compact AIM-progress bar (`ccc aim --format bar`), so it shows *which tab* and *how far
along* at a glance.

### Installing it, and chaining an existing one

```commands
ccc install-statusline            # if no statusLine is set, installs ccc's directly
ccc install-statusline --chain    # if you already have one, run it first, then ccc's
```

With no existing `statusLine`, ccc installs `ccc statusline --capture-usage` directly. If
a foreign `statusLine` is already configured, ccc **refuses to overwrite it** unless you
pass `-c/--chain`, which generates a small wrapper script that runs your original first
(under a 2 s timeout) and then appends ccc's rows. `-u/--uninstall` restores the recorded
original.

### `--capture-usage`

The status-line command is piped Claude Code's full status-line JSON on stdin, which is
the only place the account's `rate_limits` are exposed. `--capture-usage` persists that
(account-global) snapshot to `usage.json`, which feeds the Claude Code usage card in the
TUI. Idle sessions report a stale view, so concurrent writes are merged per window (a past
reset is dropped, the freshest reset wins) — the card stays correct even when every
session is parked.

### Prerequisite: stop the CLI clobbering the tab title

Claude Code overwrites the tab title on startup, *after* your shell hook set the badge.
Set `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` (e.g. in `settings.json`'s `env`) so the
shell-set, badged title sticks.
