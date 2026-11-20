# claude-command-center

## Purpose

A command center that organizes Claude Code sessions across projects by user-defined categories, tracking each session's AIM (a concrete done-condition), progress against that AIM as a checklist bar, and live status. The tool solves context fragmentation by centralizing session visibility and introducing "future jobs" — described work that can be parked and launched later from terminal, TUI, or Obsidian — externalizing memory and routing around rate limits.

## Key Capabilities

- **AIM-driven sessions**: Every session carries a stated done-condition; vague AIMs are flagged and auto-sharpened from file context and task state
- **Interactive TUI**: Cards grouped by category, live progress bars, session resumption from new tabs, optional browser UI (`ccc serve`)
- **Future jobs**: Park ideas as AIM + repo + optional prompt; launch later from CLI, TUI, or Obsidian buttons; support job dependencies
- **Optional LLM features**: Auto-derive sub-goals and grade progress; drift detection; independent AIM-met checker
- **Shell integration**: `c` wrapper asks for AIM before launching Claude, seeding the session automatically
- **Obsidian capture pad**: Queue (and launch) a future job from any device via one synced note
- **Background daemon**: Auto-reaps idle sessions, regenerates summaries, desktop notifications (launchd/systemd)
- **Auto-resume halted**: Resume rate-limit-blocked sessions once limits reset
- **macOS extras**: Karabiner hotkeys, iTerm2 integration, AppKit-based peek panel

## Tech Stack

Python 3.11+ | Textual (TUI) | PyYAML | Optional: textual-serve (browser), pyobjc (macOS), launchd/systemd (background daemon), Obsidian integration

## Key Scripts / Files

| File | Purpose |
|---|---|
| `command_center/cli.py` | Main CLI entry point (`ccc` command) |
| `command_center/views/tui.py` | Interactive Textual UI implementation |
| `command_center/shell_install.py` | Installs shell wrapper `c` and hooks |
| `command_center/core.py` | Core session/job tracking and models |
| `command_center/models.py` | Data models for sessions, jobs, state |
| `command_center/aimscore.py` | AIM concreteness scoring and sharpening |
| `command_center/autoprogress.py` | Auto-derive sub-goals and update progress |
| `command_center/drift.py` | Detect and report goal drift mid-session |
| `command_center/aimmet.py` | Grade whether AIM is actually met (independent checker) |
| `command_center/obsidian.py` | Obsidian vault mirroring and markdown generation |
| `command_center/daemon.py` | Background process: idle reaping, summary regen, notifications |
| `command_center/hooks.py` | Claude Code hooks for `/aim`, status line, live todos |
| `command_center/future_files.py` | Future job storage and sync |
| `command_center/resume.py` | Auto-resume rate-limit-halted sessions |
| `command_center/wizard.py` | Interactive setup wizard with consent checklist |
| `docs/reference.md` | Full feature reference and CLI docs |
