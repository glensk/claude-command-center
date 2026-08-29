"""Make every package module runnable by path (``./command_center/<mod>.py``).

Running a package module as a FILE leaves ``__package__`` empty, so its relative imports
(``from . import config``) die with *attempted relative import with no known parent
package*. A bare ``chmod +x`` does not help — without a shebang the shell tries to run the
Python source as shell script instead, which is worse. Each module therefore carries a
four-line guard above its imports that hands control here:

```python
if __name__ == "__main__" and not __package__:  # pragma: no cover - direct execution
    import os, sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)
```

This module then does two things the guard cannot do for itself:

* **Re-exec into the repo's own ``.venv``.** ``#!/usr/bin/env python3`` lands on the system
  interpreter, which does not have this package's dependencies (``rich``), so a direct run
  would die on the first third-party import. The venv is preferred whenever it exists.
* **Dispatch honestly.** A module with a ``main()`` is a real CLI and is called. A module
  without one is a library, and says so with a pointer to ``ccc`` — rather than importing
  successfully and exiting 0, which would look like a command that silently did nothing.

Deliberately stdlib-only and dependency-free: it runs BEFORE the venv re-exec, i.e. under
whatever interpreter the shebang happened to find.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import NoReturn

# Exit code for "this is a library module, not a command". Distinct from a CLI's own
# failure codes so a caller can tell "wrong file" from "command ran and failed".
EX_NOT_A_CLI = 2


def _repo_root(file: str) -> str:
    """The directory containing the ``command_center`` package."""
    return os.path.dirname(os.path.dirname(os.path.abspath(file)))


def _reexec_in_venv(file: str) -> None:
    """Re-run this same file under the repo venv's interpreter, if that is not us already.

    Returns normally when no re-exec is needed or possible; otherwise never returns.
    """
    venv = os.path.join(_repo_root(file), ".venv", "bin", "python")
    if not os.path.exists(venv):
        return
    if os.path.realpath(venv) == os.path.realpath(sys.executable):
        return  # already inside it — re-exec'ing would loop forever
    os.execv(venv, [venv, os.path.abspath(file), *sys.argv[1:]])


def run(file: str) -> NoReturn:
    """Entry point for a by-path run of ``file``; never returns."""
    _reexec_in_venv(file)
    name = Path(file).stem
    module = importlib.import_module(f"command_center.{name}")
    entry = getattr(module, "main", None)
    if callable(entry):
        raise SystemExit(entry())
    print(
        f"command_center/{name}.py is a library module, not a command.\n"
        f"Import it (`from command_center import {name}`) or use the CLI: ccc -h",
        file=sys.stderr,
    )
    raise SystemExit(EX_NOT_A_CLI)
