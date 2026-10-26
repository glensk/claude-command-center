# Hooks & the status line

`ccc` captures session state through Claude Code's hook system and renders live progress
in the status line. `ccc init` wires both; you can also install them individually and
inspect them with `ccc doctor`.

```commands
ccc install-hooks [-n] [-u] [-f]       # merge ccc's hook entries into settings.json
ccc install-statusline [-c] [-n] [-u]  # wire the status line (-c chains an existing one)
ccc doctor                             # read-only: which hooks + statusline are wired
```

`install-hooks` is **idempotent and non-destructive**: it replaces only ccc's own entries
in place, never touches foreign hooks, backs up `settings.json` before writing, and writes
symlink-safely (through a stow symlink to its real target). `-n/--dry-run` prints a unified
diff; `-u/--uninstall` removes only ccc-owned entries.

ccc's entries must be the **only path to `ccc hook`**. A hand-wired forwarder script that
also calls `ccc hook <event>` is foreign to the installer — which preserves foreign hooks by
contract — so it survives every install and every event it is wired on then runs ccc
**twice**. `ccc install-hooks` therefore refuses to install next to one (`-f/--force`
installs anyway) and `ccc doctor` reports it as ❌ `duplicate hook path`. Let the installer
own the wiring: delete the forwarder, don't wrap it.

## What each hook does

| Event               | ccc does                                                                          |
| :------------------ | :-------------------------------------------------------------------------------- |
| **SessionStart**    | registers the session, seeds its AIM from `$CLAUDE_SESSION_AIM`, badges its tab   |
| **UserPromptSubmit** | nags to set an AIM if missing; nudges to sharpen a vague AIM / re-align sub-goals / tick finished items |
| **PreToolUse** (`Edit\|Write\|MultiEdit\|NotebookEdit`) | acquires the cross-session file lock on the target file (or denies + queues) |
| **PostToolUse**     | forwards the session's live `TodoWrite`/Task list into ccc; nudges the lock holder to hand off when a peer waits |
| **Stop**            | end-of-turn: spawns the detached progress grader / AIM-met assessment (when enabled) |
| **release-locks**   | LEASES every file lock the session holds past the end of the turn (`stop_barrier_wait_sec`) instead of dropping it, fires an armed `mark-done --close` / `switch-account`, then runs the staff close guard — see below |
| **SessionEnd**      | final reconcile so the row parks cleanly                                          |
| **PreCompact**      | preserves state across a context compaction                                       |
| **SubagentStart**   | counts in-process subagents so `switch-account` refuses while one runs            |
| **SubagentStop**    | keeps sub-agent activity from being mistaken for the main turn ending             |

Headless `claude -p` runs never create rows: the hooks bail when
`CLAUDE_CODE_ENTRYPOINT` says `sdk-*`, and the adapter skips live registry entries whose
`entrypoint` starts with `sdk`. This matters because a `claude -p` spawned *from inside* a
real session inherits that session's AIM and cwd; without the guard every such run would
leak a duplicate row.

## The Stop-hook lock lease

**Claude Code runs every hook of one event in parallel.** Where a hook sits in the `Stop`
list therefore guarantees nothing: ccc's `release-locks` entry cannot be "ordered after"
your auto-commit, and an auto-commit that runs for two minutes is still writing the
session's files long after the turn ended. (ccc still appends its two `Stop` entries last —
that is cosmetic, and the installer tests only assert the wiring, not an ordering effect.)

What holds the invariant *commit the files → then release the locks* is a **lease in the
store**, not a running process:

- **The turn ends → the locks are stamped, not dropped.** `release-locks` sets
  `protected_until = now + stop_barrier_wait_sec` (default 180 s) on every lock the session
  holds. Nothing has to survive, finish or be observed for that to hold — it is a timestamp
  in SQLite, and it expires by itself.
- **A stamped lock denies every peer**, whatever the holder's liveness or the ordinary
  `file_lock_ttl_sec`; that refusal says the holder's files are being committed right now and
  names the seconds until the lock frees itself, so the waiting session knows the wait is
  bounded and why. `ccc locks` shows the same state (`committing — frees in N s`).
- **The holder's own next edit clears the stamp** — a new edit is a new turn.
- **`ccc handoff <file>` is the way to hand a file over IMMEDIATELY** without waiting the
  lease out: it commits (path-scoped) → pushes → releases that one file, in that order, so
  the waiter never starts on uncommitted work. `ccc lock-release [--all]` deletes leased
  rows too — by design: it is the explicit "I know what I am doing" escape hatch.
- **The price is honest and bounded:** a contended file can stay denied for up to
  `stop_barrier_wait_sec` after a turn instead of milliseconds. Lower it, or set
  `stop_barrier_enabled = false` to go back to the immediate release.
- **Do not set `stop_barrier_wait_sec = 0` to opt out.** The clamp accepts it, but at 0 the
  lease is empty AND the drain wait can never confirm anything (a single clean scan is not a
  proof), so `close-now` refuses **every** close and `switch-now` every relaunch. The way to
  opt out completely is `stop_barrier_enabled = false`.

**If you have your own commit automation** (an auto-commit-on-Stop hook), you no longer
have to register it anywhere in particular. Check instead that its declared `timeout` fits
inside `stop_barrier_wait_sec` — `ccc doctor`'s *Stop-hook timeout coverage* check does
exactly that comparison (and says so when it cannot: an entry it cannot read, an enabled
plugin, or a managed-settings file may add `Stop` hooks ccc never sees).

**The two destructive paths wait, and refuse when they cannot tell.** `ccc close-now`
(`mark-done --close`) and `ccc switch-now` (`switch-account`) must kill a Claude process,
so they first watch its process tree until the Stop chain looks drained — two consecutive
clean scans `stop_barrier_settle_sec` apart, bounded by `stop_barrier_wait_sec`. A timeout
with hooks still running, an unreadable `ps`, or a pid whose identity changed leaves the
process **and** its tab alive (logged to `events.log`, plus a desktop notification). That
observation is best effort by construction — hooks contributed by **plugins** or by a
managed-settings file appear in no file ccc can read, and are only caught by a `hook`
substring over-match — which is exactly why the *lease*, not the wait, carries the
guarantee: even a kill that lands mid-commit cannot hand a half-written file to a peer.

**One documented exception: `ON DELETE CASCADE`.** `file_locks.session_id` references
`sessions(session_id)` with `ON DELETE CASCADE`, so deleting a session row (`Store.delete`
/ `delete_many` — pruning, `ccc rm`) erases its leased locks without consulting
`protected_until`. That is deliberate: removing a session is an explicit operator action,
not an automatic expiry.

## The staff close guard (llm-staff agents)

An llm-staff agent such as `mac-overseer` ends its episode with `staff lease -r` and then
`ccc mark-done --close`. When it forgets the close, its idle `claude` keeps counting as a live
agent and blocks every later launch. `staff lease -r` therefore writes a pending-close marker
`$STAFF_STATE/released/<agent>/<session_id>.json` (`{agent, owner, generation, nonce,
released_at_iso}`; `STAFF_STATE` defaults to `~/.local/state/llm-staff`) before it frees the
lease, and `release-locks` — after it has handled the close/switch claim, so it can never keep
`close-now` from firing — checks it on every Stop
(`command_center/staff_close_guard.py`):

- **No marker → one `lstat()` per guarded agent**, nothing else (no subprocess, no store
  write). Guarded agents: `CC_STAFF_CLOSE_GUARD_AGENTS` (comma list, default `mac-overseer`).
  Agent and session ids must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; the marker is read
  with `O_NOFOLLOW` and must be a regular file owned by you, in a directory owned by you,
  naming this agent and session. An untrusted marker never blocks.
- **Marker + a claimed `mark-done --close`, or a session marked done → pass.**
- **Otherwise the read-only probe** `staff lease -s -j -a <agent> -o <session_id>` (2 s;
  `staff` resolves through `STAFF_BIN` → `$PATH`): `held_by_owner: true` means the episode
  went on and re-took the lease — the marker is stale, ccc deletes it (only while it still
  carries the same nonce) and passes. Anything else — free, lapsed, held by someone else, a
  timeout, a missing `staff`, bad JSON — **blocks**: a marker exists and re-hold is not proven.
- **A block** is `{"decision": "block", "reason": "… Call Skill ccc-mark-done-and-close now;
  if the episode continues, re-take the lease first (staff lease -a <agent> -o <sid>) …"}` on
  stdout with exit 0 — on EVERY Stop, not once. A per-nonce counter in the store
  (`staff_close_guard` table) is read and bumped in one `BEGIN IMMEDIATE`, and the block is
  printed before the commit: a hook killed in between under-counts, it never suppresses a
  block. After **5** consecutive blocks on one nonce the next Stop passes once and logs
  `escalated`; the launch trigger's `blocked:` alert is then the backstop. Any other pass
  resets the counter.
- Every decision taken while a marker exists lands in `events.log` as `staff-close-guard`
  with `agent=… nonce=… decision=…` (block, escalated, pass-close, pass-done, pass-reheld,
  invalid-marker) — never command or transcript text.
- **Off switch:** `CC_NO_STAFF_CLOSE_GUARD=1`. Any exception inside the guard means no block
  (fail open).

## The status line

`ccc`'s status line adds, under Claude Code's own line:

- the `/aim (1):` anchor row — the done-condition **as you first typed it**, dimmed, shown
  whenever the current revision is no longer the first, so a sharpened AIM never hides your
  original goal;
- the `/aim (N):` row — the current AIM (or its short label), its running index, the
  concreteness score chip (red when vague), and a compact progress bar. A **`🎭`** right after
  the label (`/aim: 🎭 tp#994, #1114: email-agent`) means the session ran `staff adopt -a
  <agent>` and works in that staff ROLE (tp#1166): ccc reads tp's adopt binding
  `$TP_CACHE_DIR/session-role/<sid>` (default `~/.cache/tp/session-role/<sid>`; the named agent
  must still exist under `$TP_STAFF_ROOT`, default `~/obsidian/01-llm/staff`). A session that is
  merely named or AIMed after an agent shows no `🎭`. The agent's own `symbol:` (its
  `agent.yaml`, tp#1288) follows the `🎭` — `🎭📧` for the email-agent — the same symbol
  that replaces the badge in its iTerm tab title and command-center row; an agent with a
  Nerd Font `glyph:` shows that instead (slack-agent: the Slack logo glyph);
- a `Status:` + `/next-step:` row;
- a blue `●` drift warning when the impartial checker has flagged one;
- a one-line `done/total` + checkbox strip of the session's live todos.

The main status line also opens with the tab's coloured **badge** (for an adopted staff
session: its agent's mark — `ccc statusline` keeps `~/.cache/ccc-role-mark/<sid>` holding the
`glyph:` or `symbol:`, and the status-line script prefers that file over the tab's badge file,
tp#1288) and closes with a
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
