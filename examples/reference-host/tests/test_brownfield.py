"""Brownfield adoption: the guard, and the reason it exists.

This is design record 0030's headline defect, turned into a permanent readable
demonstration rather than a paragraph. It is also the case that matters most to
an adopting host, because a greenfield database can only take the happy path.

The situation: ``migrate()`` is **adopt-or-create**. Finding its tables already
present and no version table, it concludes the host's own history created them
and *stamps* the baseline as applied. That is irreversible in effect — the
baseline then never runs, and re-running ``migrate()`` cannot repair it.

So adoption is shape-verified before the stamp. A host that happens to own an
unrelated table called ``notification`` — which is not an exotic name — gets a
loud, specific error instead of a silently skipped baseline and a runtime
failure weeks later.

These tests deliberately drive ``migrate()`` against a hand-built database
rather than through the app, because the app's own boot can only ever produce
the greenfield case.
"""

from __future__ import annotations

import asas_notifications
import pytest
import sqlalchemy as sa
from sqlmodel import create_engine


@pytest.fixture()
def raw_engine(tmp_path):
    """An empty database with no Asas schema of any kind."""
    return create_engine(f"sqlite:///{tmp_path / 'incumbent.db'}")


def _incumbent_notification_table(engine, columns: str) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(f"CREATE TABLE notification ({columns})"))


def test_greenfield_adoption_is_the_boring_path(raw_engine):
    """No tables, no version table: the chain simply runs."""
    asas_notifications.migrate(raw_engine)

    inspector = sa.inspect(raw_engine)
    assert inspector.has_table("notification")
    assert inspector.has_table("alembic_version_asas_notifications")


def test_a_foreign_table_of_the_same_name_fails_loudly(raw_engine):
    """The defect this guard was added for.

    An incumbent application with its own ``notification`` table — a common
    name, not a contrived one — must not have it silently adopted. The failure
    has to arrive *here*, at boot, not as a missing column at 3am.
    """
    _incumbent_notification_table(
        raw_engine,
        "id INTEGER PRIMARY KEY, message TEXT, seen BOOLEAN",
    )
    # The sibling exists, so this is not the partial-schema case below.
    with raw_engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE notification_delivery (id INTEGER PRIMARY KEY)"))

    with pytest.raises(RuntimeError) as exc:
        asas_notifications.migrate(raw_engine)

    message = str(exc.value)
    # The error has to be actionable, not merely present: it must name the
    # table, say what is wrong with it, and say what to do.
    assert "notification" in message
    assert "missing the baseline columns" in message
    assert "Rename the existing table" in message


def test_a_partial_asas_schema_fails_rather_than_stamping(raw_engine):
    """The subtler half: the sentinel is right, but its siblings are missing.

    Stamping here would record the baseline as applied and leave
    ``notification_delivery`` uncreated forever — a failure that surfaces far
    from its cause. So a partial schema is refused too, with a different
    message, because the fix is different.
    """
    asas_notifications.migrate(raw_engine)

    # Reduce a healthy install to a partial one: drop a sibling and the version
    # table, leaving the sentinel behind.
    with raw_engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE notification_delivery"))
        conn.execute(sa.text("DROP TABLE alembic_version_asas_notifications"))

    with pytest.raises(RuntimeError) as exc:
        asas_notifications.migrate(raw_engine)

    message = str(exc.value)
    assert "notification_delivery" in message
    assert "partial" in message.lower()


def test_migrate_is_idempotent(raw_engine):
    """Called on every boot, so a second call must be a no-op."""
    asas_notifications.migrate(raw_engine)
    asas_notifications.migrate(raw_engine)

    with raw_engine.connect() as conn:
        revisions = conn.execute(
            sa.text("SELECT version_num FROM alembic_version_asas_notifications")
        ).all()
    assert len(revisions) == 1


# Namespacing (the escape hatch that lets an adopting host keep its own
# `notification` table and give the package a prefixed one) is TEAMY-796 and has
# not landed. When it does, its test belongs here, beside the failure it
# resolves — that adjacency is the point of putting the brownfield case in the
# reference host at all.
