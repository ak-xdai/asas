"""Asas background jobs — durable DB-backed queue + interval scheduler.

No broker (works on SQLite and Postgres alike) with an in-process polling
worker. Extracted from Teamy (TEAMY-247, design record 0016; extraction epic
TEAMY-466, design record 0017). The package never imports host code: the host
registers handlers, injects its session factory/knobs, and optionally a
tenant-context binder.

Contract in one paragraph: producers :func:`enqueue` a ``kind`` + JSON payload
(or seed a :class:`JobSchedule` for recurring work); the runner claims rows with
a dialect-aware CAS (``FOR UPDATE SKIP LOCKED`` on Postgres), executes the
registered handler, and retries failures with exponential backoff until
``max_attempts`` — the ``failed`` rows are the dead-letter queue. Delivery is
**at-least-once**: every handler must be idempotent. Crash recovery is
lease-based (:func:`reclaim_expired`), not boot-only.

Host contract surface (table-owning variant: ``migrate`` yes, no seed/routers):
:func:`migrate` (package Alembic chain, adopt-or-create),
:func:`configure_runner`, :func:`configure_context_binder`,
:func:`register_handler`, and the queue/scheduler primitives below.
"""

from .migrate import migrate
from .models import BackgroundJob, JobSchedule, JobStatus
from .queue import claim_next, enqueue, mark_failed, mark_succeeded, reclaim_expired
from .registry import configure_context_binder, get_handler, register_handler
from .runner import configure_runner, run_loop, run_once
from .scheduler import ensure_schedule, tick_schedules

__version__ = "0.11.1"

__all__ = [
    "BackgroundJob",
    "JobSchedule",
    "JobStatus",
    "claim_next",
    "configure_context_binder",
    "configure_runner",
    "enqueue",
    "ensure_schedule",
    "get_handler",
    "mark_failed",
    "mark_succeeded",
    "migrate",
    "reclaim_expired",
    "register_handler",
    "run_loop",
    "run_once",
    "tick_schedules",
    "__version__",
]
