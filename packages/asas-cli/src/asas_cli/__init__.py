"""Asas CLI — the low-friction on-ramp to the Asas package collection.

Not a runtime framework: it never wraps or wires anything at import time in
a consuming project. It has exactly two jobs, both one-shot code generators:

- ``asas add <package>`` — pins one Asas package into an existing project's
  ``pyproject.toml`` (the correct ``git+https://...#subdirectory=...`` line,
  built for you instead of hand-typed).
- ``asas new <project> --with <packages>`` — scaffolds a new FastAPI project
  with a working ``main.py``/``settings.py`` boot sequence for the packages
  you chose. The output is plain, editable Python the team owns afterward —
  regenerating doesn't merge back into a file you've since edited.

See the repo README's "host contract" section for what every package
promises; ``asas_cli.registry`` and ``asas_cli.templates`` are this CLI's
(non-authoritative) description of that contract, used only to scaffold —
each package's own tests are what actually enforces it.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
