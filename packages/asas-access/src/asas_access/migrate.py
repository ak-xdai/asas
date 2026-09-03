"""Package-owned schema migrations — the ``migrate(engine)`` part of the Asas host
contract (DR 0017 §5).

The chain lives in ``migrations/`` inside the package and tracks its position in a
package-scoped version table, so it composes with (never touches) the host's own
Alembic chain. ``migrate`` is **adopt-or-create**:

- fresh host: no version table, no tables → run the chain from the baseline;
- adopting host (e.g. Teamy, whose historical chain already created these tables):
  no version table but the tables exist → *stamp* the baseline, then upgrade.

Adoption is guarded: a table name is not an identity, so before stamping we check
that the sentinel actually carries the baseline's columns. A host that happens to
own an unrelated table of the same name gets a loud error here rather than a
silently skipped baseline and a runtime failure later (see _assert_adoptable).
"""

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

VERSION_TABLE = "alembic_version_asas_access"
_BASELINE = "0001"
_SENTINEL_TABLE = "field_permission"
# Columns the baseline creates on the sentinel. Baselines are frozen, so this
# set is a constant; see _assert_adoptable.
# Every table the baseline revision creates. Genuine adoption means the host's
# own history created all of them; a subset is a partial schema, not an adopt.
_BASELINE_TABLES = frozenset({
    "access_group",
    "access_group_membership",
    "action_permission",
    "field_permission",
})
_SENTINEL_COLUMNS = frozenset({
    "id",
    "org_id",
    "entity_type",
    "field",
    "action",
    "principal",
})


# Columns this package's own migrations RENAME in place, as (old, new).
#
# The sentinel's identity check has to accept either vocabulary: the baseline
# name before that migration, the new name after it. A table carrying NEITHER
# name of a pair is somebody else's; a table carrying every POST-rename name
# with no version table is our own schema that has lost its bookkeeping, which
# is a different problem with a different remedy, and stamping the baseline
# over it would wreck it.
#
# Empty for a package whose baseline column names have never moved.
_RENAMED_PAIRS: tuple[tuple[str, str], ...] = ()


def _config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["engine"] = engine
    return cfg


def _assert_adoptable(inspector) -> None:
    """Guard the adopt path in :func:`migrate`.

    Adoption keys on a table *name*, which is not an identity, and stamping is
    irreversible in effect: it records the baseline as already applied, so the
    baseline never runs and a re-run of ``migrate()`` cannot repair it. Both
    checks below therefore have to pass *before* the stamp.

    1. **Every baseline table must be present.** A sentinel that looks right
       while its siblings are missing is a partial schema; stamping would leave
       those tables uncreated forever, and the failure surfaces much later at
       runtime, far from the cause.
    2. **The sentinel must carry the baseline's columns.** Extra columns are
       fine (a later revision, or the host's own); missing ones mean this is
       somebody else's table that happens to share the name.
    """
    absent = sorted(t for t in _BASELINE_TABLES if not inspector.has_table(t))
    if absent:
        raise RuntimeError(
            f"asas-access cannot adopt the existing {_SENTINEL_TABLE!r} table: the "
            f"baseline's other tables are missing {absent}. This database holds a "
            f"partial asas-access schema; stamping it would record the baseline as "
            f"applied and never create them. Restore or drop the partial schema "
            f"and retry."
        )
    actual = {c["name"] for c in inspector.get_columns(_SENTINEL_TABLE)}
    # `_RENAMED_PAIRS and ...` is load-bearing: `all()` over an empty sequence
    # is True, so without it every package that renames nothing would refuse
    # each and every adopt with the error below.
    if _RENAMED_PAIRS and all(
        new in actual and old not in actual for old, new in _RENAMED_PAIRS
    ):
        # The post-rename shape with no version table: this is the package's OWN
        # table whose migration bookkeeping went missing (a partial restore, or
        # an adopting host that tracked our chain in its own). Stamping the
        # baseline would replay the chain over it and fail, and the remedy is
        # never to rename or drop a table that holds real data.
        raise RuntimeError(
            f"asas-access found its own post-rename {_SENTINEL_TABLE!r} schema but no "
            f"{VERSION_TABLE!r} table. Restore the version table, or stamp the "
            f"chain at its true revision, and do NOT rename or drop the table: "
            f"it holds real data."
        )
    missing = sorted(
        (_SENTINEL_COLUMNS - actual)
        | {old for old, new in _RENAMED_PAIRS if old not in actual and new not in actual}
    )
    if missing:
        raise RuntimeError(
            f"asas-access cannot adopt the existing {_SENTINEL_TABLE!r} table: it is "
            f"missing the baseline columns {missing}. This database already "
            f"contains an unrelated table named {_SENTINEL_TABLE!r}, so asas-access "
            f"cannot use that name. Rename the existing table and retry."
        )


def migrate(engine: Engine) -> None:
    """Bring this package's tables to the current schema. Idempotent; call at boot
    alongside the host's own migrations."""
    inspector = sa.inspect(engine)
    cfg = _config(engine)
    if not inspector.has_table(VERSION_TABLE) and inspector.has_table(_SENTINEL_TABLE):
        _assert_adoptable(inspector)
        command.stamp(cfg, _BASELINE)
    command.upgrade(cfg, "head")
