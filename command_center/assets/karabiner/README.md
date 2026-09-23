# Karabiner-Elements samples for ccc

Two **sample** complex-modification rules that bind a chord to a `ccc` command.
There is **no installer** — these are docs-only reference JSONs you paste into
Karabiner yourself.

| File            | Chord                              | Runs        | Scope                     |
| :-------------- | :--------------------------------- | :---------- | :------------------------ |
| `peek-s-p.json` | hold **s**, tap **p** (`s`+`p`)    | `ccc peek`  | iTerm2 frontmost only     |
| `jump-f-j.json` | hold **f**, tap **j** (`f`+`j`)    | `ccc jump`  | global (any app)          |

Both use the same idiom: a `simultaneous` two-key press with strict key-down
order and a 500 ms threshold, firing a detached `shell_command`.

## Install by hand

1. Find the absolute path to your `ccc` binary:

   ```commands
   command -v ccc
   ```

2. In each JSON, replace the placeholder `REPLACE_WITH_ABS_CCC_PATH` with that
   absolute path (Karabiner runs `shell_command` with a minimal environment, so a
   bare `ccc` on `$PATH` is not reliable — use the full path).

3. Open `~/.config/karabiner/karabiner.json`, find your active profile, and add
   each rule object to `profiles[].complex_modifications.rules` (that key is an
   array — append there). Keep a backup of `karabiner.json` first.

   Alternatively, drop each file into
   `~/.config/karabiner/assets/complex_modifications/` and enable the rule from
   **Karabiner-Elements → Complex Modifications → Add rule**.

4. Karabiner reloads `karabiner.json` automatically on save; if not, toggle the
   profile or restart Karabiner-Elements.

## Notes

- `ccc peek` and `ccc jump` are macOS/iTerm2-oriented; on other setups they
  degrade (see the ccc README). The `f`+`j` chord shadows a plain `f`-then-`j`
  sequence while `f` is held — pick a different chord if that collides with your
  typing.
- These rules are **not** wired by `ccc init` or any installer; they live here as
  copy-paste starting points only.

## Optional: the resident panel server (macOS)

The q+p park panel and the s+p peek panel can open in ≤ 0.2 s instead of the cold
0.35–1.1 s when the opt-in resident server runs (`ccc panel-server --install`, see
`docs/reference.md` § Resident panel server). It installs a POSIX poker at
`~/.claude/command-center/panel-poke.sh` (under `$CCC_HOME/command-center` when set);
point the chord's `shell_command` at it instead of `ccc`:

```json
"shell_command": "$HOME/.claude/command-center/panel-poke.sh peek >/dev/null 2>&1 &"
```

(`… panel-poke.sh park …` for q+p). The poker falls back to the cold `ccc peek` /
`ccc park -g` by itself whenever the server is not installed, not running, degraded or
wedged, so the rewired rule is safe with or without it. The sample JSONs here stay cold.

