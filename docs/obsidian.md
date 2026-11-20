# ccc + Obsidian: the capture pad

Future jobs live in ccc's store: you create and change them with `ccc new-job`,
`ccc edit-job` and the TUI `e` editor, and read them back with `ccc show`, `ccc ls`,
`ccc jobs` and the TUI. The one Obsidian surface left for them is the **capture pad**: a
single note, `new-prompt.md`, that queues (and optionally launches) a job from any device
that syncs your vault — your phone included. ccc writes no other note: the session
mirrors (`running/`, `done/`, `sessions/`) and their dashboards are gone; read a session
with `ccc show -s <id> [-t]` instead. Archive an old mirror tree with
`tools/retire_vault_files.py -k mirrors` (dry run by default, `-x` to remove).

This integration is entirely opt-in. With no vault configured, `ccc` never touches disk
outside `CLAUDE_HOME`.

## The capture pad

The pad's path is the `future_pad` config key (default
`~/obsidian/01-llm/ccc-tasks/new-prompt.md`). It is served when `future_pad_enabled` is
on (or the legacy `future_files` switch is); both default to `false`. While it is
served, the future-job sync (`ccc sync-future`, run by the daemon and the vault watcher)
reads the pad on every pass:

1. Write the AIM (and, optionally, a prompt, repo, job type, start date or deadline) into
   the pad's frontmatter and body.
2. Set `status: ready` — the job is registered in ccc's store under a fresh id and the
   pad is reset to blank. Or tick the bare-boolean `launch` property instead: a `draft` pad
   with `launch` on is treated as `ready` and registers **and** launches in one pass, no
   status edit needed.

An empty `repo` defaults to `$HOME`, so the minimal capture is literally an AIM plus the
launch tick. On mobile, Obsidian renders `launch: false` as a one-tap checkbox in the
properties UI (the mobile Properties `status` field is free text, and `ready` never
persists anywhere, so its suggestions cannot offer the one value that matters). The pad
is reset to `launch: false` *before* the launch spawns, so a failed spawn never
re-triggers.

A pad flipped to `status: error` by a failed validation keeps retrying while `launch`
stays ticked: fix the offending field and the next pass registers and launches it — no
status reset, and never delete the error block by hand (ccc owns it; it disappears on
successful registration). A deleted pad is recreated blank on the next pass.

Once registered, the job is an ordinary future job: there is no per-job file to open.
Check it with `ccc show -s <id>` or `ccc jobs`, change it with `ccc edit-job <id> …` or
the TUI.

**Where the job opens.** The launchd watcher cannot AppleScript iTerm (TCC silently
denies without prompting, and iTerm's Python-API cookies are single-use and mintable
only via AppleScript — verified 2026-07-17), so watcher-spawned jobs land in a window
of the persistent tmux session instead. They are still first-class: `ccc focus-job`,
`ccc resume-job` and the TUI's focus keys locate the tmux window by walking pane PIDs,
select it, and surface it in iTerm (activating an attached client, or opening a new
tab running `tmux attach`). Opt-in for direct iTerm tabs from the watcher: root-own
iTerm's auth-disable file — `sudo chown root:wheel "~/Library/Application
Support/iTerm2/disable-automation-auth"` (the file must exist first; iTerm honors it
only when root-owned) — which lets any local process use the iTerm API without
authentication; the spawn chain then upgrades automatically via its Python-API step.

## One-time setup

```commands
ccc obsidian-setup [-r VAULT] [--install-plugins] [-n] [-u]
```

`obsidian-setup` is **content-only and offline by default**. It:

- seeds the capture pad, when it is served (above) — no per-job `future/` or `delete/`
  folders, no mirror folders, no dashboards;
- strips the four retired ccc job-button entries (`ccc-*-job-from-file`) from an existing
  `obsidian-shellcommands` `data.json` (backed up first); it never adds an entry.

`-n/--dry-run` shows what would change; `-u/--uninstall` seeds nothing and only strips
the retired entries.

### Community plugins

| Plugin       | Used for                                                       |
| :----------- | :------------------------------------------------------------- |
| **Meta Bind** | the pad's labelled `## Controls` dropdowns (optional: the pad works from plain frontmatter) |

`ccc obsidian-setup --install-plugins` is the **only networked path** — it is
consent-gated and each download is sha256-verified against a pinned manifest (which also
still pins Obsidian Shell Commands; ccc registers no commands in it). After it runs,
reload Obsidian so the plugins load.

> **Meta Bind is pinned to 1.4.1**, not the latest 1.5.1: 1.5.1's `manifest.json` declares
> `minAppVersion 1.13.1`, but the current Obsidian release line is 1.12.x — installing 1.5.1
> there never registers and its widgets render as raw code. Repin to 1.5.1 once Obsidian
> 1.13+ is widespread.

## Launch credential check

Ticking `launch` on the pad while its prompt carries a live credential is refused (the
label, never the value, is written into the pad) when the check is armed:

| Key                   | Default                                  | What it does                                          |
| :-------------------- | :--------------------------------------- | :---------------------------------------------------- |
| `launch_secret_check` | `false`                                  | arms the check                                        |
| `mirror_scrub_cmd`    | `secret-broker-client.py scrub --shapes` | the credential scrubber whose `check` verb it runs    |

It fails closed: an armed check with no resolvable scrubber refuses every toggle launch,
and `ccc doctor` flags it.

## Troubleshooting

**Verifying a note renders (for agents/automation).** Obsidian normally runs without
a debugging port. Relaunch it with the Chrome DevTools Protocol enabled and drive it with
Playwright:

```commands
osascript -e 'quit app "Obsidian"'            # wait for exit, then:
open -a Obsidian --args --remote-debugging-port=9223
curl -s http://localhost:9223/json/version    # sanity: Obsidian answers over CDP
```

Then `connect_over_cdp("http://localhost:9223")`, find the page where
`typeof app !== 'undefined' && !!app.workspace`, run
`app.workspace.openLinkText('<vault-relative-note>', '', false)`, wait for it to render,
and screenshot. The flag does not persist across a normal relaunch.
