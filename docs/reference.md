# ccc — full reference

The complete feature reference for `ccc`, the command center for your Claude Code
sessions. For the pitch and 5-minute quickstart see the [README](../README.md). The
Obsidian, hooks, Codex and mobile guides live alongside this file:
[obsidian.md](obsidian.md) · [hooks.md](hooks.md) · [codex.md](codex.md) ·
[roadmap-mobile-grapheneos.md](roadmap-mobile-grapheneos.md).

Throughout, `repo_root` is the root of your category/repo tree (the `repo_root` config
key, or `$GIT_BASE`); sessions are grouped by the first folder beneath it (e.g.
`work` / `home` / `oss`).

### Commands — the full catalogue

`ccc --help` prints the whole catalogue; `ccc <cmd> --help` documents each command's
flags. Grouped by what they do:

**The command center**

- `ccc` / `ccc tui` — the interactive Textual TUI (falls back to the flat list when piped).
- `ccc ls` — a flat, clickable, one-line-per-session list (scripting-friendly).
- `ccc demo [--ls] [--clean]` — a throwaway fake-data command center; safe to try with zero setup.
- `ccc serve [--host --port]` — serve the TUI in the browser (`textual-serve`).
- `ccc doctor` — read-only health check of the install + environment (exit 1 on any ❌).
- `ccc init` — first-run wizard: environment check → consent → installers.

**AIM, progress & the checkers**

- `ccc set-aim "<done-when>"` — set the done-condition (the single chokepoint for CLI/TUI/hook);
  `-f/--first` instead rewrites the FIRST recorded AIM (`/aim (1)`) in place, adding no revision.
- `ccc aim` · `ccc aim-history` — the current AIM / its first→current progression.
- `ccc set-next` · `ccc set-blocked` · `ccc set-deadline` · `ccc set-donecheck` — the other job fields.
- `ccc subgoals "step" …` (`--list`, `--adaptive`, `--merge`) — the progress checklist.
- `ccc check <N>` (`--uncheck`) · `ccc subgoal-check <N> "<cmd>"` — tick an item / attach a shell predicate.
- `ccc mark-done` (`--undo`, `-c/--close`, `-q/--quiet`) · `ccc keep` (`--off`) — finish / exempt a session from the idle reaper. `--close` also closes the session's pane/tab after the turn ends (see the `ccc-mark-done-and-close` skill); `--quiet` suppresses the summary print.
- `ccc todos` — a session's live TodoWrite/Task list; `ccc ack-drift` — acknowledge a flagged drift.
- `ccc subgoal-history` — the checklist's evolution + drift verdicts.
- Internal LLM checkers, spawned automatically (rarely run by hand): `ccc score-aim`, `ccc short-aim`, `ccc check-drift`, `ccc assess-aim`, `ccc autoprogress`.

**Future jobs**

- `ccc new-job -a "AIM" [-p PROMPT] [-c REPO] [-s YYYY-MM-DD] [-d DEP] [-j codex] [-O overseer] [-E executor]` — park a future job (`-d` = another job it depends on).
- `ccc new-prompt [-r cat/repo] [-o]` — a prefilled capture file for a future job.
- `ccc jobs` — list registered future jobs (drafts).
- `ccc job-account` — per-account usage urgency + which account a new job will bill to (the `job_account` policy).
- `ccc quota` — cache-first quota oracle: which provider/account still has tokens, and when each blocked one unblocks.
- `ccc start-job <id>` / `ccc open-job <id>|--file` — launch a saved job (in place / in a new tab, safe from Obsidian). Prefer `open-job` from scripts and agents: `start-job` execs in place and refuses without a TTY (it opens a tab instead) — see "Terminal guard" below.
- `ccc done-job` · `ccc delete-job` · `ccc restore-job` · `ccc unlaunch` — the lifecycle: done-without-running / trash / restore / back-to-draft.

Every `<id>` above accepts the **8-char id `ccc jobs` prints** (or any unique prefix), not just the full UUID — exact match wins, an ambiguous prefix errors with the matches listed, matching is case-insensitive.

**Sessions & tabs**

- `ccc resume <id>` — resume a session in this terminal (`execvp`).
- `ccc resume-job <id>` — resume a parked session in a new tab (focuses it if already live).
- `ccc focus-job <id>` — bring a live session's tab forward (verifies it is live first).
- `ccc archive <id>` (`-u/--undo`) — soft-hide a PARKED session (`archived=1`) so it is listed once, on tp's board (`tp import-ccc -a -z` calls this); the row keeps its cwd/account, so `ccc resume <id>` / `ccc resume-job <id>` still work, and the moment the session is seen live again it is un-archived (it parks back into ccc's list until tp re-archives it). Refuses live sessions and FUTURE drafts (`delete-job` is the draft trash).
- `ccc rm` · `ccc prune` — drop a tracked row / delete leftovers (contentless, headless `claude -p`, and dead-launched jobs that never had a turn).
- `ccc peek` (`--print`) · `ccc jump` — the macOS peek panel / the ccc↔session toggle.
- `ccc snapshot` (`-l/--list`, `-n/--dry-run`) · `ccc restore-snapshot [name]` (`-n/--dry-run`, `-y/--yes`) — save the whole iTerm layout before a reboot, rebuild it afterwards.

**Obsidian & mirrors**

- `ccc obsidian-setup [-r VAULT] [--install-plugins]` — seed the vault folders, dashboards & job buttons.
- `ccc sync-future` · `ccc sync-mirrors` — reconcile the future-job files / export the running+done mirrors (usually automatic).

**Cross-session file locks**

- `ccc handoff <file>` — commit+push a locked file, then release it for a waiting session.
- `ccc locks` · `ccc lock-release <file>` — list / force-release active locks.

**Install & housekeeping**

- `ccc install-hooks` · `ccc install-statusline` · `ccc install-commands` · `ccc install-shell` — the individual installers `ccc init` runs.
- `ccc daemon [--install|--uninstall|--status|--dry-run]` — the background housekeeper (launchd on macOS, systemd `--user` on Linux).
- `ccc resume-halted [--watch|--dry-run]` — auto-resume rate-limit-halted sessions once the limit resets.
- `ccc toggle-idle` · `ccc tab-symbol` · `ccc tag` · `ccc copilot-usage` · `ccc codex-usage` — mute idle popups / per-repo badge / typed @tags / refresh Copilot usage / refresh the live OpenAI Codex usage.
- `ccc restart-tui` — restart the running ccc TUI in its own tab (for automations that changed ccc's code/config).

Inside a Claude Code session the slash commands `/aim` `/next-step` `/done` `/block`
`/deadline` (plus `/aim-history`, `/subgoal-history`) drive the same actions from the
prompt; they are installed by `ccc init` / `ccc install-commands`. The same installer
also ships the **`ccc-mark-done-and-close`** skill (by default): when the user asks to
finish AND close the whole job — "mark this session as done", "we're done here — close
it" — it runs `ccc mark-done --close -q`, which marks the session done and then, after
the turn's Stop-hook chain (auto-commit included) completes, SIGTERMs the Claude process
and closes its terminal pane/tab. The wiring is the internal `ccc close-now --session
<id> [--iterm <id>]` command, spawned by the final `release-locks` Stop hook once the
close request is atomically claimed. `ccc doctor` warns when that `release-locks` hook is
not the LAST Stop entry (it must run after foreign Stop hooks like auto-commit). Undo a
mistaken close with `ccc mark-done --undo --session <id>`, then `ccc resume <id>`.

### Peek — "what have I asked here?" (`ccc peek`)

`ccc peek` resolves the iTerm tab you are looking at to the Claude session running
in it and shows a floating macOS panel titled **"ccc peek panel"** (so you can refer to
it by name). A **header** mirrors the Claude Code status
line so the panel is unambiguously "this tab": the tab's coloured emoji **badge**
(💙 / 🔵 / …), the **session id** painted on its tab-colour background (the same
colour the status line uses — the per-tab `iterm-tab-rgb` cache, repo colour as
fallback), and the **working directory**. Below it are **three tabs**:

- **prompts** — every *human* prompt of the session, oldest `(1)` at the top down to
  the most recent at the bottom (the view opens scrolled to the bottom), in a
  permanently-scrollable pane. Each prompt is headed by a `(N) ───…` **rule line**
  (number + separator on one line, in a distinct blue) followed by a blank line; the
  **newest prompt renders pronounced in bold gold** so the current ask always stands
  out. A prompt you typed **while Claude was working** is queued by the harness and
  stored in a different transcript shape (a `queued_command` attachment, not a user
  record) — it is listed too, at the point the queue delivered it. Background-task
  completion notices (`<task-notification>`) and cross-session messages from a peer
  session are **not** prompts and are filtered out — the list is what *you* typed only.
- **session** — the **full conversation**, terminal-like: each `## (N) you` prompt
  (numbering matches the prompts tab), Claude's replies, and dim `⏺ Tool(input…)` /
  `⎿ result…` one-liners for every tool call (results truncated to a few lines; no
  thinking blocks, no full tool outputs). It is the SAME canonical render the vault
  **session mirror** file embeds (`command_center/sessionmd.py` is the single
  source), capped at 512 KiB keeping the newest whole turns (`showing last N of M
  turns` note when trimmed).
- **aim** — the session's AIM (done-condition) history, oldest first with the current
  AIM marked `← current`, including each revision's score and short label.

**Parked / done sessions**: when the focused tab is the **ccc TUI itself**, the panel
resolves the TUI's *selected row* instead (the same `jump_selected` state `f+j` uses)
— so `s`+`p` on the ccc tab peeks at any parked, running or done session without a
live tab. (Checked before the tab-UUID map, so a stale UUID recorded on the ccc tab
can never shadow the selected row.) Inside the TUI you can also just type the **`sp`
chord** (`s` then `p`, like the other two-key chords `sh` / `os` / `ah`): it spawns
`ccc peek --session <highlighted-row-id>` directly, so it always hits the row under
the cursor regardless of tty/uuid detection — the reliable in-TUI path, versus the
global Karabiner `s`+`p` which needs a *simultaneous* press to fire.

`←` / `→` switch tabs; **⌘C** copies the visible tab (the selection if you made one,
otherwise the whole body). **`/`** or **⌘F** starts an incremental **search** over the
visible tab — every match is highlighted amber, the current one orange, with a `n/total`
counter; **Return** / **⇧Return** step to the next / previous match (search follows you
across tabs), **⌫** edits the query, **⎋** leaves search (a second **⎋** closes). When
not searching, **Space**, **Return** or **Escape** close the panel — and it also
**closes on click-away** (the moment you click the terminal or any other window), so it
never lingers in the foreground; clicking inside the panel keeps it open. The mapping reuses
the `iterm_session_id` (the `$ITERM_SESSION_ID` tab UUID) ccc already records per
session — found via `$ITERM_SESSION_ID` when run inside the tab's shell, or via
AppleScript (the focused iTerm session) when triggered by a global hotkey. The AIM
history (and the badge/colour) come from ccc's store, so they are only populated for a
tab ccc tracks (the cwd fallback for an untracked tab shows prompts only, coloured by
the repo). The AppKit panel (PyObjC, macOS-only dependency) is imported lazily, so no
other `ccc` command pays its cost. `--print` prints the prompts to stdout instead of
showing the panel.

It is wired to a **Karabiner** chord — **hold `s`, then tap `p` while `s` is still
down**, only while iTerm2 is frontmost. This mirrors the existing movement-layer
idiom (`simultaneous [s, …]`, strict key-down order, 500 ms threshold); the rule
runs `ccc peek` and lives in your `karabiner.json`. Typing `sp` normally is unaffected unless `s` is held down.

### Jump — a one-key toggle between ccc and your session (`ccc jump`)

`ccc jump` is **context-aware** — tapping the chord flips you back and forth between
the command center and the tab you came from:

- **In a Claude session tab** → the live TUI moves its cursor onto *that* session's
  row, then the ccc tab comes forward.
- **In the ccc tab** → it does what the TUI's **`r`** does: brings the
  currently-selected session's tab forward (or resumes it in a new tab if its tab is
  gone). Arrow to any row first and `f+j` jumps to *that* session.
- **In another app** (or with `--no-toggle`) → it just brings ccc forward; if no TUI
  is running it opens one (`--no-launch` suppresses that and exits non-zero).

So from a session you go to ccc-with-that-row-selected, and from ccc you go straight
back — `f+j` · `f+j` round-trips. The TUI tab is located by the bare `ccc` process's
controlling **tty** (`ps` → iTerm's `tty of session`) — title-independent, so
renamed/badged tabs still match; it falls back to the `tab_title` (e.g. `!!!`).

**Fast path (a live TUI is running).** `ccc jump` hands the *whole* toggle to the
resident TUI — the out-of-process chord run only writes a one-byte request verb and
returns (~80 ms), skipping its own `ps` scan and the AppleScript window walk (~1 s over
many tabs). The TUI does the toggle in-process over **iTerm2's Python API**: a warm,
long-lived websocket makes focus-refresh, session-by-id lookup, and activation
sub-millisecond, so the whole f+j lands in ~0.2 s. This path needs the bundled
`iterm2` package **and** iTerm's *Settings → General → Magic → Enable Python API*
turned on. When no TUI is running, the API is off/unavailable, or you pass
`--no-toggle`, `ccc jump` falls back to the original AppleScript path described above —
it must work with no TUI at all.

Coordination with the live TUI is a handful of tiny files under
`$CLAUDE_HOME/command-center/` (`jumpstate`): the TUI publishes its cursor's session
(`jump_selected`) and its own identity (`jump_tui`, `pid|iterm_session_id`, enabling the
fast path), and consumes a cursor-move request (`jump_request`), the whole-toggle verb
(`jump_toggle`), or the restart verb (`jump_restart`, see `ccc restart-tui` below). They
are polled every **0.1 s**, so the jump feels instant — a ~0.1 ms
file read at 10 Hz is free, and that cadence (not a slow osascript walk) now bounds the
perceived f+j latency. The frontmost-app check uses `lsappinfo` (no Accessibility
prompt).

It is wired to a **global Karabiner** chord — **hold `f`, then tap `j` while `f` is
still down** — mirroring the `s+p` peek idiom (`simultaneous [f, j]`, strict
key-down order, 500 ms threshold) but with **no frontmost-app condition**, so it
works everywhere. The chord shadows the redundant `f`-layer `f+j → ↓`; the `s`-layer
still provides `s+j → ↓`, so vim-down is not lost. Revert by removing the
`Global: f+j … ccc jump` rule from `karabiner.json` (a timestamped
`karabiner.pre-ccc-jump.*.bak.json` backup sits beside it).

> **The TUI half needs a restart to activate.** `ccc` is installed editable, but the
> *running* TUI loaded its code at launch — quit (`q`) and relaunch `ccc` once so it
> starts publishing its selection and polling for jump requests.

#### Restart the TUI in place (`ccc restart-tui`)

`ccc restart-tui` automates that "quit and relaunch" step for **automations** — a Claude
session (or any script) that just edited ccc's code or config on an editable install and
needs the running TUI to pick it up. It reuses the jump plumbing: it writes the
`jump_restart` verb, the live TUI's 0.1 s poll consumes it, exits cleanly, and — once
Textual has restored the terminal — the process **re-execs itself in place**
(`os.execv`/`execvp`), so the TUI comes back up in its **own terminal tab** with the new
code, no new window or manual keystroke. It exits **0** once the TUI has restarted (the
request was consumed and a live TUI re-registered), and **1** when no TUI is running or
the restart did not complete within 25 s. It is an internal-style command (no TUI key, no
footer entry). A leftover restart request is always cleared on TUI startup, so a stale
file can never loop-restart a freshly launched TUI.

**Liveness watchdog.** The TUI runs a plain daemon thread (`watchdog.py`, not a Textual
timer — those are the first casualty of a wedge) that the 0.1 s poll beats. If the
heartbeat stops for 120 s while the app is not exiting, or an exit (quit / restart) has
not finished within 10 s, the TUI is wedged: the watchdog appends a report — the app's
state flags plus a `faulthandler` dump of **every thread's Python stack** — to
`$CCC_HOME/command-center/tui-watchdog.log`, restores the terminal, and then re-execs
the TUI in place (a stall, or a hung restart) or finishes the quit. At most three
consecutive watchdog re-execs (`$CCC_TUI_WATCHDOG_RESTARTS`, reset by the first applied
refresh) — past that it exits 1 and names the log. `kill -USR1 <pid>` writes the same
all-thread stack dump on demand. Why: on 2026-09-02 a shutdown hung before `Unmount`
and the last frame sat on screen for hours — ages, next-steps and progress bars frozen
while the store (and the status line) moved on, and `ccc restart-tui` could not help
because the poll that consumes its request was dead too. The 25 s `restart-tui` budget
covers that heal.

In the TUI, **`d`** marks the selected session done (its in-session `Status:` line
shows `done` on that session's next render). If the session is still live, `d` then
asks whether to close it — confirming SIGTERMs the process and closes its iTerm
pane, taking the whole tab with it when that was the only pane.

Navigate with **`↑/↓`** (whole-row highlight); in whole-row mode **`Enter`** **resumes /
switches to the session's tab** (same as `r` — a LIVE tab is focused, a PARKED session
resumes in a new tab, a FUTURE job launches). Press **`←/→`** to cycle a single-cell
cursor across the editable columns (`/aim` · `/next-step` · `progress`, wrapping at either
end); there **`Enter`** edits one inline — on the **progress bar** it asks for a **manual
percentage** (0-100; blank returns the bar to auto = the sub-goal ratio) — `↑/↓` (or
`Esc`) snap back to whole-row selection. The
**word-back / word-forward** keys (`Option+←/→`, which iTerm
sends as `Esc b`/`Esc f` → Textual `ctrl+←/→`) **jump three rows down / up** (wrapping
around at the top/bottom). The single keys `a` `n` `D` `b` still edit a field directly,
and **`e`** edits the fields **in place** — the `job details:` **layout does not change** (the
divider reads `job details (e to edit):`, the `e` gilded gold); the editable lines simply gain a
cursor (the focused line is tinted — that tint shows immediately on `e`, scrolled into view — no
boxes, no popup). While editing, `Tab` is **confined to the detail pane**: it cycles the fields
plus the read-only **`Status:` head** as an extra stop (tinted too, scrolls the pane to its top),
so the top of the pane is always reachable; it never escapes into the session table. The head
also **compacts** while editing — its models/account readout, `Scheduled for:` line and
*Prompt to run* body drop out, since those are exactly the form's editable rows (every option
shows once). **`↑/↓`** move between the one-line fields
(**`/next-step` · `/deadline` · `progress %` · `/block`**); the **AIM**, the **sub-goal checklist**
and the future-job **prompt to run** are borderless **multi-line** fields that grow to fit
(**`Tab`** leaves them). **`progress %`** sets a manual bar override (blank = auto from sub-goals;
cleared when the session is marked done). The **sub-goals** field edits the checklist one item per
line — **add or delete lines** freely, ticks carry over by text — and a manual edit labels the
checklist **`manual`** (instead of e.g. `auto (claude-haiku-4-5)`) in the detail pane. For a
**future job** you also edit the **folder/repo** it runs in (category→repo picker). Type to edit,
and **`Esc`** saves every changed field and returns focus to the session table — **clearing the AIM
warns first** (it is never lost: every AIM stays in aim-history). Ticking stays on `Space`
or `ccc check`; the bottom checklist (with its tick state) stays put
(**`/deadline` & `/block` are no longer table columns** — set them with `D` / `b` or via
`e`; their full values, and the deadline badge, show in the detail pane). The narrow **version column**
(third, just before the repo name) shows the **patch part of the Claude Code version** that
last wrote the session's transcript (`193` of `2.1.193`) — live sessions are stamped on every
reconcile, parked/legacy rows are self-healed by a daemon backfill, and a session with no
on-disk transcript shows blank. A session using
`/codex-implement-task-and-claude-review` shows an inverse **`OAI`** badge there instead,
including manually-invoked workflow sessions detected from the transcript. That column's
header carries the **`head:`** label — it both names
the version column and, like the footer's `keys:`, names the whole column-header line (which is why
the importance `!`/`!!`/`!!!` column to its left has no heading of its own). Right of the repo
name the **`id` column** mimics the Claude Code status line: the tab's **emoji badge** and the
**first 4 chars of the session id** form **one contiguous chip** — a single run painted on
that tab's **colour** (the per-tab `iterm-tab-rgb` cache, else the repo colour) with black or
white text picked by luminance, no uncoloured gap between badge and id. Only a row whose
**tab is actually open** is painted: a parked (`☾`), finished or failed row — and a future job,
which shows its blue 4-hex display hash — keeps its badge but gets **no background**, so the
colour reads as "this session is on screen right now". Two open tabs that would show the same
colour are automatically pulled apart (see
[Tab badges](#tab-badges-telling-same-folder-sessions-apart)). The **`/aim` column
auto-stretches** to fill the leftover row width so the trailing **`progress` column is
pinned flush to the right edge** (it crops the AIM text to whatever fits and re-fits on
resize). The `/aim`, `/next-step` and
`/block` editors open as a **large, soft-wrapping editor** so a long, multi-line value is
fully visible without scrolling — there **`Enter` inserts a newline**, **`Ctrl+S` saves**,
`Esc` cancels, and **`Tab` completes the current `@tag`** (`/deadline` stays a one-line
field that `Enter` submits). **DONE** sessions are **hidden by default** (toggle with the
`td` chord) and **FUTURE jobs are shown by default** (toggle with `tf`); a session **waiting
for input** shows its
`⏸` indicator in **red**. A session **halted by a Claude rate limit** — its last turn
ended in a "You've hit your … limit ·" API error (the 5-hour *session* or 7-day *weekly*
window) — shows a **red `||`** instead (status `halted`); it clears automatically once the
session **successfully** resumes past the limit (a non-error assistant turn lands).
Detection reads the transcript tail and keys on the **last *assistant* record** being a
genuine API-error: a queued "continue", a background task-notification, or any other
trailing *user* record arriving while still rate-limited does **not** clear it — otherwise
the indicator would flip-flop between `halted` and `working` during the wait, with no
work actually happening. A prompt that merely *quotes* the phrase never trips it. A
Codex-workflow session waiting on an exhausted OpenAI Codex usage window shows `😴`
instead; it is lower-precedence than active/waiting/halted states and lower than
`💤` snoozed when a live background task still exists.

Press **`?`** (or `h`) for the full key reference. The footer shows **`toggle`** (the `t`
leader); the **`td`** chord (type `t` then `d`) shows/hides DONE sessions, **`tf`** (`t`
then `f`) shows/hides FUTURE jobs, and **`ti`** (`t` then `i`) mutes/unmutes Claude Code's
idle "waiting for input" macOS popups (it flips the native `agentPushNotifEnabled` in
`settings.json` — global, ON by default; also `ccc toggle-idle`). Two more `t…` chords act on
the highlighted row: **`tp`** / **`tw`** set its Claude account to private / work (see
*Home-icon marker* under Multi-account). Pressing `t` alone briefly lists the available chords. **`u`** undoes the last action (close, done, toggles, account switch, sub-goal tick), walking back up to 20 steps. The bottom footer hint line — prefixed **`keys:`** — lists **the commands that opt
in (any with a `footer_pos`), each with its key gilded gold** — single letters in place
(`/`**a**`im`) and two-key chords with both letters gilded (**a**im-**h**istory = `ah`,
**s**ubgoal-**h**istory = `sh`). A few keys are deliberately kept out of this line to keep
it short (e.g. **`K`**eep) and live only in the help menu. Every keystroke, that footer line,
the column-header mnemonics and the help are generated from one registry
(`command_center/views/commands.py`), so a command added there (with a `footer_pos`) surfaces
everywhere at once — it can never be wired in one place and forgotten in another.

**Named UI parts.** So each region can be referred to by name (when talking about the TUI
with a person or an LLM), the major parts carry a `name:`-style label, mirroring the footer's
long-standing **`keys:`** prefix:

| Name           | Where                                                                       |
| :------------- | :-------------------------------------------------------------------------- |
| `head:`        | the column-header line at the top of the session table (was the `!` column) |
| `keys:`        | the bottom footer hint line (the commands with a `footer_pos`)              |
| `job details:` | the divider above the detail pane (the bottom half of the screen)           |
| usage cards    | `Claude` · `Codex` · `GitHub Copilot <model>` (top-right of the detail pane) |

There is **no top title bar** — the table (with its `head:` line) is the topmost widget, so
the screen opens straight onto the sessions. The same names are listed under **Named parts**
in the in-TUI help (`?`).

![the ccc detail pane (job details:): the selected session's AIM, its concreteness score, the sub-goal checklist, the next step, and the stacked Claude / Codex / GitHub Copilot subscription-usage cards](img/tui-detail.svg)

### Snapshot & restore — survive a reboot with your desk intact (`ccc snapshot`)

A macOS update is about to take the machine down and you have eleven Claude sessions,
three `vim`s and an `htop` spread across four iTerm windows. `ccc snapshot` writes the
*shape* of that desk to a JSON file; after the reboot `ccc restore-snapshot` rebuilds it.

**What is captured.** Every iTerm2 window (its frame and which tab was selected), every
tab, and each tab's **split topology** — read from `tab.root`'s splitter tree, so pane
*order* is preserved (`tab.sessions` is unordered and is never used for layout). Per pane
one of three things:

- **a Claude session** — matched to ccc's own records by the pane UUID first (the
  `iterm_session_id` ccc already stores), by the live process's tty as a fallback. Stored
  as its session id **plus the account** (`config_dir`) it was running under.
- **a command** — the foreground program's **exact `argv`**, read straight out of the
  kernel (`sysctl KERN_PROCARGS2`), never re-parsed from a display string. An **ancestor
  rule** walks up from the foreground process toward the login shell and keeps the
  *highest* allowlisted ancestor, so `man git` wins over the `less` child that actually
  owns the terminal. The pane's cwd comes from one batched `lsof`.
- **a shell** — nothing in the foreground (or a program whose `argv` could not be read):
  just the cwd, plus a note naming what was there.

Snapshots live in `$CLAUDE_HOME/command-center/snapshots/` (dir `0700`, files `0600`,
written `mkstemp` + `os.replace` so a file is never half-written) as
`<YYYYmmdd-HHMMSS>.json`, with `-2`, `-3` … on a same-second collision. There is no
`latest.json`: "latest" is resolved as the newest file at restore time. `ccc snapshot -l`
lists what you have (name, age, window/tab/pane counts, how many are Claude sessions);
`ccc snapshot -n` prints the capture and its per-pane classification without writing.

**What restore does.** `ccc restore-snapshot` (no argument = the newest snapshot; a bare
name resolves in the snapshots dir with or without `.json`; anything containing `/` is
used as a path) prints the snapshot's age, then plans every pane:

| Pane | Action |
| :--- | :----- |
| Claude session, already live | left alone — `already running — skipped` |
| Claude session, resume blocked | **error** pane: only `cd <cwd>`, with the reason |
| Claude session, clean | `cd <cwd> && claude --resume <id>`, prefixed with the account pin |
| command on the allowlist | `cd <cwd> && <argv re-quoted element by element>` |
| command not on the allowlist | `cd <cwd>` + a printed `[ccc-restore] was running: …` note |
| shell / unreadable argv | `cd <cwd>` (+ the note when we know what ran) |
| cwd gone from disk | nothing is run; the pane is reported as degraded (not an error) |

"Resume blocked" is the **same** `core.resume_blockers` pre-flight `ccc resume` uses:
an unknown account while several are configured, an id live under two accounts at once,
or no transcript on disk. So a restore can never bill the wrong seat or exec a doomed
`claude --resume`, and the wording is identical on both surfaces.

Execution is **two-phase**: first the *whole* empty layout is built (windows in order,
frames best-effort, tabs, then each tab's splits rebuilt recursively — child 0 keeps the
pane it was handed, each later child is split off the previous one), then every pane
receives its command and non-Claude panes get their recorded title back. A failure
half-way therefore leaves a usable, if bare, desk rather than a half-typed one; per-pane
failures are collected and reported. **Exit 0** = everything restored or cleanly skipped;
**1** = any pane errored (or a guard refused).

**Guards.**

- `-n/--dry-run` prints the full window/tab/pane plan and touches iTerm not at all.
- macOS restores app windows *itself* after a reboot, and restoring on top of that
  silently **duplicates** the layout. So restore counts the panes iTerm already has
  besides the one you are typing in (identified via `$ITERM_SESSION_ID`) and **refuses**
  while any exist — `-y/--yes` overrides.
- `launcher = "tmux"` refuses with a pointer at `tmux attach` (tmux persists its own
  layout); a missing iTerm2 Python API refuses naming
  *Settings → General → Magic → Enable Python API*.

**Config.** `snapshot_restore_commands` (list) is the allowlist of programs a pane may
**re-run** from its captured argv — default
`["vi", "vim", "nvim", "less", "man", "tail", "htop", "btop", "top", "ccc", "ssh"]`.
Anything else only ever gets *printed*. Matching **sees through interpreter wrappers**: a
`#!`-script is exec'd as `<interpreter> <script> …`, so a `python`/`python3*`/`perl`/
`ruby`/`node`/`bun` argv0 is matched by the **script's** basename instead — which is what
makes the `ccc` entry relaunch the TUI (really `python3 …/bin/ccc`). An interpreter with
no script (`python3 -m http.server`) stays matched as `python3`, and what gets re-run is
always the full exact argv, interpreter included.

**Safety model in one line:** a restored command is rebuilt exclusively from exact
kernel-read `argv` of your own processes, re-quoted per element with `shlex.quote`;
free-text fields are never executed, only `printf`-ed.

**v1 limitations** (deliberate):

- No automatic/periodic snapshot — you run it before the reboot.
- macOS + iTerm2 Python API only; no AppleScript, Terminal.app or tmux backend.
- Split *topology* is restored, pane **sizes** are not; profiles are not; a frame that
  will not apply (multi-monitor) is skipped silently.
- Natively-restored panes are not adopted or reused — hence the guard above; a
  deliberate double restore duplicates non-Claude tabs.
- Codex CLI panes come back as a shell + note (there is no reliable `codex resume` map).
- A tmux session running *inside* an iTerm pane is not descended into.

### Future jobs — park a thought, start it later (`f`+`n`)

A **future job** is a Claude Code session you *describe now and start later* — a way to
capture "I should do X in repo Y" without opening a session for it yet. In the TUI, put the
cursor on a repo row (or a category header like `home`/`sdsc`) and press **`f` then `n`**.
You get a small dialog that captures the **AIM** (required — the done-condition), an optional
**prompt** (the first message to send; defaults to the AIM), an optional **intended start**
note (free text like *"during holidays"*, shown in the **id** column), an optional **fixed
start date** (ISO `YYYY-MM-DD` — see **SCHEDULED** below), an optional
**deadline**, and a **"Run as"** selector — *Claude Code (normal)* by default, or
*Codex delegate (patch)* / *Codex delegate (write)* to launch the job into
[`/codex-implement-task-and-claude-review`](#delegate-a-task-to-codex-codex-implement-task-and-claude-review)
instead (Codex implements, Claude only verifies — see below). On a category header (or anywhere off the repo tree) you first pick the repo
from a menu of everything under `$GIT_BASE/<category>/`, including a **"➕ create new repo"**
entry that runs `new-repo.py` for you.

Saved jobs collect under a blue **`── FUTURE ──`** section (shown by default; toggle with
**`tf`**). The `── FUTURE ──` line is shown even when no future jobs are registered (the hint
then reads `(fn adds one)`), so `tf` always has at least this one line to toggle. Select one and press **`r`** (or `Enter`) to launch it: a new tab opens running
`claude --session-id <id> "<prompt>"` in the repo, with the AIM already set — the draft simply
becomes the live session (it reuses the pre-assigned id, so the AIM carries over). Everything
is also scriptable: `ccc new-job -a "…" [-p "…"] [-c REPO] [-w "during holidays"]
[-s YYYY-MM-DD] [-O overseer] [-E executor] [-N] [-K KEY] [-J]`, `ccc jobs [-j]`,
`ccc start-job <id>`. Future jobs are
inert until launched — the daemon never reaps, grades, or alerts on them.

#### Banning Codex for one job (`-N/--no-codex`)

`ccc new-job -N` marks a job **no-codex**: every ccc-owned surface that launches or resumes it
exports `CCC_NO_CODEX=1`, the kill switch all Codex integrations honour (the plan-gate debate,
the optional-offload hook, `codex-in-claude`). One helper — `accounts.session_launch_env` and
its `session_apply_to_environ` / `session_launch_env_prefix` renderings — is the single place
that decides this, and **every** launch surface goes through it: `start-job`, `resume` (the
in-place `execvp`), `resume-job`, the TUI's `r` and its undo-close, `ccc jump`, the
attached-prompt `fire-attached`, the halted-session auto-resume, and snapshot restore (the typed
tab command carries the `export`). An ambient `CCC_NO_CODEX` from the parent shell is never
cleared — the flag is only ever added.

The flag round-trips through the job file (`no_codex: true` in the frontmatter, emitted only when
set so existing files stay byte-identical) and is shown as a `[no-codex]` tag in `ccc jobs`.
It is **refused together with `-j codex` / `-j codex-write`** — that job type *is* Codex work, so
banning Codex would make it unrunnable. The refusal fires at the domain boundary
(`ccc new-job` exits 2; `Store.create_draft` raises; the job-file import writes a sync-error
callout) **and** again at launch, where `ccc start-job` refuses a row that somehow carries both,
before it claims the draft — so nothing is lost.

#### Idempotent registration + JSON output (for automations)

`ccc new-job -K/--idempotency-key KEY` makes registration **create-or-retrieve**: the first call
registers the job, every later call with the same KEY returns that same job instead of a second
one. The claim is a single `BEGIN IMMEDIATE` transaction against a partial UNIQUE index, so even
concurrent creators collapse onto one row. A KEY reused with a **different** `cwd`/`aim`/
`--no-codex` is a caller bug: exit 2 naming the differences, rather than silently handing back
the wrong job. Keyless jobs are never deduplicated (their key is `NULL`).

Two machine-readable outputs, both a single line, schema `version: 1` (additive fields keep
version 1 — read by key):

```commands
ccc new-job -a "…" -c <repo> -K deploy-42 -J
# {"version":1,"session_id":"…","created":true,"account":"private","no_codex":false}

ccc jobs -j
# {"version":1,"jobs":[{"session_id":"…","cwd":"…","aim":"…","draft":true,"archived":false,
#                      "created_at":1756…,"account":"private","no_codex":false,
#                      "idempotency_key":null}]}
```

`--json` suppresses every human line on stdout (warnings still go to stderr). Note the short
options: on `new-job`, `-j` is already `--job-type`, so JSON takes **`-J`**; on `jobs` there is no
clash and it is **`-j`**. Both accept the long `--json`.

**SCHEDULED — future jobs with a fixed start date.** A future job can carry a machine-readable
`start_date` (`-s/--start-date YYYY-MM-DD`, the dialog's *Fixed start date* field, the `e`
form's **`Scheduled for:`** row, or the `start_date` frontmatter key of its job file) in
addition to the free-text `start_when` note.
A dated job leaves the FUTURE block and sinks into its own blue **`SCHEDULED`** section at the
**very bottom** of the table (below FINISHED), ordered soonest-date-first; the date is shown in
blue as a compact `D.M.YY` label (`2026-08-11` → `11.8.26`) **spanning** the `!!!` (importance)
and `head:` (version) cells at the row's start — no column widens and the **id** column stays a
narrow bare hash — and the Obsidian future-jobs dashboard mirrors the same bottom section.
Both the FUTURE and SCHEDULED headers render like the category dividers — a full-width blue rule
from the left edge to the right edge of the screen. Launching a scheduled job **before** its date
is guarded: `ccc start-job` warns (`⚠ start date 2026-08-11 not reached — 39 days early`) and
launches only on an explicit `y` (non-interactive callers must pass `-F/--force`); the TUI's
**`r`**/`Enter` first asks with a *Start anyway / Cancel* dialog and, on OK, passes `--force` so
the new tab never asks twice. The Obsidian ▶ button routes through the same in-tab question. Once
the date arrives the guard disappears and the job launches like any other.

#### Dependent jobs

A job can declare it **depends on another job finishing first** — a single dependency,
stored as the dependency's full session UUID in the `sessions.depends_on` column (`''`/NULL =
none). Set it with `ccc new-job -d <id-or-hash>` (a full UUID or any unique id/4-hex-hash
prefix), or from the TUI `e` editor's **`/depends-on:`** row — a button that opens a picker
over candidate jobs (every session that is not done, not archived, not itself, and would not
form a cycle). Cycles are refused at every write boundary (the CLI, the editor commit, and the
Obsidian file import, which drops the edit with a managed sync-error callout).

A job's dependency is in one of four states, read from the dependency's own row: **satisfied**
(a real completion — `done`, not cancelled), **unmet** (still running / parked / a not-yet-done
future job), **cancelled** (the dependency was `mark-done`-on-draft or `delete-job`-trashed), or
**missing** (no such row). The impartial "looks done" verdict never counts — only the human
`done` flag does.

- **Marker + hoisting.** A row whose dependency is *unsatisfied* (unmet / cancelled / missing)
  wears a red **`|-->`** marker starting at column 0. When the dependency's row is **visible and
  unmet**, the dependent row additionally **hoists** directly under it, indented one level per
  depth; chains nest. A satisfied (or missing/cancelled-but-hidden) dependency shows the marker
  (when unsatisfied) but does not move the row. `ccc ls` shows the same marker plus a
  `depends: <hash> (<state>)` note on the `↳` line. Hoisting is a TUI/`ccc ls` concern only — the
  Obsidian future dashboard shows a neutral `depends: <hash>` hint and keeps its category→created
  sort.
- **Launch guard.** Every launch path consults one shared check: `ccc start-job` warns
  (`⚠ depends on <hash> "<aim>" — <state>`) and launches only on an explicit `y` (non-interactive
  callers must pass `-F/--force`); the TUI's **`r`**/`Enter` folds the dependency into its
  *Start anyway / Cancel* dialog; the Obsidian `launch: true` toggle refuses to spawn and writes a
  managed `blocked: depends on <hash> — <state>` sync-error callout instead. A *satisfied*
  dependency never blocks.
- **Round-trip.** `depends_on:` is emitted in a future-job file's frontmatter (and in all three
  running/done/session mirrors) **only when non-empty**, so dependency-less files stay
  byte-identical.

Each future job also picks **which models it runs on**: `-O/--overseer` and `-E/--executor`
(choices `fable-5` / `opus-4.8` / `opus-4.8-1m` / `sonnet-5` / `haiku-4.5`, both defaulting to
`fable-5`; `opus-4.8-1m` is the 1M-context Opus, launched as `claude-opus-4-8[1m]` and delegating
to plain `opus` — fable-5 and sonnet-5 are natively 1M). At launch the session
runs on the **overseer's** model (`claude --model …`) with an **explicit reasoning effort**
(`claude --effort <level>`, config key `launch_effort`, default `xhigh`; set it to `""` to omit the
flag and let `~/.claude/settings.json`'s `effortLevel` decide — whose own absence means `high`).
If the **executor** differs, the launch
prompt tells the overseer to delegate implementation to Agent-tool subagents on the executor's model
(Fable-5 oversees, Opus executes) while keeping planning, review and integration itself. Both fields
round-trip through the mirror file's frontmatter and Meta Bind selects. In the inline editor (`e`)
the overseer/executor are **clickable dropdowns** (Textual `Select`s) — pick with the mouse or the
arrow keys; invalid input is impossible. Shortcut: **clicking the `overseer ▸ exec` cell** (the
`model` column) of a future-job row opens the editor straight onto the overseer dropdown.

**Launch from outside the TUI:** `ccc open-job <id>` (or `ccc open-job -f/--file <path>`, which
reads the id from a job file's frontmatter) opens a future job in a new iTerm tab exactly
the way the TUI's **`r`** does — it reuses the same helper (a new **login-shell** tab running
`ccc start-job <id>`), creating an iTerm window first if none is open. That login shell gives the
tab a complete `PATH`, so a play button in an Obsidian dashboard can launch a job without hitting
the `execvp("claude")` ENOENT that a bare AppleScript window (no login shell) used to. `open-job`
takes exactly one of the id or `--file` and validates the id is a real, un-archived draft (errors
otherwise; `ccc start-job` remains the in-tab exec that actually replaces the process).

**Where did it land? (tp#90, 2026-09-02).** The launch ladder is iTerm2 via AppleScript →
iTerm2 via the Python API (only when iTerm2's root-owned `disable-automation-auth` switch makes
that path free of Apple events — otherwise the `iterm2` package would re-run the very
`osascript` that just failed, with no timeout) → a window in the persistent tmux session
(`tmux_session`). `open-job` never hides the rung: the human line says where the job runs,
`-j/--json` prints exactly one line `{"version": 1, "session_id": "<uuid>", "launcher":
"iterm_applescript" | "iterm_api" | "tmux"}`, and a tmux landing on an iTerm2-configured Mac adds
a `warning:` on stderr. **Exit 0 iff something launched** — tmux included, the job IS running —
and 1 iff nothing did; consumers (gitlab-ci-watch) read `launcher` to tell a visible tab from a
tmux window, and never retry a launch that returned 0.

Under launchd the AppleScript rung is gated by macOS **Automation (TCC)** for the launchd job's
*real executable path* (the process launchd started — for a job launched by another program,
that program's interpreter, not ccc's). Every Homebrew/uv python patch release is a new path
and therefore a new prompt; an unattended job hits ccc's 10 s `osascript` timeout and falls
through to tmux until Allow is clicked once. Two tools make this visible without dispatching
anything real: **`ccc terminal-probe [-j]`** runs the exact `open-job` ladder with one harmless
command that prints `CCC-TERMINAL-PROBE <nonce>` into the tab it opens (JSON `{"version": 1,
"launcher", "marker"}`; exit 0 iff something launched; a tmux landing closes itself) — run it
from the LaunchAgent in question and look for the marker in a NEW iTerm2 session
(gitlab-ci-watch's `tests/acceptance/launchd_tab_probe.sh` automates that); and **`ccc doctor`**'s
*Terminal* section reads the Automation verdict for ccc's own interpreter (❌ only on an explicit
denial; a never-asked path or an unreadable store is −), plus the Python-API server and
auth-disable switches.

**Terminal guard — a job always starts in its own tab.** `ccc start-job` (and `ccc resume`)
`execvp` claude *in place*, and Claude Code only runs an interactive session when it owns a
TTY. Launched from a pipe, `</dev/null`, or an agent's background shell, the exec'd process
instead consumes its argv prompt as a headless **one-shot** and exits after a single turn —
and its transcript then opens with a `queue-operation` record, which is exactly the signature
`ClaudeAdapter.is_oneshot_headless` matches, so the daemon's `prune_headless` self-heal
deletes the row. Net effect: no tab, no ccc row, and no error logged anywhere. Job
`42fc3505` was lost that way on 2026-08-28 (an agent ran `ccc start-job -u` inside a
`run_in_background` shell instead of `ccc open-job`).

So both commands now check `cli.has_terminal()` (a TTY on **both** stdin and stdout) *before
any state mutation*. With no terminal they hand the launch to a real tab
(`terminal.start_job_in_new_tab` / `resume_in_new_tab`) and exit 0; if no tab can be opened
they refuse with exit 1 rather than exec headless, leaving the draft and its file untouched
(an `--auto` dispatch additionally disarms `fire_at` so the daemon stops retrying). Inside
the spawned tab stdin *is* a TTY, so the guard passes and the exec proceeds — no recursion.

`CCC_START_JOB_HEADLESS=1` opts out for a deliberate headless one-shot (the test suite sets
it via an autouse fixture in `tests/conftest.py`). This is intentionally the *only* escape
hatch: explicitly-launched headless subagents (`claude -p`, `cpriv` runs) still produce
`queue-operation` transcripts and are still pruned, so they stay invisible in ccc — which is
the desired split. Covered by `tests/test_start_job_tty.py`.

Each synced job file carries an in-note **button row** — **▶ Start this job · ✓ Mark job as done ·
🗑 Delete job** (three hidden Meta Bind `meta-bind-button` definitions plus one inline
`BUTTON[start-job, done-job, delete-job]` line, between the frontmatter and `## AIM`), so a parked
job can be launched, finished, or trashed straight from its Obsidian note. Each button runs the
matching vault `obsidian-shellcommands` entry → `ccc open-job|done-job|delete-job --file
{{file_path:absolute}}` against the active (job) file. The buttons are emitted only when the draft
has a real `session_id` — the button-less capture pad stays inert. **✓ done-job** means "this got
done without running": the draft is promoted out of draft-hood (`draft=0, done=1`, sub-goals
reconciled to 100%), its file is archived with a terminal `done` status, and the next mirror pass
writes its snapshot under `done/` — distinct from `ccc mark-done` on a draft, which *cancels* it
(archived, never mirrored). **🗑 delete-job** soft-deletes the row (`archived=1`) and moves the file
to the **`01-llm-tasks/delete/` trash** (keeping the `<cat>/<repo>` substructure) with
`status: deleted`, a `deleted: <date>` stamp and a single **↩ Stage job back in** button;
`ccc restore-job` (the button, the trash dashboard's ↩ column, or the CLI) un-archives the row and
moves the file back to its live spot (`status: registered`) — and can re-register from the file
alone (same UUID) even if the row was pruned. The trash has its own dashboard
(`01-llm-tasks/delete/delete.md`) with an ↩ restore column; the future dashboard deliberately
keeps only ▶ per row — ✓ done / 🗑 delete live inside each job note.

> **Troubleshooting — buttons show "Button ID not Found".** Meta Bind keys its button registry
> by *file path* with refcounted register/unregister per render. When a job file is renamed or
> moved **while its tab is open** (e.g. a repo-category migration relocating
> `future/<cat>/<repo>/`, or mass rewrites hitting an open note), the inline
> `BUTTON[start-job, …]` row can end up looking the buttons up under a different path than the
> hidden definitions registered under — all three chips then read "Button ID not Found" even
> though the file content is correct. **Fix: close and reopen the note (or reload Obsidian,
> Cmd+R).** Nothing is wrong with the file, the Shell Commands entries, or `ccc` — verify with
> `ccc sync-future -v` (expect `errors=0`) if in doubt. Diagnosed 2026-07-04 during the
> home→llms repo moves; a plain restart re-registers everything.

Below the prompt, every job file ends in a **`## Controls`** section of labelled Meta Bind inline
selects — **status**, **job type**, **overseer** / **executor** (the `llm_overseer` / `llm_exec`
model fields), **account** (which Claude account the job launches/bills under — emitted only when
more than one account is configured, see the multi-account section; also settable via
`ccc new-job -A <label>` and the TUI's account selects), and **repo** (one box listing every
`<cat>/<repo>` on disk, the file's own repo first). These dropdowns are the intended edit surface:
Obsidian's native Properties panel can only *suggest* values already used somewhere in the vault,
it has no fixed option lists.

A future job also mirrors to an editable markdown file in the Obsidian vault. The **id**
column shows the bare 4-hex display hash, and the **`/next-step`** column doubles as the
draft's **tags/notes column**: any typed `@tags` plus the free-text `start_when` note (e.g.
`@home · tomorrow evening`, note in blue) — `ccc ls` shows the same as `next:` / `when:`
detail lines. The draft's configured models stay a colour-coded `overseer ▸ executor`
readout in the **model** column; in the job-details pane they render **on the `Status:` line
itself** (`/overseer: … /executor: …`, plus `/account: …` in multi-account mode), and every
draft shows a **`Scheduled for:`** line directly under `Status:` — the blue `D.M.YY` date when
set, a grey `—` when not. While editing (`e`) those readouts drop out of the head (their
editable rows — `/overseer` `/executor` `/account` `Scheduled for` — are the **top rows of the
form**, so each option renders exactly once). All of it is editable inline in the job-details pane
(`e`); once the job has synced to its file, the 4-hex hash becomes a clickable link
(`obsidian://open`) that opens it directly — same in `ccc ls`. That's a Rich/OSC 8 hyperlink, so clickability depends on the terminal
(⌘-click under iTerm2); the guaranteed path is the **`oo`** chord (type `o` then `o` on the
selected job) — it shells out to `open` regardless of terminal support. A draft that hasn't
synced yet (no file) has nothing to open — `oo` just notifies.

You can also **capture a new job as a file first**: `ccc new-prompt [-r cat/repo] [-o]` writes a
fresh, hash-named draft page under the future root (prefilled frontmatter + Meta Bind repo
picker) and prints its clickable `obsidian://` path (`-o` opens it in Obsidian); fill in the
AIM/prompt and flip `status: ready` to register it. The persistent capture pad
`01-llm-tasks/new-prompt.md` is the manual variant — write into it, flip it to `ready`, and the
sync registers its content into a hash-named file and resets the pad (a deleted pad self-heals).

**Whole-lifecycle vault mirrors.** The FUTURE file is the *editable* draft; the other two lifecycle
phases get **export-only** read mirrors so every tracked session is one markdown file in the vault:
**RUNNING** (`01-llm-tasks/running/<cat>/<repo>/<slug>-<hash>.md`, for every active session) and
**DONE** (`.../done/…`, the final snapshot of every finished session). They are generated from the
store (a banner + `ccc_mirror: running|done` frontmatter mark them; user edits are overwritten),
byte-stable (rewritten only on a real change — the only timestamps are `created`/`last_response`/
`done_at` as ISO dates, so routine passes are no-ops), and cleaned up only via the `ccc_mirror`
marker — a file without it is never touched. Every mirror also carries **`model:`** and
**`effort:`** frontmatter keys — the model that **actually answered** (the last real
`message.model` in the transcript, mapped to a ccc short name like `fable-5`; `""` until a turn
exists) and the observed reasoning effort (captured while the session is live: an explicit
`--effort` launch flag is authoritative, else the global `effortLevel` from
`~/.claude/settings.json` fills it once; `""` for a session never observed live — a historical
parked session is never backfilled with today's default). Trust them over
`llm_overseer`/`llm_exec`, which are job *config* with a `fable-5` database default — for a session
never launched as a ccc future job those are defaults, not observations. The same observed pair
renders in ccc itself as the **`model` column** (right before `/aim`, in both the TUI and
`ccc ls`, e.g. `fable-5·xhigh`; a draft never ran, so its cell shows the CONFIGURED
`overseer ▸ executor` pair instead — compacted to the single name when both are equal). The top **`## AIM (1)`** section always shows the
session's *first* recorded AIM (the original done-condition — never the latest sharpened revision),
with the current revision's short label appended (`↳ short (current): …`) once available. Each body
also carries an **`## AIM history`** section
(every revision, `1.`→`N.` oldest→current) and a **`## Prompts`** section — every prompt the `sp`
peek box shows, as a numbered list from the same source `ccc peek` reads (so they never diverge;
capped at the last 200 prompts / 256 KiB for a pathological session). Filenames are slugged from the
session's **first** AIM, so they never rename when the AIM is sharpened mid-session (a stale
current-aim-named mirror is renamed on the next pass). The daemon refreshes them each pass and every
lifecycle command (`start-job`, `mark-done`, `rm`, `unlaunch`) fires a detached `ccc sync-mirrors`.
Both are **off by default** (fresh-install inert); enable with `mirror_running = true` /
`mirror_done = true`.

**Full-session mirrors (`01-llm-tasks/sessions/`).** A third export-only tree holds ONE file per
tracked session (parked, running or done — membership is independent of the running/done
switches; its own kill-switch is `mirror_sessions = false`, root `sessions_dir`) with the **whole
conversation** rendered terminal-like: `## (N) you` / `## claude` sections, every prompt and reply,
and dim `⏺ Tool(input…)` / `⎿ result…` one-liners per tool call — the exact content the peek
panel's **session** tab shows (`command_center/sessionmd.py` is the single renderer; capped at
512 KiB keeping the newest whole turns). The file keeps ONE stable path across the session's whole
life (running → done), and every running/done mirror links to it TWICE: a **`session` frontmatter
property** (a clickable chip at the very top of the note, next to the `transcript` property
holding the raw `.jsonl` path) and a `[[…|full session]]` wikilink at the top of its
`## Transcript` section — so from any running-job note in Obsidian one click opens the up-to-date
full session. Embedded verbatim text is **fence-safe**: line-leading ``` runs in user prompts are
backslash-escaped (a pasted terminal snippet can open a code fence it never closes, which would
swallow the whole rest of the note), and a dangling fence in an assistant reply is self-closed —
Claude's balanced code blocks keep rendering as code. In the TUI, the **`os`** chord (type `o`
then `s` on the selected row) opens the selected session's full-session file in Obsidian directly.

**Vault dashboards.** Three dataviewjs dashboards sit one level above the mirror trees (outside the
queried folders, so they never mirror themselves): **`01-llm-tasks/future.md`** (editable — native
dropdowns write frontmatter, ▶ launches via `ccc open-job`), **`01-llm-tasks/running.md`**
(read-only over the RUNNING tree, ⤢ focuses a live tab via `ccc focus-job`) and
**`01-llm-tasks/parked.md`** (read-only view over the SAME running tree filtered on
`status: "parked"` — every ☾ row, i.e. closed-but-unfinished sessions; ▶ resumes in a new iTerm tab
via `ccc resume-job`). Running and parked stack the Models cell identically: `ran` — the observed
`model:` key with its `effort:` next to it — above the `ovr`/`exe` config chips. Gotcha baked into all three: the Hash cell derives from `session_id`,
never from the frontmatter `id` — Dataview coerces duration-shaped id strings (`"035d"` = 35 days
renders as `P35D`).

**Verifying dashboards render (for agents/LLMs).** Obsidian normally runs without a debugging port,
so a dashboard change can't be checked headlessly. Relaunch it with the Chrome DevTools Protocol
(CDP) enabled, then drive it with Playwright:

```commands
osascript -e 'quit app "Obsidian"'   # wait for exit, then:
open -a Obsidian --args --remote-debugging-port=9223   # 9222 is the shared login Chromium
curl -s http://localhost:9223/json/version             # sanity: Obsidian answers over CDP
```

Then from Python (`uv run --with playwright python …`): `connect_over_cdp("http://localhost:9223")`,
find the page where `typeof app !== 'undefined' && !!app.workspace`, run
`app.workspace.openLinkText('<vault-relative-note>', '', false)`, wait ~8 s for dataviewjs to
render, and `page.screenshot(...)`. The flag does **not** persist — a normal manual relaunch drops
the port, so re-run the two commands above when needed. (Dataviewjs blocks eval as a *Program*:
no top-level `return`; the dashboards wrap their body in an async IIFE for that reason.)

`ccc unlaunch <id>` reverses a launch
(back to a FUTURE draft — requires the tab be closed first), and `ccc start-job` is **resume-aware**:
if the job already has a transcript in its current repo it relaunches as a bare `claude --resume`
(continuing the original prompt) instead of re-submitting it. `ccc resume-job <id>` is the
shell-invokable equivalent of pressing **`r`** on a parked (`☾`) row — it opens the session in a
**new** iTerm tab (never execs in place, so the Obsidian parked dashboard's ▶ button can spawn it),
focuses the existing tab instead when the session is still live, and refuses a transcript-less or
draft session.

**Dead rows (a launched job that never had a turn).** `ccc start-job` clears a future job's draft
flag and execs `claude --session-id <id>`; if that tab is closed before the first turn (or the work
happened in a different session), the row is stranded — no longer a FUTURE draft, yet with no
`<id>.jsonl` it can't be resumed. Pressing **`r`** on such a row opens a triage dialog: **Restore to
FUTURE** (re-lists it as a job to run — offered only when the launched-draft file is still
recoverable), **Delete** (removes it from the command center), or **Keep**. These phantoms carry an
AIM inherited from the launch, so the contentless guards spare them; `ccc prune` and a daemon pass
now reap them anyway (they are `[orphan]`-tagged in `prune`'s output) — a row is only ever treated
as dead-launched when it is parked, not live, `prompt_count == 0`, **and** has no transcript under
any account (so a real session whose transcript was merely deleted, which keeps a non-zero
`prompt_count`, is never auto-deleted).

> **Headless `claude -p` sessions never appear.** Both row-creating paths skip
> them: the adapter ignores live `~/.claude/sessions/<pid>.json` entries whose
> `entrypoint` starts with `sdk` (the daemon's own summary calls, etc.), and the
> hooks bail when `CLAUDE_CODE_ENTRYPOINT` says `sdk-*`. The hook guard matters
> because a `claude -p` spawned *from inside* a real session (e.g. `ai.py`'s
> commit-message generation) inherits that session's `CLAUDE_SESSION_AIM` and cwd
> — without it, every such run leaked a duplicate row stamped with the parent's
> AIM. `ccc prune` mops up any that a pre-fix process left behind (it spots them
> by their transcript: a `claude -p` one-shot opens with a `queue-operation`
> record), and a daemon pass does the same automatically (`prune_headless`, on by
> default).

`set-*` resolve the target session from `--session`, then `$CLAUDE_SESSION_ID`,
then the live session running in the current directory. In a Claude Code
session, use the slash commands `/aim` `/next-step` `/done` `/block` `/deadline`.

### Parked prompts — auto-fire at the token reset (`ccc park`, `new-job --at-reset`)

A ready-made prompt you would otherwise leave sitting unsent in a Claude Code composer
until the usage limit resets can be **parked** instead — registered now, launched
automatically the moment the rate-limit window resets. (The composer draft itself lives
only in process memory and can never be captured; parking replaces that habit rather
than reading it.)

Two front-ends, one persistent mechanism — every parked prompt is a normal future-job
draft row armed with a fire time (`fire_at`, epoch seconds) and the window that produced
it (`fire_window`):

- **`ccc park [PROMPT] [-c] [-N] [-n] [-w WINDOW] [-b SEC] [-a AIM] [-e]`** — same-tab
  flow. With no prompt argument and no piped stdin, the **floating "ccc park panel"**
  opens (the editable sibling of the s+p peek panel): a dark panel with the target
  repo · account · fire time in the header and a multi-line editor — **⌘↵ parks, ⎋
  cancels**, plain Return is a newline. `-c/--clipboard` prefills the panel for
  review; `-e/--editor` uses `$EDITOR` instead (with a fallback chain
  `$EDITOR → $VISUAL → vim → vi → nano` when the configured binary is missing).
  The job is registered FIRST (Ctrl-C, a closed tab, or a reboot never loses it),
  then the command waits in the tab with a live countdown (tab title `⏳42m → auto`)
  and at the reset execs the job right there via the canonical `ccc start-job` path.
  Enter fires early; Ctrl-C keeps the job but disarms auto-fire; `-n/--no-auto` rings
  instead of launching; `-N/--now` skips the wait. Shell alias: `alias qp="ccc park"`.
- **`ccc park -g/--grab`** — the **global q+p chord** (Karabiner: hold `q`, tap `p`
  while iTerm2 is frontmost, mirroring the s+p peek chord): the park panel pops over
  whatever tab you are looking at (resolution as in peek: tracked session first,
  tab cwd fallback). Two targets, chosen automatically and named in the panel
  header:
  - **Over a live Claude session tab (default: ATTACH)** — the prompt is parked
    ON that session (`prompt` + fire time on its row, no new job): the "open the
    session with its AIM first, define the real prompt with q+p" workflow. At the
    reset the daemon **types the prompt into that session's tab** (bracketed
    paste + ⏎, same context and billing); if the tab or process is gone it opens
    a resume tab instead (`ccc fire-attached` → `claude --resume <id> "<prompt>"`,
    double-delivery excluded by a one-shot claim). `-N` delivers into the tab
    immediately; `-j/--new-job` forces the detached behaviour below; the session's
    own statusline shows `⏳ parked prompt for THIS session fires …` while armed.
    **Re-edit:** a second q+p over the same session reopens the panel PREFILLED
    with the armed prompt — ⌘↵ overwrites prompt + fire time. **"continue" works:**
    while armed, the session's hooks (SessionStart + every user prompt) announce
    the parked prompt, so telling the session to continue/run it makes it call
    `ccc claim-fire <id>` — a one-shot claim that prints the full prompt, disarms
    the auto-fire, and hands delivery to the session itself (the daemon and a
    manual claim can never both run it).
  - **Over any other tab (DETACHED)** — an armed future job for that tab's repo +
    account; the daemon launches it as a NEW session in a new tab at the reset
    (`-N` launches immediately). Feedback arrives via notify — there is no
    terminal attached to a chord launch.
- **`ccc new-job -R/--at-reset [-W WINDOW]`** — headless flow: the job is armed and the
  **daemon** fires it in a new tab within ~5 minutes after the reset (it warns at
  registration when the daemon service is not installed).

Scheduling is **deterministic**: the fire time is the selected window's `resets_at`
plus a small buffer (`-b`, default 90 s) — utilization never decides the schedule, and
missing/stale usage data is a loud registration error, never "start now". The window
is `five_hour` by default; `seven_day` / `fable_week` only on explicit selection.

Dispatch correctness: the daemon only touches jobs ≥ 2 min past their fire time (a
live `ccc park` waiter always wins its own tab), re-arms the fire time 15 min forward
BEFORE dispatching (a crash or a tab that never ran simply retries — nothing is lost),
and launches with `start-job --auto`, which **never bypasses** the start-date /
dependency / account guards — a tripped guard disarms the job and keeps it as a draft.
The actual promotion is a one-shot atomic claim, so two racing launchers can never
start the same job twice. If a fresh usage snapshot shows the job's OWN window still
exhausted at fire time, the fire is postponed to that window's next reset (other
windows never hold a job back). Markers: the park tab title, `ccc jobs`
(`[⏳ fires 14:03 (in 37m)]`, overdue-aware), and a `⏳ parked prompt fires …` chip in
every session's statusline rows. `restore-job`/`unlaunch` clear a stale fire time —
a restored job never silently auto-launches.

### Delegate a task to Codex (`/codex-implement-task-and-claude-review`)

Hand the **implementation** of a task to OpenAI Codex and have Claude only *oversee* it — so
the heavy generation runs on the Codex (ChatGPT) subscription, **not on Anthropic tokens**.
From a Claude Code session: `/codex-implement-task-and-claude-review [--write] [--no-takeover] [model] <task>`.
It runs a bounded loop: (optional read-only **scout** → plan) → Codex implements + self-checks →
Claude verifies by running the project's checks → on failure gives concrete feedback → Codex
revises; if Codex still fails after round 3, Claude announces it and takes over (unless
`--no-takeover`). The **first output line is always the model**, e.g. `model: gpt-5.5 (effort xhigh)`.

**Codex does the code discovery, not Claude.** Claude does *not* pre-read the repo to "build the
task" — that would just duplicate the reading Codex must do anyway and burn the very tokens this
command saves. Claude supplies only intent + acceptance criteria; Codex (running `-C <repo>`)
reads the code itself. Each Codex round runs in the **background** and the harness re-invokes
Claude the instant it finishes — no fixed wait, no polling.

- **Default (patch)** keeps Codex read-only: it returns a `git apply`-able diff that Claude
  applies and verifies — your global Codex read-only lockout is untouched.
- **`--write`** lets Codex edit files directly (`workspace-write`, that call only) and run the
  tests itself; Claude reviews the resulting git diff.

The Codex **model + reasoning effort** for both this command and `/codex-debate` are governed by
one script, **`codex-in-claude.py`** (on `PATH`; it's this repo's folder, added to `PATH` in
`.zshrc` — the skill/command call it by bare name, so the repo can move):

```commands
codex-in-claude.py models                              # list models (* = configured)
codex-in-claude.py set-model gpt-5.5 --for delegate-review   # or --for debate / --for all
codex-in-claude.py set-effort high                     # low|medium|high|xhigh|default (model's own)
codex-in-claude.py get-model --for debate
```

`delegate` runs are **supervised**: `codex exec` lives in its own process group (killed whole
on timeout, stall, or parent death — never an orphan editing the workspace), the wall timeout
scales with effort (low 600s … xhigh 2700s; `-t 0` = no wall, the recommended mode when a task
just takes as long as it takes), and an idle watchdog (`-i`, default 900s of silence) culls
hung runs. Each run heartbeats a tiny JSON and reports its codex session as `### SESSION`:

```commands
codex-in-claude.py delegate -t 0 --write -C <repo> "task…"   # unbounded, stall-guarded
codex-in-claude.py runs                                # one line per in-flight run (cheap check)
codex-in-claude.py delegate -R <session> -f "fix X"    # resume that session's context (round 2+)
codex-in-claude.py delegate -P notes/map.md "task…"    # curated repo map (-M = no map)
codex-in-claude.py delegate -n "task…"                 # dry run: show assembled prompt, launch nothing
```

The prompt is auto-prefixed with the repo's `repo_scope_short.md` (else a git top-level
summary) so codex starts oriented, and it is told its time budget explicitly.

The same selector powers **future jobs**: a `new-job -j codex` / `-j codex-write` draft (or the
TUI "Run as" menu) launches straight into `/codex-implement-task-and-claude-review`, so a parked
task gets done by Codex and verified by Claude when you start it.

#### Codex launch policy — permission profiles, the `-C` root, and the session journal

Every `codex exec` ccc starts — the `delegate`/scout rounds and ccc's own cheap text calls —
is assembled by **one** module, `command_center/codex_launch.py`. Nothing else builds a codex
command line.

**Named permission profiles, never `-s/--sandbox`.** Codex ≥ 0.150 replaced the legacy
`sandbox_mode` key with named profiles (`default_permissions = "<name>"` + a
`[permissions.<name>]` table). The two must not be mixed: passing `-s`/`--sandbox` forces the
LEGACY sandbox and silently **drops** the profile's deny rules — credential stores, the
workspace's own `.env`/`*.pem`/`*.key`, the network block. ccc therefore emits
`-c default_permissions="hardened-ro"` for a read round and `-c default_permissions="hardened-rw"`
for a write round, and never emits `-s` at all.

`hardened-rw` is **opt-in**: a write round runs only when the active `$CODEX_HOME/config.toml`
really declares `[permissions.hardened-rw]`. Without it ccc refuses (exit 2,
`no hardened-rw profile configured; refusing legacy workspace-write`) rather than falling back to
a rule-free `workspace-write`. You only ADD the table — the file's own
`default_permissions = "hardened-ro"` stays put, and ccc selects the write profile per
invocation with `-c`, so an interactive `codex` is unaffected. The profile is the read-only one
plus workspace write; keep its deny list identical to `hardened-ro`'s and change only `extends`,
which must be `":workspace"` (`":workspace-write"` is **not** a built-in name and fails config
load with `cannot extend unsupported built-in profile`):

```toml
[permissions.hardened-rw]
description = "Workspace-write implementer; credential stores unreadable; sandbox network off"
extends     = ":workspace"

[permissions.hardened-rw.filesystem]
glob_scan_max_depth = 3
"~/.ssh"                   = "deny"
"~/.aws"                   = "deny"
"~/.gnupg"                 = "deny"
"~/.kube"                  = "deny"
"~/.netrc"                 = "deny"
"~/.pgpass"                = "deny"
"~/.docker"                = "deny"
"~/.codex/auth.json"       = "deny"
"~/.config/gcloud"         = "deny"
"~/.config/sops"           = "deny"
"~/.config/rclone"         = "deny"

[permissions.hardened-rw.filesystem.":workspace_roots"]
"**/.env"   = "deny"
"**/.env.*" = "deny"
"**/*.pem"  = "deny"
"**/*.key"  = "deny"
"**/*.sops" = "deny"
```

**The `-C` workspace root is always explicit and always validated.** `resolve_workdir`
resolves it strictly (symlinks included) and then:

| Case                                                | Result                                                           |
| :-------------------------------------------------- | :---------------------------------------------------------------- |
| `$HOME` itself, or any directory containing `$HOME` | **refused** (exit 2) — a write round there owns the account      |
| a path that does not exist / is not a directory     | **refused** (exit 2)                                             |
| **implicit** cwd (no `-C`) inside a git work tree   | accepted                                                         |
| **implicit** cwd NOT in a git work tree             | **refused** (exit 2) — pass `-C` if that really is the workspace |
| **explicit** `-C` on a non-git directory            | accepted, and `--skip-git-repo-check` is added                   |

`--skip-git-repo-check` is now passed only in that last case instead of unconditionally, so a
mistyped root fails loudly instead of being waved through.

**The session journal.** Each successful launch appends one line to
`$CODEX_HOME/ccc-sessions.jsonl` (mode `0600`):
`{"ts", "session_id", "resolved_cwd", "permission_profile", "write"}`. `--resume <id|last>`
resolves through it and is honoured **only** when the session was launched by ccc on this seat,
its read/write mode matches the round being asked for, and its recorded root still passes
`resolve_workdir` today — otherwise exit 2 with the reason. This matters because
`codex exec resume` inherits the ORIGINAL session's permissions and working root while `--write`
is recomputed from the new command line; without the journal a resume could quietly run a write
round in a root the current policy rejects.

**ccc's own text calls** (`short_aim` via `run_codex`) go through the same policy plus
`--ephemeral` (no session file), a throwaway empty `mkdtemp()` as `-C` (so a label call can
neither be confused with nor write into a repo), and one `-c mcp_servers.<name>.enabled=false`
per configured MCP server. They deliberately no longer pass `--ignore-user-config`, which would
also drop the permission profiles. There is no wholesale "no MCP" switch: codex's `-c` deep-MERGES
tables, so `-c mcp_servers={}` parses fine and changes nothing (verified against codex-cli
0.150.1) — servers have to be disabled by name, which is why the disable list is derived from the
active config.

### AIM quality (low score → red chip), progress grading & weighting

A vague AIM (e.g. *"improve the progress bar"*) yields ungradeable sub-goals and a
stuck bar, so the center scores every AIM for specificity (0–100):

- **Two-tier score** — an instant offline lexical estimate the moment the AIM is
  set (`set_aim` is the single chokepoint for CLI, TUI **and** the `SessionStart`
  hook that seeds `$CLAUDE_SESSION_AIM`), refined out-of-band by one cheap LLM call
  (`ccc score-aim`, spawned detached — routed through the pluggable score-backend
  ladder below). **Every AIM is always scored**: a daemon pass backfills any row still
  at the `-1` sentinel (and re-fires the refine), so no AIM silently escapes the vague
  check.
- **Pluggable score backend — a fallback ladder, not just `claude -p`.** The refine call
  walks `score_backends` (default `["claude"]`) in order and the **first rung that returns
  non-empty text wins**:
  - `copilot` — GitHub Copilot via `opencode run -m github-copilot/<copilot_model>`;
  - `gemini` — `gemini -p` (optional `gemini_model`);
  - `codex` — `codex exec` (its own model resolution);
  - `claude` — `claude -p` on `score_model` → `llm_model`;
  - `custom` — the escape hatch below.

  Put `copilot`/`gemini`/`codex` ahead of `claude` and the concreteness score **moves off
  Anthropic tokens** whenever one of those CLIs is available, with `claude` as the last-resort
  rung. Unknown rung names are skipped with a stderr warning; if **every** rung fails the score
  degrades to the offline lexical estimate. `ccc init` writes the ladder it detects on your
  machine (in the order copilot, gemini, codex, claude), and `ccc doctor` reports per-rung
  availability. This is a **deliberate behaviour change** — the public default (`["claude"]`)
  keeps the old single-backend behaviour; add the other rungs to opt in.
- **`custom` score backend (escape hatch).** Set `score_custom_command` to any shell command:
  ccc feeds it the full scoring prompt on **stdin** and reads the model's raw text response from
  **stdout** (a non-zero exit → next rung). This routes the score call through your own
  multi-provider router (e.g. a local `ai.py`-style script) without ccc depending on any private
  tool. The JSON extraction (the first `{…}` object) is identical for every rung. Preview which
  backend serves a candidate — the `--dry-run` JSON now carries the serving rung:

  ```commands
  ccc score-aim --dry-run "<candidate>"   # → {…,"backend":"codex"}  ("backend":"lexical" if all rungs fail)
  ```
- **Per-action labels for custom routers (`CCC_LLM_PURPOSE` / `CCC_LLM_NOTE`).** Every
  headless call ccc makes carries a **purpose** label (`aim-score`, `aim-met`,
  `subgoal-drift`, `subgoal-derive`, `subgoal-grade`, `summary-nextstep`, `short-aim`)
  and a **note** (the session's first AIM, collapsed to one line). Both are exported
  into the backend subprocess's environment as `CCC_LLM_PURPOSE` / `CCC_LLM_NOTE`
  (omitted when empty), so a `score_custom_command` / `llm_custom_command` router can
  **log which action and which session** a call served — or route each purpose to a
  different provider/model. They are metadata only and never change what is generated;
  the `codex` rung's CLI has no label support, so labels are dropped there.
- **`llm_custom_command` — route EVERY headless call, not just the score.** The score
  ladder above covers only AIM scoring; the other checkers (drift, AIM-met, sub-goal
  derive/grade, summaries, the claude short-aim backend) go through one `run_model`
  chokepoint. Set `llm_custom_command` to a shell command with the same contract as
  `score_custom_command` (full prompt on **stdin**, raw model text on **stdout**,
  labels in the env as above) and **all** of those calls route through it — moving
  ccc's own housekeeping LLM cost onto whatever provider your router picks. A failed
  run (non-zero exit / empty output) degrades to the built-in headless `claude -p`,
  which is env-pinned to the `llm_account` config (default: the default account) so it
  can never bill an ambient work seat. `""` (the default) disables the hatch.
- **The score is shown** as a leading chip in the `/aim` column of `ccc ls`, the
  TUI (table + detail) and the status line: `NN%`, or `-1` while a score is still pending.
- **Short-AIM label (scannable column text + status line).** The full AIM is kept verbatim
  (detail pane, `aim-history`), but the narrow `/aim` **column** (its revision (1) label, see
  below) and the in-session **status line** (the current AIM's) render a ≤10-word label —
  `implement X`, `maria: ws reconnect` — so running sessions are tellable apart at a glance.
  It is generated out-of-band on every AIM change by a cheap **codex** run (`codex exec`,
  via `ccc short-aim`, spawned detached) — keeping the cost off Claude tokens — and a daemon
  pass backfills any session still missing one. The backend is pluggable
  (`short_aim_backend` = `auto` (codex if on PATH else claude, the default) | `codex` | `claude`,
  `short_aim_model`); it is **off by default** (fresh-install inert), enable with `short_aim = true`.
  On any failure the column falls back to the full AIM. Preview a label without saving:

  ```commands
  ccc short-aim --dry-run "<candidate aim>"   # → e.g. "implement short-aim column"
  ```
- **Below `aim_score_threshold` (default 50) only the `NN%` score chip renders red** in
  `ccc ls`, the TUI (table, detail, AIM history) and the status line — the AIM text itself
  stays its normal colour, so the red flags the *quality*, not the goal. The status line also
  keeps its dim `⚠ vague — sharpen it` nudge.
- **Agent-driven sharpening with an independent checker.** While the AIM is vague, the
  `UserPromptSubmit` hook nudges the running session **every turn** (`sharpen_every_n_turns`)
  to rewrite it — *keeping your goal intact, only making it concrete* — grounded in what the
  session has actually been doing (files edited, todo list, task in progress). The agent
  drafts, then verifies each candidate against the **independent rubric checker** (a separate
  score-backend call — the ladder above, `claude -p` on `score_model` by default — blind to the
  agent's reasoning), iterating on its `missing` hint until it clears the bar:

  ```commands
  ccc score-aim --dry-run "<candidate>"   # → {"score":84,"criteria":{...},"reason":"…","missing":"…"}
  ```

  The rubric is published (`aimscore.AIM_RUBRIC`) and reproducible — four criteria summing to
  100: observable end-state (30), objective check (30), bounded scope (20), no vague verbs (20).
  Once a candidate passes, the agent **auto-applies** it (`ccc set-aim`) and prints old→new + the
  one-line revert. The goal's meaning is never changed — only how concretely it is stated.
- **Changing the AIM** drops the auto-derived checklist and resets the grading offset, so a
  fresh, AIM-aligned checklist re-derives (no stale 67%). For the turn it changed in, the status
  line shows the transition `/aim (N-1): <old>  ====> /aim (N) <new>` — always on **one** row
  however long (status-line rows are scarce; an over-wide row soft-wraps in the terminal rather
  than being split up), reverting to the plain `/aim (N):` row at the next prompt.
- **Running index** — the `/aim` prefix carries the current AIM's 1-based number, both in the
  Claude Code status line (`/aim (1):`, `/aim (2):`, …) and the TUI detail pane: `1` is the first
  AIM ever defined, incrementing each time it changes. An AIM that predates history tracking shows
  as `(1)`.
- **The status line always shows revision (1)** — once the AIM is sharpened past its first
  revision, the Claude Code status line prints an `/aim (1):` anchor row (dimmed, revision (1)'s
  own short label when it has one) above the current `/aim (N):` row, so the done-condition you
  typed yourself stays on screen for the whole session — same first-plus-last shape as the TUI
  detail pane. The row is omitted only when it would duplicate what is already visible: a
  single-revision session, and the turn the AIM moves off revision (1) (the transition row
  `/aim (1): <old> ====> /aim (2): <new>` already carries it). Sessions whose AIM predates
  history tracking have no recorded revision (1) and get no anchor row.
- **The `/aim` column shows revision (1)** — the TUI table (whose header then reads
  `/aim (1)`) and `ccc ls` render the done-condition **as you first typed it**, not the latest
  sharpened wording. The column is how you recognise a row, so it stays put while the AIM is
  re-stated over a session's life; the *current* AIM is never more than a glance away (the
  in-session status line, the `job details` pane, `ccc aim-history`). Each revision carries its
  own short-AIM label, so the column stays scannable. Sessions whose AIM predates history
  tracking have no recorded revision (1) and show their live AIM. Set `aim_column = "latest"`
  to put the current AIM back in the column (only that exact value opts out; the header drops
  the `(1)` marker with it). Pressing `Enter` on the column still edits the **current** AIM (a
  new revision) — the `e` form's `/aim (1):` line is what rewrites the original.
- **Detail-pane AIM (first + last only)** — the `job details:` pane shows just the **first** AIM
  ever defined (`/aim (1):`) and the **last/current** one (`/aim (N):`), never the middle
  revisions; when the AIM has only one revision (or predates history) just the single `/aim (1):`
  line shows. The full progression lives in `ccc aim-history` / the `ah` chord.
- **Editing the first AIM** — `/aim (1):` is an editable line in the `e` form (and
  `ccc set-aim -f/--first "<text>"`), for when the *original* done-condition was stated badly and
  the pane/history keeps showing that wording. It is rewritten **in place**: revision 1's text is
  replaced (re-scored, its stale short-AIM label dropped), **no** revision is appended, the running
  index never shifts, and the current AIM is untouched. The **short-AIM labels are regenerated**
  with it — revision (1)'s (what the `/aim` column renders) and the current AIM's, whose
  generator is hinted with the original wording, so both went stale. The rewrite drops them
  (column and detail fall back to the full text) and spawns `ccc short-aim`, which rebuilds
  both; without that the column would keep showing the pre-edit wording while the detail pane
  already shows the new. Emptying it is refused — history never
  loses where the goal started. When that first revision *is* the current AIM (a single revision,
  or an AIM predating history) the live AIM is rewritten with it and any stale DONE verdict is
  dropped; the sub-goal checklist is kept, since restating a goal is not changing it.
- **AIM history** — every (re)definition is recorded, so you can see how the goal got sharper.
  Review the full first→current progression with `ccc aim-history`, the `/aim-history` slash
  command, or the TUI **`ah`** chord (type `a` then `h`; a bare `a` still edits the AIM). Each
  revision is numbered `1.`, `2.`, … (matching the status-line index) and shows its specificity
  score and timestamp, current marked — plus the short-AIM label generated for that revision
  (often close to the *original* `(1)` wording, which the generator is hinted to stay near).
- **Grade-after-turn** — with `grade_on_turn` (off by default; opt in) the Stop hook spawns a
  detached, debounced (`grade_debounce_sec`) grader so the bar updates seconds after
  a turn instead of waiting up to 5 min for the daemon (which stays as a fallback).
- **Weighted sub-goals** — derived sub-goals tagged *essential* count double; the
  bar is `Σ(checked·weight) / Σ(weight)` (identical to a plain count when unweighted).
- Sub-goals are linted for verifiability: `ccc subgoals` warns on vague items, and
  the auto-deriver drops ungradeable ones — **and ceremony steps** (open/merge a PR,
  push, commit, deploy, release) unless the AIM explicitly asks for one. This stops a
  permanently-unsatisfiable *"Pull request merged to main"* from capping progress in a
  direct-push workflow.
- **Full re-grade on idle** — the conservative per-turn grader judges only the unseen
  delta, so a behavioural goal whose evidence is split across turns can stay unticked.
  When a session goes idle the daemon re-grades the **whole** transcript once (leaving
  the delta offset intact) so the bar catches up.
- **Self-tick nudge** — when a checklist is *partly* done with items left unticked, the
  `UserPromptSubmit` hook reminds the agent (every `nudge_unchecked_every_n_turns`, default
  4) to `ccc check` what it actually finished — the agent is a better judge than the
  conservative auto-grader.
- **Manual progress override** — set a fixed percentage on any session from the TUI
  (`Enter` on the `progress` column, or the `progress %` line in the `e` form; blank
  returns to auto). A set value **wins over the sub-goal ratio at every bar site**
  (TUI table + detail head — labelled `(manual)` there — `ccc ls`, the status line and
  `ccc aim`); stored as `sessions.manual_progress`, rendered through the single helper
  `models.effective_progress`.
- **Marking done reconciles the bar** — `ccc mark-done` (and TUI `d`) ticks every
  remaining sub-goal and **clears a manual progress override**, so a done session reads
  100% instead of stranding at "2/5" (or a stale manual 40%). The human's done verdict
  is authoritative; reopening (`--undo`) leaves ticks as-is.
- **Machine-check predicates** — attach a shell command to any sub-goal with
  `ccc subgoal-check <n> "<cmd>"` (e.g. `pytest -q`, `gh pr view 42 -q .state`,
  `test -f dist/app`); each grading pass ticks it deterministically when the command
  exits 0 (no LLM, and the LLM never overrides it). Same trust model as
  `set-donecheck` — user-authored only; keep them fast (they run on every pass).
- **Live todos** — the agent's `TodoWrite` / Task list (forwarded each turn by the
  `PostToolUse` hook) renders as a one-line `done/total` + checkbox strip
  (`☒`/`◧`/`☐`) in the status line, and in full in the TUI detail pane for the
  selected session.

### Adaptive sub-goals + impartial drift checker

The AIM and its checklist form a **self-modifying goal loop** — the in-session agent
sharpens the AIM each turn and re-derives sub-goals from it. Left unchecked, an agent
can quietly move its own goalposts (drop scope, weaken a goal, inflate the bar). Two
mechanisms keep it honest:

- **Adaptive checklists** — auto/agent-authored lists (and any manual one set with
  `ccc subgoals --adaptive`) re-align to the **latest AIM** when it changes. The
  checklist's AIM revision is tracked (`subgoals_aim_rev`); when it lags, a hook nudges
  the agent to **smart-merge** the list (`ccc subgoals --merge`) — ticks carry over for
  items whose wording is unchanged, so completed work is preserved. Manual lists are
  **pinned** by default. The TUI detail pane labels each checklist with its origin:
  `Sub-goals · auto (claude-haiku-4-5) · from AIM v2 · 5/5` — a user-edited list (via
  the `e` form or `ccc subgoals`) reads `manual` instead.
- **Impartial drift checker** — on every checklist change a **separate** cheap `claude -p`
  (`drift_model`, **never the session agent**) judges whether the new sub-goals still
  faithfully decompose the AIM, anchored to **both the original and current AIM** (to catch
  slow cumulative drift). It is fed only the AIMs and the before/after sub-goals — never the
  agent's own justification — scores a published rubric (`drift.DRIFT_RUBRIC`: coverage,
  goalpost integrity, scope, progress integrity, change justification), and **escalates on
  suspicion** (one pass when clean; two more confirm a flag, majority of 3). A confirmed
  drift shows a **blue `●`** in `ccc ls`, the TUI and the status line, and nudges the session
  to self-correct until it's resolved by a later clean check or `ccc ack-drift`.
- **Sub-goal history** — every checklist version is recorded (mirrors AIM history) with its
  trigger, the AIM revision it tracked, and the drift verdict: `ccc subgoal-history`, the
  `/subgoal-history` slash command, or the TUI **`sh`** chord (type `s` then `h`; a bare `s`
  still opens Settings).
- **Why impartial?** Goal drift in long-running agents is real and self-reinforcing, and a
  judge that sees the planner's rationale tends to be talked out of flagging it — so the
  checker is a fresh, context-free process scoring a fixed rubric. (Refs:
  [arXiv 2505.02709](https://arxiv.org/abs/2505.02709),
  [2606.04923](https://arxiv.org/abs/2606.04923),
  [2605.02964](https://arxiv.org/abs/2605.02964).)

### AIM self-assessment — the red `DONE` in the bar

Separate from the sub-goal bar, the center asks one holistic question at the end of every turn:
**has this session fully achieved its AIM?** — a plain True/False, judged directly against the
AIM (not the checklist decomposition). A `True` shows as a red **`DONE`** stamped *inside* the
progress bar (fill still visible on both sides, the `%` unchanged) across the TUI table, `ccc ls`,
`ccc aim --format bar` and the status line. The bar *continues behind the word*: the DONE
bar renders its filled cells as **solid `█`** (not the ordinary bar's dotted `▓` — no solid
letter background can ever match a glyph texture exactly), and each filled-cell letter's
background is the **very same colour** the `█` glyphs are drawn in, so letter cells and bar
cells are pixel-identical; letters over the empty `░` track get a faint 25 % tint, its
average colour (a 50 % bar reads `DO` on the fill, `NE` on the empty tint) — the word never
punches a seam into the bar. It is a *soft* signal — display-only, and deliberately
distinct from the human-authoritative `ccc done` (the green ✓ + FINISHED bucket): the model saying
"this looks finished" never marks the session done.

- **Impartial & out-of-band** — the Stop hook spawns a detached `ccc assess-aim` (never blocks the
  turn; the daemon runs a capped fallback for any missed spawn). That runs a **separate** cheap
  `claude -p` (`assess_aim_model` → `llm_model`, Haiku, **never the session agent**), the same
  pattern as the drift / score-aim checkers.
- **Grounded in evidence, not self-report** — it is fed the AIM (original + current) and a tail of
  the transcript that **includes truncated tool-result outputs** (command output, test runs, file
  edits), so a `DONE` rests on what actually happened. The published rubric (`aimmet.AIM_MET_RUBRIC`)
  is conservative — partial or ambiguous evidence ⇒ `False` — and **escalates on a True** (one pass;
  two more confirm, majority of 3), because a false "done" is the costly error.
- **Cheap & bounded** — one Haiku call per turn, gated to sessions with a **concrete** AIM
  (score ≥ threshold; drafts / done / archived skipped) and only when a **new turn** has happened
  since the last assessment, so idle/parked sessions cost nothing. A new AIM clears the prior verdict.
- **Reason on hover** — the TUI detail pane shows the one-line "why" (`model self-assessment: DONE
  — <reason>`). It is **off by default** (fresh-install inert); enable with `assess_aim_on_turn = true`.
- **Internal command** — `ccc assess-aim --session <id>` (like `score-aim` / `check-drift`); not a
  TUI key.

### Cross-session file locks (no two sessions editing one file)

When several Claude Code sessions share **one working checkout**, two of them editing the same
file interleave their changes into one ambiguous blob (lost work + commit-attribution bleed). An
advisory, per-file lock serializes *writes* so each session's edits stay an atomic, separately
committed unit.

- **Acquire on edit** — a `PreToolUse` hook (`Edit|Write|MultiEdit|NotebookEdit`) takes the lock
  on the target file for the session. Free / already-yours / reclaimable → the edit proceeds.
- **Validity = liveness + TTL** — a lock counts only while its holder is a **live** session
  (`ccc` already discovers these) and is fresh (`file_lock_ttl_sec`, default 1800). A dead-holder
  or stale row is reclaimed automatically — no manual cleanup, no deadlock from a crashed session.
- **Contention → deny + queue** — if the file is held by a live peer, the second session is
  registered as a waiter and its edit is **denied** with a message ("locked by …; edit another
  file or retry shortly"). `file_lock_wait_sec` (default 0) optionally polls-then-denies instead.
- **Eager hand-off, agent-judged** — when a peer is queued, the holder's `PostToolUse` hook nudges
  it: *"session X is waiting on F — run `ccc handoff F` when done."* The agent (the only party that
  knows it is *done with F this turn*) runs it; `ccc handoff` **commits (path-scoped) → pushes →
  releases**, so the waiter never starts on uncommitted work. Forcing release mid-edit is avoided
  on purpose — it would break the turn's edit atomicity.
- **Stop floor** — at end of turn the auto-commit commits+pushes the session's files, then a final
  Stop hook (`release-locks`) drops all its locks. A parked / idle session therefore holds none.
- **Fail-open** — any error in the lock path (store down, adapter error) lets the edit through; a
  *deny* only ever happens when a peer is provably live and the lock fresh. Kill-switch:
  `file_lock_enabled = false`.

```commands
ccc locks                      # list active locks (live holder + non-stale)
ccc handoff path/to/file.py    # commit+push that file, then release it (the normal hand-off)
ccc lock-release [file|--all]  # force-release without committing (escape hatch)
```

### Idle daemon (auto-close)

```commands
ccc daemon --dry-run -v            # preview: what would be reaped/summarized/alerted
ccc daemon                         # one pass: reap idle, done-check, summaries, alerts
ccc daemon --install               # load the launchd agent (runs every 5 min)
ccc daemon --uninstall             # remove it
```

Reaping is **off by default** (`reap = false` — fresh-install inert; enable it to
auto-close). When on it is conservative: only `interactive`, only `idle` past
`idle_timeout_min` (default 60), never `keep`/`done`, never while a child process (tool) runs.
Each pass also prunes leftover rows three ways — *contentless* rows (zero signal of
their own: no aim/prompts/summary/next/sub-goals), *headless one-shots* detected by
their transcript (e.g. `ai.py`'s commit-message generation, which carries an inherited
aim), and *dead-launched* jobs (a `start-job` that never had a turn: parked,
`prompt_count == 0`, no transcript, despite an inherited aim) — never a row that is
live, done, or kept. Transcripts persist — resume any reaped session by id.
Tunables live in `~/.claude/command-center/config.toml`.

On **Linux**, `ccc daemon --install` writes a **systemd `--user`** service + timer under
`~/.config/systemd/user/` instead of a launchd agent (the timer fires `ccc daemon` every
`daemon_interval_sec`); `--status` reports the timer, `--uninstall` removes the units, and
a `<label>-future-sync.path` unit replaces launchd's `WatchPaths` when a vault feature is
on. `ccc doctor`'s Daemon section is platform-aware. See
[linux.md](linux.md) for the full Ubuntu daemon walkthrough.

### Auto-resume rate-limit-halted sessions

When a Claude account hits its session/rate limit, its tracked sessions stall
(`||` **halted** — the last turn was a `You've hit your … limit` error).
With `resume_halted` on (**off by default** — fresh-install inert; enable it to use this),
the daemon spawns a singleton watcher (`ccc resume-halted --watch`) that resumes them
**automatically once the limit resets**, via `claude-session-continue.py`:

- **Reset is detected explicitly, per account** — each account with queued work gets
  its own headless `claude-session-continue.py --wait-only` detector, spawned with that
  account's `CLAUDE_CONFIG_DIR` pinned, so it probes the rate-limit window of the very
  seat it gates. Resumes only fire after **that** account's limit is confirmed clear.
- **Each session is revived on the account it was started from** — a `work` session
  comes back on `work`, a `private` one on `private` (the stored `config_dir` is
  prefixed onto the resume command). A `work` halt never gates a `private` resume,
  and vice versa: the two windows are independent.
- **Staggered across repos** — at most one resume every `resume_stagger_sec`
  (default 120 s), so a backlog doesn't thundering-herd the moment the window opens.
- **Serial within a repo** — one resume in flight per git repo; the next in that
  repo starts only after the prior session's turn completes (a finished transcript
  turn, then idle), so two sessions never edit one checkout at once.
- **A fresh halt invalidates pre-halt reset evidence** — the queue file outlives
  drained cycles, so a reset confirmation (or leftover signal file) from an earlier
  limit window would otherwise release a NEW halt immediately, dispatching a
  premature resume into the still-active limit. A newly-halted session instead
  re-arms its account's gate and waits for a fresh detector to confirm the reset.

A still-open halted REPL is SIGTERM'd (at its freshly-resolved pid) and relaunched
in a new tab. A resume that re-hits the limit is **never terminal**:

- a **productive** re-halt (the resumed session produced real model output before
  the next window's limit) is a fresh halt — full requeue, attempts reset;
- a **barren** re-halt (only the injected prompt + an error turn landed — e.g. a
  weekly/Opus cap the haiku probe cannot see) backs off until the reset time the
  halting error itself names, else an escalating fallback (15 min · 2^attempts,
  capped at 5 h), then retries. The watcher exits during a long backoff; the daemon
  respawns it when the retry is due.

`state="failed"` (bounded by `resume_max_attempts`, default 3) is reserved for
launch-infrastructure faults — a resume that never took at all. A failed entry whose
session finishes is pruned automatically; legacy rate-limit `failed` entries from
older versions are revived into the backoff machinery on the next tick. Inspect
without acting: `ccc resume-halted --dry-run` (guaranteed to touch neither the queue
file nor `resume.log`). Disable with `resume_halted = false`.

Every dispatched restart and queue/gate transition is appended to
`<state dir>/resume.log` (TSV: timestamp, event, session id, detail — e.g.
`launch <id> cwd=… account=… ok=True`), so "which sessions did ccc restart, and
when?" is a `grep launch` away even though the detached watcher's stdout is
discarded.

The one thing auto-resume still **refuses** is a session whose account it cannot
identify (no stamped `config_dir` while several accounts are configured): reviving it
would probe and bill an arbitrary seat, so it is skipped rather than guessed. Such a
row shows a bare `||` (no `▶`) — see the status-icon table below.

### List order

Sessions with an **AIM defined** sort first, then sessions without one; the
**done** section sinks to the bottom. Status (working / waiting / parked …) is
read from the first-column icon, not from group separators. Within each of the
AIM / no-AIM blocks, rows are grouped by repo category (`folder_order`, default
`home, infra, llms, sdsc`) and then ordered by most progress first (a session
with no sub-goal checklist sorts last). In the TUI each category is shown once as
a full-width blue divider — `──────── home ────────` — whose name starts exactly
at the **`folder`** column (the `head:` `folder` heading is aligned to that same
column), with its repos nested (indented) beneath it; the flat done block
keeps the full `category/repo` label. Reorder the categories via `folder_order` in
`config.toml`. Any session **outside** the `repo_root` tree (running in `~`, `/tmp`,
or anywhere else) is gathered under a single `others` header, where each row shows
its full home-relative path (`~/scratch`, `/tmp/x`) instead of just a repo name.

### Status icons

The first-column glyph on every row encodes the session's state. The same legend
is shown in the TUI help (`h`) — both are generated from `models.STATUS_ICON` /
`STATUS_HELP`, so this table can never drift from the code (an import-time assert
forces every `Status` to carry an icon and a help line):

| Icon | Status          | Meaning                                  |
|:----:|-----------------|------------------------------------------|
| `▶`  | `working`       | live — the agent is busy right now       |
| `⏸`  | `waiting_input` | live — paused, waiting for your input     |
| `\|\|▶` | `halted`     | live — rate-limit halt; **auto-resumes** when that account's limit resets |
| `\|\|` | `halted`        | live — rate-limit halt; **nothing will revive it** (resume it yourself with `r`) |
| `😴` | `waiting_codex` | live — idle, waiting for Codex quota reset |
| `❯`  | `idle`          | live — idle, waiting for your input (amber) |
| `💤` | `snoozed`       | live — idle, waiting on a background task |
| `☾`  | `parked`        | closed — process gone; resume with `r`    |
| `✓`  | `done`          | AIM marked achieved (done)                |
| `✗`  | `failed`        | ended in failure                          |

**The `||▶` two-tone halt icon.** A rate-limit halt is painted **red `||`**. When ccc will
bring that session back by itself — once **its own** account's limit resets — a **green `▶`**
is appended, so the red/green pair answers "do I need to do anything?" at a glance:

- **`||▶`** — leave it. The reset will revive it on the account it was started from.
  Sessions waiting out a barren-re-halt backoff keep the `▶` — they WILL retry.
- **`||`** — **stranded**: nothing will resume it but you (`r`). Shown for ineligible
  sessions (unattributable account, no transcript, feature off) and for a session
  whose auto-resume ended in a terminal launch-infrastructure failure
  (`resume_max_attempts` exhausted with zero progress).

The `▶` is per-session, not decoration: it appears only when the resume watcher would
actually act — `resume_halted` is on, the session's Claude account is identifiable, and it
has a transcript on disk (`claude --resume` needs a recorded conversation). Done, draft and
archived sessions never get one. See
[Auto-resume rate-limit-halted sessions](#auto-resume-rate-limit-halted-sessions).

A session marked **done** while the agent is still mid-turn keeps showing `▶`
(working) in the head column until that turn ends — the `✓` takes over the moment
it stops being busy. Derived live each refresh, never stored as a sticky flag.

A **waiting_codex** (`😴`) session is a Codex-workflow session that is otherwise
idle, while the OpenAI Codex 5-hour or weekly usage window is at 100% and its reset
time is still in the future. It is derived live from `read_codex_usage` each
refresh, never stored as a sticky flag, and clears as soon as usage is healthy again
or the session is no longer idle. When the reset is known, the row/detail hint says
which Codex window is blocking and how long until reset.

A **snoozed** (`💤`) session is idle *and* has a background task it spawned still
running (e.g. a `run_in_background` shell that will re-invoke the agent when it
finishes). It is derived live from the process tree each refresh — purely
deterministic, with no stored flag — so the instant that task exits the row
reverts to plain `❯` idle. (The idle reaper leaves snoozed sessions alone.)

A **closed** session is exactly a **parked** (`☾`) one — there is no separate
"closed" status. Accordingly the footer's close hint renders as **`☾lose`**, the
moon standing in (in gold) for the `c` key that parks the highlighted session.
**Done wins over parked**: closing (`c`) a session already marked done keeps its
status `done` — it is never demoted to `parked` — so the row sinks to the bottom
FINISHED section instead of lingering in the active list. Reconcile self-heals
any done row a pre-fix close left stamped `parked`.

### Tab badges (telling same-folder sessions apart)

Several Claude Code sessions running in the **same folder** look identical in the
list. Each iTerm tab is therefore given a distinct **colored emoji badge** shown
in two places that always agree:

- in the TUI, **opening the `id` cell** — badge and 4-char id form one chip painted on the
  tab's colour (`ccc ls` keeps it inline before the repo name), and
- prepended to the **iTerm tab title**, so a row maps to its tab at a glance.

A **live** row shows the badge its iTerm tab actually claimed (keyed by
`$ITERM_SESSION_ID`). Every other row — parked, finished, a future job, or a session in a
plain terminal with no iTerm cache — falls back to the **deterministic per-repo symbol**, so
a row (and a screenshot) always carries a glyph. The live cache is deliberately not trusted
there: that process is gone, so no open tab wears the emoji, and its `$ITERM_SESSION_ID` may
since have been recycled by an unrelated shell.

The chip's **colour is deduped the same way**: when two **open** tabs resolve to the same
background (the usual cause: both sit in the same repo, so both fall back to the repo colour),
every one but the oldest is assigned an unused colour from a distinct-hue palette. The tab
itself and the Claude Code status line follow: the assignment is written to the per-tab
`iterm-tab-rgb` cache plus a `<slug>.manual` marker, and with that marker present the
status-line wrapper takes the cached colour as authoritative and repaints the real tab to it
on its next render — no terminal API involved. The recoloured tab is also repainted
immediately: dedupe resolves the session's pane tty from its pid (`ps -o tty=`) and writes
the colour escapes straight to that device, so even an idle session — whose status line may
not re-render for a long time — wears the new colour at once. It runs on every TUI refresh,
writes only on a real collision, and is idempotent (a recoloured tab already resolves
distinct next pass).

Badges come from a palette spanning **six shapes** (circle / square / diamond /
triangle / heart / star) across well-separated colors. Assignment is **greedy and
derived from every open badge**, by a lexicographic preference (least-used wins):
a new tab gets the free badge whose shape — then color — is least used by *other
tabs in the same folder* first (so same-folder sessions, the reason badges exist,
are pushed apart hardest — e.g. 7 tabs in one folder → 7 distinct colors and all 6
shapes), and then, among badges equally good for the folder, the shape — then
color — least used **globally across all open tabs** (so a new tab also prefers a
shape *and* color no other open tab is wearing, regardless of folder). The palette
order (`🔺 🟢 🟪 ⭐ 🔷 🤎 …`) breaks the final tie and is front-loaded for
distinctness — one red and one warm-yellow up front, look-alikes (extra
reds/yellows, dark glyphs) pushed to the tail.

The badge is keyed to the iTerm tab (`$ITERM_SESSION_ID`), not the Claude
session, so it is claimed at **folder-entry time** (before `claude` runs) and is
stable for the life of the tab. Assignment is owned by:

```commands
ccc tab-symbol            # claim (or reuse) this tab's badge, print it; idempotent
ccc tab-symbol --read     # print the existing badge only, never assign
ccc tab-symbol --sync     # ensure every tracked live tab has a badge AND its title shows it
```

`--sync` (and the daemon, every pass — see below) is **marker-preserving**: it
rewrites only the `<emoji> repo` *core* of a tab title, so a tab flagged
"waiting" by `set-iterm-wait-marker.sh` stays `🔴 <emoji> repo` rather than being
reset to `<emoji> repo`.

`ccc tab-symbol` owns the palette and guarantees uniqueness across live tabs
(recycling the oldest when the palette is exhausted). State is one tiny file per
tab under `~/.cache/iterm-tab-symbol/<ITERM_SESSION_ID>` — mirroring the sibling
`~/.cache/iterm-tab-rgb/` tab-color cache: the shell **writes**, the TUI
**reads**, no daemon/DB coordination. The zsh `chpwd` hook
(`_repo_tab_color_hook` in `.zshrc`) calls it once per tab and prepends the badge
to the title it already sets; with `ccc` absent the title is simply left
un-badged. Colored emoji are used (not ANSI-styled glyphs) so the exact same
character renders identically in the terminal table and the tab title.

The `chpwd` hook only fires on `cd`, which never happens while `claude` holds the
foreground — so a badge **assigned mid-session** (e.g. a tab opened before the
badge cache existed, or a resume that re-keyed the tab) would otherwise show in
the TUI row but never reach the tab title. The cache (read by the TUI row **and**
the status line) is the source of truth; the tab title is a *pushed* copy, so it
goes stale whenever a badge is reassigned (e.g. palette recycling) and nothing
re-pushes. Three paths close that gap by pushing the badge into a running tab's
title via AppleScript (`set name`, which beats the CLI's own OSC title): the
**`SessionStart` hook** seeds it the moment a session launches; the **daemon**
re-converges every live tab each pass (`sync_tab_titles`, default on); and the
**TUI itself** re-converges on every refresh (~5 s, gated so AppleScript fires
only when the badge↔tab mapping actually moves) — so tabs heal **while you watch
them in the TUI**, even with no daemon loaded. All are marker-preserving and
idempotent.

The **status-line wrapper** (`statusline-command.sh`) reads the same
`~/.cache/iterm-tab-symbol/<id>` cache to **prepend the badge as the first
character of status-line line 1**, and ends that line with a compact
AIM-progress bar from `ccc aim --session <id> --format bar` (filled `▓` green,
empty `░` dim, `NN%`; a dim `░░░░░░░░` when the AIM has no checklist yet, and a
`🎯 /aim` hint when none is set) — so the main status line now opens with *which
tab* and closes with *how far along*. The bar reads `store.progress` **live**, so
it is always as fresh as the store; the only latency is Claude Code's status-line
cadence. Claude Code runs the command event-driven (after each message, debounced
300 ms) and **goes silent while the session is idle**, so an out-of-band progress
change (the daemon/after-turn grader ticking a sub-goal) would otherwise not show
until the next activity. Set `"refreshInterval": <seconds>` on the `statusLine`
block in `settings.json` to also re-run on a timer while idle (we use `3`) — that
is the only supported way to refresh an idle status line; there is no external
trigger.

> **Prerequisite — stop the CLI clobbering the title.** Claude Code overwrites
> the tab title on startup, *after* the shell hook ran, which would wipe the
> emoji. Set `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` (e.g. in `~/.claude/settings.json`
> `env`) so the shell-set title sticks. Tabs already open before the hook landed
> are healed automatically — the daemon badges every running session's tab in
> place each pass — or run **`ccc tab-symbol --sync`** to do it instantly.
> (`codex` tabs aren't tracked by `ccc` and manage their own title, so they stay
> un-badged.)

### Usage panels — Claude + Codex + GitHub Copilot (in the TUI)

The TUI's detail pane (bottom half) shows your subscription usage in the top-right,
as **stacked, border-titled cards** so the providers are never confused —
`Claude (private)` (gold border, per-bar green/orange/red usage bars) on top, `Claude (work)`
(blue border — shown only when a second `work` account is configured, see
*Multi-account* below), one `Codex <account>` card per configured ChatGPT login
(green border, green bars — the second one only when `codex_home_private` is set), and
`<copilot_card_title> <copilot_model>` (violet border) below — the Copilot title
shows the default delegation model from the `copilot_model` config (e.g. `gpt-5.4`).
Each Codex card names its own account in the title (`t3:Codex first…@example.org`,
read from that home's `auth.json`), so two logins are never mistaken for one another.
The titles drop the vendor prefix and the word "usage", and the bars drop "used", to keep
the cards narrow — a title wider than the card's 34 columns is truncated by Textual, and
what it truncates away is the domain that tells two accounts apart.
Each window is a **single bar** — no standalone title line; the window name and the
reset time are **embossed inside the bar itself** (`Session:` / `Week:`, dark over the
used portion, the card's accent colour over the track) so the bar's fill still shows
usage behind it. The percentage sits **immediately after the bar**, flush against the
card's inner edge: the bar takes the whole row minus the percentage's own width, so
there is no gap between bar and number (a `100%` bar is therefore one cell shorter than
a `27%` one). The cards sit flush against each other:

```text
╭──── Claude (private) ───────────────╮
│ ██Session: Resets in 1h 57m░░░░░░33% │
│ ██Week: Resets in 3d 11h 36m░░░░░20% │
╰──────────────────────────────────────╯
╭──── Codex first…@example.org ───────╮
│ ██Session: Resets in 2h 11m░░░░░░14% │
│ ██Week: Resets in 6d 0h 5m░░░░░░░10% │
╰──────────────────────────────────────╯
╭──── Copilot gpt-5.4 ────────────────╮
│ ░Resets in 4d░░░░░░░░░░░░░░░░░░░░░0% │
╰──────────────────────────────────────╯
```

**Expand/collapse each card with the persistent `t1`…`t5` / `to` / `ta` chords** (`t1` = Claude
private, `t2` = Claude work, `t3` = Codex, `t4` = Copilot, `t5` = the second Codex login,
`to` = nixos overseer supervised,
`ta` = nixos overseer tier_a; type `t` alone for the menu). **Collapsed is not hidden**: the card
keeps its titled top border — the line that names its own chord — and drops the rest of the box,
so a folded-away card stays one keystroke from coming back:

```
╭─ t1:Claude (private) 🏠 ───────────────╮
╭─ t3:Codex first…@example.org ─────────╮
╭─ t5:Codex second…@example.com ────────╮
╭─ t4:Copilot gpt-5.4 ──────────────────╮
│ ░Resets in 4d░░░░░░░░░░░░░░░░░░░░░░0% │
╰───────────────────────────────────────╯
```

A collapsed card also gives back its **width**: the cards share one auto-width column
pinned to the right edge, so the widest card decides how far left the column starts and
how little room is left for the job-details pane beside it. Collapsed, a card is sized to
its title alone rather than to its (now hidden) content — folding away the nixos
supervised card, ~79 cells wide with real incidents, hands ~38 columns back to the left.

Unlike the view-local `td`/`tf` toggles these **persist** to `config.toml`
(`usage_card_private/_work/_codex/_codex_private/_copilot`,
`card_nixos_overseer_supervised/_tier_a` — pure render gates); `t4` also flips the
`copilot_usage` network-fetch gate so a collapsed Copilot card costs no `gh` call, and
`t2` on a machine with no `work` account explains itself instead of toggling an empty
box (that card is absent entirely, title line included — as is the `t5` card without a
`codex_home_private`; those two are the only cards that ever disappear outright).

**The two nixos-overseer cards** read incidents from an *external* homelab
"overseer" alert-triage daemon (a separate project — nothing to do with ccc's own
future jobs) and are OFF until you point `nixos_overseer_dir` at that daemon's
directory (its SQLite DB is read **read-only** at `<dir>/state/overseer.sqlite`,
one cheap query per render tick, never blocking — every failure collapses to a
one-line placeholder). Both card titles carry a live count — e.g. `nixos
overseer supervised (6)` — of every incident in the category (the tier_a count
includes rows folded into the `… +N more` tail); a broken/unset source shows no
count. The **supervised** card (`to`, orange, shown by default)
lists incidents awaiting a human decision (`<id> <status> <fingerprint> <age>`,
newest first) with an `approve: …` hint, and prepends a red `⛔ dispatch disabled`
line when the daemon is halted; zero rows is the good `— none —` state. The
**tier_a** card (`ta`, teal, hidden by default) lists recent *automatic* activity
over the last 7 days (capped at 10 with a `… +N more` tail).

The Claude/Codex cards show a 5-hour (`Session:`) and a weekly (`Week:`) window with
**relative** reset times (`Session: Resets in 1h 57m`), recomputed on every re-read
(`usage_refresh_sec`, default 5 s — this drives the whole TUI's refresh timer). The
Copilot card is a single bar too: once the seat is on usage-based **AI Credits**
billing (the case since 2026-06) the bar is credits used ÷ the seat's credit
entitlement, embossing the reset **and** both figures (`Resets in 3d · 1505/1500cr`);
otherwise it falls back to premium requests used ÷ `copilot_quota` (300), resetting on
the 1st of the month. Both AI-Credit numbers come from the seat's own live meter,
`gh api /copilot_internal/user` → `quota_snapshots.premium_interactions`
(`entitlement` + `credits_used`) — the entitlement differs per plan (1,500 on an
individual/faculty seat) and the billing endpoint's month-to-date quantity lags by up
to a day, so anything else is a guess that silently mis-scales the bar. When that
endpoint does not answer, the bar degrades to the billing quantity ÷ the configured
`copilot_credit_quota` (default `1900`, the documented Copilot Business per-user
baseline) and `ccc copilot-usage` marks the denominator with a `?`.

**Claude Code** exposes this data only in its **status-line JSON**
(`rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}`) — the numbers
ride on every API response's `anthropic-ratelimit-unified-{5h,7d}-*` headers,
there is no standalone endpoint. The status-line wrapper therefore pipes its JSON
to `ccc statusline --session <id> --capture-usage`, which persists the
(account-global) snapshot to `~/.claude/command-center/usage.json`; the TUI reads
it. The account *totals* are global, but each session's status-line block only
reflects **that session's last API response**, so an idle session reports a stale
view (percentages and `resets_at` from days ago). Since every concurrent session
writes the one shared file, `write_usage` **merges** per window — a reset already
in the past is dropped as stale, the later (freshest) `resets_at` wins, and at an
equal reset (idle sessions share the fixed weekly boundary) the higher cumulative
`used_percentage` wins. So a parked session can neither make the card read "Resets
now" nor flip-flop the percentage (e.g. 8% ↔ 28%). The panel therefore works even
when every session is parked, and over `ccc serve` in the browser:

> **Closing the gap vs `claude`'s own `/usage` — the OAuth fetch (opt-in).** The
> capture path above only *replays* what sessions report, so with every session idle
> the card can lag the CLI's live `/usage`. Setting `claude_usage = true` closes that
> gap: `ccc claude-usage [-a LABEL]` reads each account's OAuth token from the macOS
> Keychain (`Claude Code-credentials`; the token is never logged, an expired one is
> skipped, and on Linux/no-keychain it degrades to a silent no-op) and fetches
> `https://api.anthropic.com/api/oauth/usage` — the same endpoint the CLI queries.
> The fetch **authoritatively replaces** the snapshot (self-healing a window boundary
> Anthropic rebased, which merge rules would pin forever) and also picks up any
> **weekly model-scoped window** (rendered as a third bar in the Claude card — the
> status line never carries it). A re-pin guard then stops idle sessions' status-line
> replays from overwriting the fresh figures for an hour, while an actively working
> session's same-window rise still merges (the ~3 s fast path survives). The fetch is
> throttled like Copilot's (`claude_usage_refresh_sec` 600 s idle /
> `claude_usage_refresh_active_sec` 200 s while any job works) and runs out-of-band —
> the daemon per configured account, plus a detached TUI spawn when the snapshot is
> stale. Off by default (fresh-install inert: a keychain read + network call).
>
> **429 backoff & stale marking.** The endpoint has started answering HTTP 429 with a
> large `Retry-After` (observed 3357 s). Rather than keep hammering it every few minutes,
> such a 429 persists `oauth_backoff_until` (= now + the server-given wait, capped at 2 h)
> into the account's usage cache; while that time is in the future `claude_usage_stale`
> reports the cache fresh, so neither the daemon nor the TUI re-attempts the fetch until it
> passes (a successful fetch clears the field). Because a backed-off fetch means the number
> can freeze, the Claude card marks it: when the last *successful* OAuth fetch is more than
> 1 h old the Fable row is embossed `Fable: stale <age>` (e.g. `Fable: stale 6h 25m`)
> instead of `Fable: Resets …`, so a stale figure is never shown as if it were live.
>
> **Session/Week staleness (bare `?%`, no bar).** The same "never show a frozen number
> as if it were live" rule applies to the two main bars, not just Fable: once
> `now - captured_at` exceeds the window's own lifetime — 5 h for `Session:`, 7 d for
> `Week:` — that row drops its coloured bar for a bare `Session: ?%` / `Week: ?%`
> instead, since no session has talked to the API recently enough for the figure to
> mean anything. Each row is gated independently off the one shared `captured_at`
> clock. Codex and Copilot are unaffected (their own sources are already "as fresh as
> the last event").

```commands
echo "$input" | ccc statusline --session "$sid" --capture-usage
```

**OpenAI Codex** has two sources, and `read_codex_usage` serves whichever is **newer**.

*The live endpoint (opt-in, `codex_usage = true`).* ChatGPT's own
`https://chatgpt.com/backend-api/wham/usage` returns exactly what its *Settings → Usage*
page shows. `ccc codex-usage` GETs it with the credentials `codex login` parked in
`$CODEX_HOME/auth.json` (`Authorization: Bearer <tokens.access_token>` +
`ChatGPT-Account-Id: <tokens.account_id>`; the token is never logged or printed, and an
API-key login — `auth_mode` other than `chatgpt` — is skipped, since it has no
subscription windows). The two windows are identified by their `limit_window_seconds`
(18000 → `Session:`, 604800 → `Week:`), never by their primary/secondary position, and a
`rate_limit_reached_type` marks the card blocked from the same code the rollout path
reads. The card draws **no banner** when the live snapshot already pins the exhausted
window: that row reads `Session: Resets in 19m … 100%`, which is both the refusal and
when it lifts, so `⛔ usage limit reached` / `access returns in 19m` / `live figures, 0m
old` were three lines repeating the bar under them. Where the bars do *not* carry the
block — rollout-sourced figures (the last SUCCESSFUL call's, so they read as headroom),
or no live exhausted window to pin — the banner stays, in **card-sized wording**
(`⛔ usage limit reached`);
`ccc codex-usage` and `codex-in-claude headroom` keep the long explanatory form
(`BLOCKED — included usage limit reached (no credit overflow)`), which at 61 characters
would stretch the 38-column card past 60 and steal that width from the job-details pane
beside it. A refusal code with no short form mapped falls through to its long label, so
the card never drops the reason. The access token is a JWT valid ~10 days that `codex` refreshes as it runs;
if it has expired anyway the endpoint answers **401**, and the fetch falls back once to
the official `codex app-server` (JSON-RPC `account/rateLimits/read` over stdio), which
refreshes the token itself and writes it back. The fetch is throttled exactly like the
Claude one (`codex_usage_refresh_sec` 600 s idle / `codex_usage_refresh_active_sec`
200 s while any job works) and runs out-of-band — the daemon per configured home, plus a
detached TUI spawn when a cache is stale; the render path only reads the cached JSON.
Off by default (fresh-install inert: an `auth.json` read + a network call).

```commands
ccc codex-usage              # refresh + print one line per configured CODEX_HOME
ccc codex-usage --json       # dump the cached snapshots (a dict keyed by home label)
ccc codex-usage -a private   # just the second login
```

*The rollout files (always available, no wiring, no network).* Codex writes a
`rate_limits` block (`primary` = 5-hour, `secondary` = weekly) onto each
`token_count` event in its session rollout files
(`$CODEX_HOME/sessions/**/rollout-*.jsonl`, default `~/.codex`) — account-global, like
Claude's. Codex emits more than one block shape, though: the `limit_id: "codex"` block
carries the windows, while short `codex exec` runs (the ones ccc spawns for
short-aim/delegate) log a **windowless** `limit_id: "premium"` block whose
`primary`/`secondary` are both `null`. The reader skips windowless blocks and scans back
through enough files to find the freshest one that actually has data — otherwise the
pile-up of tiny exec runs would bury the real numbers and the card would read "(run Codex
to populate)". This source is only as fresh as your most recent Codex turn, which is the
gap the endpoint closes: on 2026-08-30 the newest windowed event was ~14 h old and its
5-hour reset had long passed, so the only *live* window left for a refusal to pin was the
weekly one — the card claimed `Week: 100%, access returns in 6d 8h` while the web page
said the 5-hour window had just filled (19 min to go) and the week still had 77% headroom.
With live data the block is attributed to the window that actually filled — and the card
then needs no banner at all, where the rollout path still prefixes its bars with
`⛔ …` + `100% = the limit that fired; other figures are <age> old`.

*A second ChatGPT login.* Point `codex_home_private` at another `CODEX_HOME` (create it
with `CODEX_HOME=~/.codex-private codex login`) and a second green card appears, with its
own `t5` chord, its own throttled fetch and its own cache. Empty — the default — means no
second card at all (absent, not merely collapsed). Each card titles itself with the
account e-mail read from that home's `auth.json` `id_token` (abbreviated:
`t3:Codex first…@example.org`, and squeezed further — `fi.la@example.org` — when the
title would otherwise outgrow its card), so the two are never confused. The rollout files
belong to the default login, so the second card is live-endpoint-only, and a refusal
recorded by one login never bleeds into the other's card.

**GitHub Copilot** is read from the official `gh` CLI hitting your own per-user
enhanced-billing usage endpoint (`/users/{login}/settings/billing/usage`) — no
proxy, your real GitHub credentials. `fetch_copilot_usage` sums the current month's
`copilot` line-items (premium requests historically, **AI Credits** since 2026-06)
into a single month-to-date figure with its list cost; `net == 0` renders as
`· covered` (absorbed by the subscription). The `gh` call is **throttled** and run
out-of-band — the daemon refreshes it (`copilot_usage_refresh_sec`, default 900 s),
the TUI also fires a detached `ccc copilot-usage` when the cache is stale, and the
render path only reads the cached `copilot_usage.json`. The throttle is the **only**
adaptive one (it is the only card whose data costs a network call): while **any**
tracked session is actively working (`working`/`snoozed`) it tightens to
`copilot_usage_refresh_active_sec` (default 300 s, ~1/3 of idle; `0` disables the
speed-up) so the card tracks reality more closely during active work — applied both
in the daemon and the TUI-spawned refresh, never to the cheap Claude/Codex cache
reads. Refresh by hand or inspect the raw snapshot with:

```commands
ccc copilot-usage          # refresh + print this month's figure
ccc copilot-usage --json   # dump the cached snapshot
```

The card is **off by default** (fresh-install inert — the `gh` call is a network hit);
enable it with `copilot_usage = true`. (Independent of the `/copilot` slash command, which
*delegates* prompts to a Copilot-served model via OpenCode — the card just reports what that
seat has spent this month.)

### Multi-account Claude Code (private + work)

One ccc can watch sessions from **several Claude Code accounts** (e.g. a private and a
work subscription, each with its own config dir and its own rate-limit windows) without
ever billing the wrong seat. Configure the accounts as `"label=path"` entries — the
**first entry is the default account**:

```toml
claude_accounts = ["private=~/.claude", "work=~/.claude-work"]
```

Empty (the default) means today's single account, so single-account installs are
completely unaffected. Labels must match `^[a-z0-9][a-z0-9_-]*$` (they feed cache
filenames); malformed entries are skipped.

- **The billing pin (`accounts.py`) — never hand-roll the env.** Claude Code hashes
  `CLAUDE_CONFIG_DIR` into its Keychain service name **whenever the var is set**, so
  the default account must run with it **UNSET** (exporting its own default path
  authenticates nothing) and any other account with it **SET** to that account's dir;
  `CLAUDE_SECURESTORAGE_CONFIG_DIR` is always stripped (it outranks the config dir in
  that hash). Three renderings of the one rule: `launch_env` (a `Popen(env=)` dict),
  `apply_to_environ` (in place, before an `os.execvp`), `launch_env_prefix` (a shell
  snippet for the iTerm/tmux launchers, which take a command *string*). Exports use
  the account's **configured spelling** — a resolved symlink would hash differently
  and read as "not authenticated".
- **Attribution.** `sessions.config_dir` records the account a session **last ran
  under**: the adapter's `discover()` scans every account's live registry and stamps
  each live session; `core.reconcile` persists it on change; the in-session hooks
  stamp it from the session's own env. A store migration backfills pre-existing rows
  to the default account, so afterwards an empty `config_dir` means **unknown — and
  fails closed**.
- **Fail-closed launches.** `ccc resume`, `ccc resume-job`, `ccc start-job` and the
  `jump` chord all refuse (stderr + exit 1) when several accounts are configured and
  the session's account is unknown, or when the id is **live under two accounts at
  once** (a conflict needs two *live* processes — a stale registry file left by a
  crash never blocks resume). Launching pins the session's own account into the child
  env, so an ambient `CLAUDE_CONFIG_DIR` in your tab can never flip the seat.
  Resuming a session under a *different* account than it last ran under gets a
  SessionStart warning inside the session — the only guard that reaches the native
  `claude --resume` picker.
- **Per-account usage.** Each account keeps its **own snapshot**
  (`usage.json` for the default, `usage-<label>-<hash8>.json` otherwise) and its own
  card; a statusline write is routed by the session's `CLAUDE_CONFIG_DIR` and skipped
  entirely for an unknown account, so two accounts' windows can never merge into one
  bar.
- **Identity hard-link (`claude_account_emails`) — surviving a drifted login.** WHICH
  config dir a label points at is a path; WHICH Claude account is actually logged
  into that dir can drift (a bare `/login` in a shell with the wrong, or unset,
  `CLAUDE_CONFIG_DIR` silently overwrites it — the dir named `work` can end up
  holding the private account, or vice-versa, with no visible cause). Configure an
  expected email per label, same `"label=email"` shape as `claude_accounts`:
  ```toml
  claude_account_emails = ["work=you@company.com", "private=you@personal.example"]
  ```
  Before rendering, `accounts.resolve_card_label(label)` reads every configured
  account's **current** identity — `oauthAccount.emailAddress` from Claude Code's own
  `.claude.json` (the DEFAULT account's lives at `$HOME/.claude.json`, a sibling of
  `~/.claude/`, not inside it; any other account's lives at `<config_dir>/.claude.json`
  — the same field `/status` prints as `Email:`) — and swaps in whichever account's
  cache actually matches, so e.g. the "work" card always shows the SDSC/company
  account's numbers regardless of which physical dir currently holds them. A label
  with no hard link configured passes through unchanged (today's pure path-based
  behaviour, zero extra reads); one WITH a hard link but no current match renders
  **empty** rather than falling back to a path-based guess that could be wrong again.
  Empty (the default) ⇒ no hard link for any label — single-account installs and
  anyone who hasn't hit this drift are completely unaffected.
- **Subscription end dates (`subscription_ends`) — the `-> D.M` a card can carry.** A
  paid plan you mean to cancel is only cancellable if you remember it renews. Pin the
  date to the card that bills it and its border title says so:
  ```toml
  subscription_ends = ["claude_private=auto", "codex_private=2026-09-30"]
  ```
  ```
  ╭─ t1:Claude (private) 🏠 -> 18.9 ──────╮
  ╭─ t5:Codex se.lo@example.com -> 30.9 ──╮
  ```
  Cards: `claude_private`, `claude_work`, `codex`, `codex_private`. The date renders
  Swiss `D.M` — four columns, no padding, no year — and gains a `!` (`30.8!`) once it is
  past, so a **pinned** date cannot quietly rot after its renewal. Empty (the default) ⇒
  no card carries a date and no extra endpoint is ever called.

  `auto` derives the date instead of pinning it, and is only as good as its source:
  - **Claude cards** — Anthropic's OAuth `/profile` exposes `subscription_created_at`,
    `subscription_status` and `billing_type` but **no** `current_period_end`, so the date
    is the next **monthly billing anniversary** of that creation timestamp (clamped in
    short months, Stripe's own rule). It is the *earlier* of the monthly/annual readings
    on purpose: this date exists to be cancelled before. The profile is fetched at most
    once a day, riding along with `ccc claude-usage`, and only when an `auto` entry asks
    for it.
  - **Codex cards** — ChatGPT's `backend-api/subscriptions` answers 403 behind Cloudflare
    (only `wham/usage` is reachable with the Codex token), leaving the `auth.json`
    id_token's `chatgpt_subscription_active_until` claim. That claim is refreshed only by
    a `codex login`, so it goes stale within weeks — prefer a pinned date here.

  Either way an unresolvable `auto` renders **nothing** rather than a guess. Note the
  title budget: Textual clips a border title at the card width minus four (32 cells at
  the default 38-column card), and the date sits at the very end — so a title carrying
  one squeezes its **local part** (`al.gl@gmail.com`, two characters per dotted segment)
  and, if that still does not fit, the card widens by the cells needed. The domain is
  never squeezed and nothing is ever cut.
- **Per-job account.** A future job carries the account it will launch (bill) under:
  `ccc new-job -A <label>`, the TUI's new-job/`e`-form account selects, and the job
  file's `account` frontmatter + control — all shown **only when more than one
  account is configured**, so single-account setups see nothing new. `ccc jobs` tags
  a non-default account `[<label>]`.
- **Per-account glyph + the `tp`/`tw` quick switches.** In the **model** column (TUI and
  `ccc ls`), each row is prefixed with a little per-account glyph: **🏠** for the
  **`private` (cpriv)** account, **💼** for the **`work` (cwork)** account — so you can see
  at a glance which sessions are personal vs work; any other (unknown) account gets an
  equal-width blank so the model text stays aligned. The same 🏠 / 💼 also appears on the
  two Claude usage-card titles and in the **statusline** right after the model name
  (`Model: Opus 4.8 🏠 | xhigh | …`) — the statusline (`dotfiles/claude/.claude/
  statusline-command.sh`) calls `ccc statusline --print-glyph`, a standalone
  Store-free fast path that prints `accounts.current_account_glyph()`: the session's
  `CLAUDE_CONFIG_DIR`, corrected by the identity hard-link
  (`accounts.effective_account_label`) when `claude_account_emails` is configured, so
  the badge survives a drifted login rather than just trusting the path. Falls back
  to a plain path check when `ccc` is unavailable. The **row** markers are corrected the
  same way: each render/listing resolves every configured dir's current identity once
  (`accounts.effective_home_markers`, then a per-row `accounts.home_marker_from` lookup —
  one `.claude.json` read per account, never per session), so a drifted login shows the
  account each row TRULY bills instead of the one its config dir happens to be named
  after. Without a hard link configured (or with an unreadable identity) both surfaces
  degrade to exactly the old path-based marker. Keep the glyphs in sync with
  `accounts._HOME_GLYPH` / `_WORK_GLYPH` (public accessor: `accounts.card_glyph`).
  The glyph only shows in multi-account mode (with one account it would sit on every
  row and mean nothing). To change a row's account fast, without opening the `e` form: highlight it
  and press **`tp`** (type `t` then `p`) for **private** or **`tw`** for **work**. Both
  are per-session `t…` chords, listed in the `t` leader menu. They flip a **FUTURE job**
  (draft) freely (it never ran); on a **PARKED** session they re-stamp the account **only
  when that session's transcript already lives under the target account** (else they warn
  and leave it unchanged — resuming under an account with no transcript would find
  nothing); a **LIVE** session cannot be switched (it already bills the account its
  process runs under).
- **Routing a NEW job (`job_account`).** When a job is created **without** an explicit
  account (no `-A`, no account select), the `job_account` config key decides which
  account it bills to: `""` (default) ⇒ the default account (today's behaviour); a
  configured label ⇒ that account (a hard pin); `"auto"` ⇒ the account with the highest
  **required burn rate** `(100 − used%) ÷ hours-to-reset` over its Fable weekly window
  (falling back to the plain 7-day window). Routing to the max saturates the allowance
  that resets *soonest* first and self-balances as it fills (the leader's remaining%
  falls until the other overtakes); a snapshot older than 6h, or an account ≥ 90% used
  while another is usable, is skipped so a routed job never trusts stale data or dies on
  the hard cap. The stamp is evaluated once, at **creation** (visible/editable in the TUI
  and the job file's account select), never re-routed on edit. `ccc job-account` prints
  each account's used%, reset, urgency, and the account the policy currently resolves to;
  `ccc job-account -p/--pick` prints only the picked label — the machine-readable form a
  shell launcher can dispatch on per invocation (so a long-lived shell never goes stale).
- **Transcripts.** `transcript_path` searches the session's **owning account first**,
  then every other account, so a shared transcript tree is an optimisation — never a
  correctness precondition.
- **Auto-resume.** The rate-limit auto-resumer keeps **one reset gate per account** —
  a detector process and signal file each, spawned with that account's
  `CLAUDE_CONFIG_DIR` pinned — and revives each session **on the seat it was started
  from**. The accounts' limit windows are independent: `work` being rate-limited never
  holds back a `private` resume. Only a session whose account cannot be identified (no
  stamped `config_dir` in multi-account mode) is skipped and purged from the queue —
  ccc will not guess which seat to bill.
- **ccc's own state stays account-independent**: the DB/config root is `$CCC_HOME`
  (default: the `claude_home()` tree), so one store, daemon and TUI serve every
  account; only the usage snapshots are per-account.


## `ccc quota` — the quota oracle

Answers "which provider still has tokens, and until when?" from **cache only** (~0.07 s of
reading, ~0.3 s of process start), so any script or agent can consult it before spending a
provider attempt. This exists because the alternative is discovering a dead provider by
ATTEMPTING it: with the GitHub Copilot seat hard-429 for three days, every `ai.py push`
paid a doomed retry — 300 s of it, because the same-seat OpenCode fallback re-ran the
already-refused request.

```commands
ccc quota                       # human table: provider · state · data age · unblocks · windows
ccc quota -j                    # versioned JSON contract (for scripts), schema v2
ccc quota -p codex:private      # one provider; exit 0=available 1=blocked 2=unknown
ccc quota -b                    # best CLAUDE account label only
ccc quota -M claude-opus-4-6    # scope Claude windows to the model you will call
ccc quota -r                    # force a live re-fetch (the ONLY networked path)
ccc quota -m copilot -u 272848 -R "429 quota exceeded"   # record an authoritative block
ccc quota -m codex -H -U 2026-09-07T00:00 -R "team seat reserved"  # administrative HOLD
ccc quota -c copilot            # clear a block (also the only way to lift a hold)
ccc quota -c claude:private -O  # observed-only clear: lifts a rejection, never a hold
```

Both Codex seats appear as their own rows — `codex` (the canonical team seat,
`~/.codex`, env-independent) and `codex:private` (`codex_home_private`) — each with the
account e-mail from its `auth.json` as identity proof and its own rollout-refusal
attribution. The footer names the seat delegation bills right now
(`best_codex_account`: an ELIGIBLE pin wins, holds/blocks exclude a seat first,
team-first otherwise); `codex-in-claude`'s `_codex_home()`, `codex-review.py` (via
`codex-in-claude.py home -j`) and ccc's own `llm.run_codex` all follow that one
selector. The `data age` column shows how old each row's governing evidence is
(`marked <age>` for cooldown/hold rows — the age of the mark, not of quota data).

### Four states, and why the distinction matters

| state | meaning | effect on a caller's ladder |
| :--------- | :------------------------------------------------------ | :-------------------------- |
| `available` | headroom proven by fresh, authoritative data | use it |
| `blocked` | a window at 100 %, or an unexpired provider rejection | skip it |
| `unknown` | no data, stale data, or a *guessed* denominator | **still try it** |
| `disabled` | a capability fact with no reset (retired Gemini tier) | skip it, permanently |

`unknown` is deliberately not `blocked`. Refusing to try a provider because we failed to
*measure* it would silently delete a working rung — a worse failure than one wasted
attempt. Callers fail open.

### Windows are never collapsed

A provider can be at 100 % on its 5-hour window and 49 % on its weekly one. A single
`used_pct` would render that as healthy and send the caller straight into a rejection, so
each provider carries a `windows` map, a `blocked_by` naming the window that blocks, and a
`resets_at` taken from *that* window.

### Hard exhaustion ≠ routing risk

`routing._EXHAUSTED_PCT` (90 %) deprioritizes an account because a long job launched there
might die mid-run. Reusing it here would throw away a tenth of a paid subscription, so
this module reports `risky` separately; only `blocked` (≥ 100 %, or a rejection) removes a
rung.

### Model scoping

Claude accounts expose a Fable-model-scoped weekly window alongside the plain one. An
account at 100 % on `fable_week` is **not** out of tokens for an Opus request — pass
`-M <model>` (or `model=` to `quota.snapshot`) so only the governing windows are consulted.

### The cooldown store

`cooldowns.json` records authoritative rejections — HTTP status, quota marker, scope,
`observed_at`, and an absolute `blocked_until` taken from the provider's own `Retry-After`.
It is *retry suppression*, not a billing calendar. The whole read-merge-write runs under
one `flock` (atomic replacement alone prevents corruption but not lost updates), and
entries apply by `observed_at` so a slow process's stale 429 cannot overwrite a later
success.

Two entry kinds share the store. `observed` (the default) is a provider's own
rejection; `hold` (`-H`, deadline via `-U`, EXCLUSIVE — blocked while `now < STAMP`)
is an administrative reservation ("leave the team seat alone until the weekly reset").
An unexpired hold outranks every observed write regardless of timestamps, a success
never lifts it (`-O` exists precisely so success-path clears cannot), and only expiry
or an explicit `-c` removes it. `scope` tags why a block exists — `ai.py` records
Claude auth/billing failures with `scope=auth`, and its all-blocked safety valve
refuses to restore auth-scoped rungs (an unpaid seat rejects every attempt until a
human acts). Expiry is filtered on READ, so concurrent invocations racing an expired
entry can each probe once before the first failure re-records — accepted; they
converge via `observed_at` ordering.

## Layout

```text
command_center/
  config.py        paths + user tunables (~/.claude/command-center/config.toml)
  models.py        dataclasses, Status enum, pure formatters
  store.py         SQLite store (WAL) — single source of truth
  usage.py         account usage snapshots (Claude + Codex bars; Copilot month-to-date via gh)
  tabsymbol.py     per-iTerm-tab colored badge (claim/read), shared by shell + TUI
  links.py         OSC 8 clickable links (vendored from new-repo.py / list_repos.py)
  core.py          reconcile(live registry → store), build_rows()
  adapters/        claude.py = the ONLY reader of Claude Code internals
  views/commands.py  single source of truth for TUI keys/commands (bindings, footer, headers, help)
  views/tui.py     the interactive Textual command center
  views/ls.py      the flat clickable list
  cli.py           the `ccc` entry point
```
