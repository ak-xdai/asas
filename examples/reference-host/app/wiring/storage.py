"""asas-storage.

Contract row: **Host hooks** (``configure``).

The trap this file exists for: ``configure()`` must run before *anything* calls
``storage()``, and the first caller is usually a seed or a migration, not a
request. So it is step 2 of the boot sequence — ahead of the package chains and
the seeds, not alongside the other hooks in step 3.

The package fails loudly rather than falling back to disk when it is skipped,
which is the correct trade: a silent local fallback in production writes files
onto a container filesystem that vanishes on the next deploy.
"""

from __future__ import annotations

from pathlib import Path

import asas_storage

from ..config import settings


def _factory() -> asas_storage.Storage:
    """Local disk, because the core tier must need no configuration.

    Swapping to S3 or Azure Blob is a change to this function and nothing else:
    the keys stored in the database are backend-independent relative paths, so
    the backend can change without touching a single row.
    """
    root = Path(settings.uploads_dir)
    root.mkdir(parents=True, exist_ok=True)
    return asas_storage.LocalStorage(root)


def configure() -> None:
    """Step 2 of the boot sequence — before init/migrate, per the docstring."""
    asas_storage.configure(_factory)
