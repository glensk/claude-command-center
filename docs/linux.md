# ccc on Linux (Ubuntu quickstart)

`ccc` is developed on macOS but runs on Linux. This page is the honest map of what works
out of the box on a plain Ubuntu desktop, what needs `tmux`, and what stays macOS-only.

## Install

```commands
# the CLI on PATH (uv-first; pipx/pip also work)
uv tool install git+https://github.com/glensk/claude-command-center

# try it with fake data, zero setup
ccc demo --ls

# first-run wizard: environment check → consent → installers → doctor
ccc init            # or `ccc init -y` for the recommended profile non-interactively
```

`ccc init` on Linux writes the same minimal `config.toml`, wires the Claude Code hooks and
status line, and (below) can install the **systemd --user** daemon. It also prints a pointer
to the Linux **hotkey samples** (see the bottom of this page).

## Works out of the box

- **Core TUI** (`ccc`), **`ccc ls`**, the whole session model — AIM, progress bars,
  sub-goals, future jobs, drift/score/DONE checkers (the "score ladder" picks whatever LLM
  CLI you have: copilot/gemini/codex/claude).
- **Hooks + status line** — `ccc install-hooks`, `ccc install-statusline`, `ccc doctor`.
- **Deterministic tab symbols** — every session row shows a stable per-repo emoji, and
  **`ccc install-shell`** adds **cross-terminal OSC tab badges** that title your terminal
  tab `⟨symbol⟩ ⟨repo⟩` on `cd` (gnome-terminal, konsole, any emulator — see below).
- **Obsidian setup** and the capture pad (`ccc obsidian-setup`) — identical to macOS.
- **Desktop notifications** via **`notify-send`** (libnotify). The default `notify = ["auto"]`
  resolves to `notify-send` on Linux; install it with `sudo apt install libnotify-bin`. A
  missing `notify-send` is a silent no-op.
- **Background daemon** as a **systemd --user** service + timer (see next section).

## The daemon — systemd --user

On Linux `ccc daemon --install` generates and starts a systemd **user** service+timer (the
launchd-agent equivalent), under `~/.config/systemd/user/`:

```commands
ccc daemon --install     # write + enable --now <label>.service + .timer
ccc daemon --status      # is the recurring timer active?
ccc daemon --uninstall   # disable + remove the units
ccc daemon               # run a single pass by hand (no service needed)
```

- The **timer** fires `ccc daemon` every `daemon_interval_sec` seconds
  (`OnUnitActiveSec`, the `StartInterval` equivalent).
- When any **vault feature** is on, a `<label>-future-sync.path` unit is generated too — the
  systemd counterpart of launchd `WatchPaths` — running `ccc sync-future` when the vault's
  future dir changes. (systemd path units watch a directory, not recursively; the periodic
  daemon pass covers deeper edits.)
- `ccc doctor`'s Daemon section is platform-aware: it reports the systemd timer's state on
  Linux (and the launchd agent on macOS).

Logs land in `~/.claude/command-center/daemon.log` (and `.err`), as on macOS.

## Needs tmux

Launching and resuming sessions in a **new window** uses AppleScript+iTerm on macOS. On
Linux, set the launcher to tmux:

```toml
# ~/.claude/command-center/config.toml
launcher = "tmux"
```

Then `ccc start-job`, `ccc resume-job` and the Obsidian ▶ buttons open a window in the
persistent `tmux_session`. Plain-terminal users who don't want tmux can still
**`ccc resume <id>`** to resume a session **in place** (an `execvp` in the current terminal).

## Stays macOS-only

- **`ccc peek`'s floating panel** — it uses AppKit. On Linux `ccc peek` **auto-degrades to
  `--print`**: it dumps the focused/selected session's prompts to stdout instead of showing
  a panel (no crash, no AppKit import). Bind it to a hotkey with the samples below.
- **`ccc jump`** (the ccc-tab ↔ session-tab toggle) — iTerm/AppleScript + a Karabiner chord.
- **iTerm tab colors** — the coloured iTerm tabs are iTerm-specific. The **OSC title
  badges** from `ccc install-shell` are the cross-terminal replacement and *do* work.

## `ccc install-shell` — AIM-at-startup + tab badges

```commands
ccc install-shell            # detect zsh/bash from $SHELL, write a markered rc block
ccc install-shell -n         # dry-run: print the block + target rc
ccc install-shell -u         # remove only the block
ccc install-shell --no-wrapper   # badges only    (or --no-badges for the wrapper only)
```

It adds two independent pieces to your `~/.bashrc` / `~/.zshrc`:

1. an **AIM-at-startup wrapper** — a shell function (default name `c`) that asks
   `AIM of this session (empty to skip):` and launches `claude` with `CLAUDE_SESSION_AIM`
   set, so the session starts already knowing its done-condition (empty input runs `claude`
   plain). If a command named `c` already exists it refuses — pass `-w NAME` or `--no-wrapper`.
2. **tab badges** — a precmd/`PROMPT_COMMAND` hook that titles the tab `⟨symbol⟩ ⟨repo⟩` via
   plain OSC (`printf '\033]0;…\007'`), the symbol from `ccc tab-symbol --print "$PWD"`,
   cached per directory so `ccc` runs at most once per `cd`.

## Hotkey samples (keyd / xremap)

The Linux counterpart of the macOS Karabiner samples lives in the package under
`command_center/assets/hotkeys-linux/` (also printed by `ccc init` on Linux):

- **`keyd-peek-s-p.conf`** — keyd; the true **hold `s`, tap `p`** chord → `ccc peek --print`.
- **`xremap-peek.yml`** — xremap; a practical **Super+p** → `ccc peek --print`.

They are **suggested-only** (no installer). Because the peek panel is macOS-only, the Linux
chord just runs `ccc peek --print` and routes the text to `notify-send` (or a scratch file);
the cleanest experience is to run `ccc peek --print` in your terminal directly. See that
folder's `README.md` for the honest caveats.
