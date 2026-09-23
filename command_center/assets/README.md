# command_center/assets

Package data shipped inside the `command_center` wheel (it lives under the package
directory, so hatchling includes it in both the sdist and the wheel — verified by the
packaging smoke test). Because it ships with the installed package, code can read it at
runtime through `importlib.resources`, e.g.:

```python
from importlib.resources import files

text = (files("command_center") / "assets" / "README.md").read_text(encoding="utf-8")
```

## What belongs here

Static, non-Python resources that `ccc` needs at runtime and that must survive a
**non-editable** install (a plain `pip install` / `uv tool install` of the wheel, where
the source tree is gone). Anything referenced only from a source checkout does **not**
belong here.

## Layout

| Path                | Seeded by                          | What it is                                                                 |
| :------------------ | :--------------------------------- | :------------------------------------------------------------------------- |
| `commands/*.md`     | `ccc install-commands`             | The seven ccc slash commands (aim, next-step, done, block, deadline, aim-history, subgoal-history) → `$CLAUDE_HOME/commands/`. |
| `codex/`            | `ccc install-commands -x`          | The optional Codex delegate command (`codex/commands/…`) + skill (`codex/skills/…/SKILL.md`). Both call `codex-in-claude` (the console entry point), never a personal path. |
| `obsidian/*.md.tmpl`| `ccc obsidian-setup`               | The four dataviewjs dashboards (future/running/parked/delete). Placeholders `{{CCC_BIN}}`, `{{FUTURE_FOLDER}}`, `{{RUNNING_FOLDER}}`, `{{DELETE_FOLDER}}`, `{{REPO_TREE}}` are substituted from config at render time; every generated file carries a `ccc_generated: true` frontmatter marker. |
| `obsidian/plugins.json` | `ccc obsidian-setup --install-plugins` | Pinned community-plugin releases (Meta Bind 1.4.1, shellcommands 0.23.0, dataview 0.5.70) with per-file GitHub URLs + real sha256 hashes. Meta Bind is held at 1.4.1 because 1.5.1 requires Obsidian ≥ 1.13.1 (see the entry's `note`). |
| `panel-poke.sh`     | `ccc panel-server --install`       | POSIX `sh` template of the Karabiner-side panel poker (hands a `park`/`peek` chord to the resident panel server, else execs the cold `ccc park -g` / `ccc peek`). `command_center.panelpoke.install_poker` replaces `@APP_HOME@`, `@COLD_CCC@` and the `# @ENV_OVERRIDES@` line with shell-quoted values and writes `<app_home>/panel-poke.sh` atomically, mode 0755. |
| `karabiner/`        | (docs only — no installer)         | Two sample Karabiner chord rules (`s`+`p` peek, `f`+`j` jump) with a `REPLACE_WITH_ABS_CCC_PATH` placeholder, plus a README explaining manual install. |

The templates and vendored commands are **generified**: they contain no personal absolute
paths (the `tools/check_public_tree.py` scanner enforces this).
