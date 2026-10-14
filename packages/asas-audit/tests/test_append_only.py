"""Edits and deletes are refused by the DATABASE, not merely absent from the code.

This is the layer that survives a compromised application, a stray migration, or
somebody at a psql prompt. It is eight lines of plpgsql that nobody writes
unprompted, and it is the difference between a log that is append-only by
convention and one that is append-only in fact.

Postgres only, and honestly so: the trigger is not portable, so on SQLite this
package's append-only property rests on the first two layers alone (no mutating
code, and row-level security). A host that needs the third needs Postgres, which
the README says rather than leaving a reader to infer it from a skip.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from conftest import ORG_A, append_and_commit


def test_update_is_refused(requires_postgres):
    engine = requires_postgres
    append_and_commit(engine, ORG_A)
    with pytest.raises(Exception) as caught:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL app.tenant_id = 'org-a'"))
            conn.execute(text("UPDATE audit_event SET actor = 'mallory'"))
    assert "append-only" in str(caught.value)


def test_delete_is_refused(requires_postgres):
    engine = requires_postgres
    append_and_commit(engine, ORG_A)
    with pytest.raises(Exception) as caught:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL app.tenant_id = 'org-a'"))
            conn.execute(text("DELETE FROM audit_event"))
    assert "append-only" in str(caught.value)


def test_the_error_names_the_operation(requires_postgres):
    """So the failure reads as a policy rather than as a mysterious constraint."""
    engine = requires_postgres
    append_and_commit(engine, ORG_A)
    with pytest.raises(Exception) as caught:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL app.tenant_id = 'org-a'"))
            conn.execute(text("UPDATE audit_event SET actor = 'mallory'"))
    assert "UPDATE" in str(caught.value)


def test_insert_is_still_allowed(requires_postgres):
    """The trigger must be BEFORE UPDATE OR DELETE and nothing else. A trigger on
    INSERT too would make the table unwritable, which is a failure mode worth one
    test rather than a careful reading of the migration."""
    engine = requires_postgres
    append_and_commit(engine, ORG_A)
    append_and_commit(engine, ORG_A, action="second.thing")
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.tenant_id = 'org-a'"))
        assert conn.execute(text("SELECT count(*) FROM audit_event")).scalar() == 2


def test_truncate_is_not_covered_and_that_is_stated(requires_postgres):
    """A row-level trigger cannot see TRUNCATE, so this documents the boundary
    rather than pretending it is closed.

    Closing it would mean a second, statement-level trigger. It is deliberately
    not added: TRUNCATE requires table ownership or an explicit grant, so it is
    not reachable by the ordinary application role in a deployment that follows
    the least-privilege advice in the README, and a guard that only fires for
    someone who already owns the table buys very little.
    """
    engine = requires_postgres
    append_and_commit(engine, ORG_A)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_event"))
        assert conn.execute(text("SELECT count(*) FROM audit_event")).scalar() == 0
