"""Enable ``python -m command_center`` as an alias for the ``ccc`` CLI.

Same dispatcher as the ``ccc`` console script: :mod:`command_center.entrypoint` (the lean
``ccc aim`` path, else ``cli.main``).
"""

from __future__ import annotations

from .entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
