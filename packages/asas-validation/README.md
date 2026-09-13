# asas-validation

Business rules on data, written once as one-line **checks**, enforced the same
way on every write path (API, bulk import, AI agent), explained to the user on
the exact field. Ships a **44-check library** (dates, numbers, cross-field,
presence, formats, lists); every check is a plain function whose name reads as
the rule it enforces.

Table-less variant of the Asas host contract: no session dependency, no `seed`,
no `migrate`. Design record: DR 0004.

## The check library (0.11.1)

```python
from asas_validation import validate, enforce

@router.post("/interviews")
def create_interview(payload: InterviewIn, session=Depends(get_session)):
    job = session.get(Job, payload.job_id)
    application = session.get(Application, payload.application_id)
    new = payload.model_dump(exclude_unset=True)
    enforce([
        validate.email(new.get("candidate_email"), field="candidate_email"),
        validate.between(new.get("scheduled_date"), job.posting_date,
                         job.closing_date, field="scheduled_date"),
        validate.after(new.get("scheduled_date"), application.submitted_date,
                       days=3, field="scheduled_date"),
    ])
    ...
```

- Each check returns `None` when valid, else a `Violation` (field, code,
  params, message key). **`enforce(list)`** drops the `None`s and raises one
  422 carrying every remaining violation — the user fixes everything in one
  round trip. The envelope is FastAPI's own shape, so clients keep a single
  error path for shape errors and business errors.
- **Absent values pass** (a partial edit that never touched a date must not be
  blocked by a rule about it) — except the presence family
  (`required_when` and friends), whose subject is absence itself.
- **Business logic composes at the call site** from generic checks — a
  recurring composite is a plain host function returning a list, never a new
  check. Checks stay domain-generic.
- **Your own checks are functions in a file**, not registrations: write them
  in `app/checks.py` shaped like the shipped ones and call
  `include_checks(app_checks)` once at startup. `validate.iban` exists because
  a function named `iban` exists.
- **`catalog()`** lists every check machine-readably (what it takes, its
  settings, its human sentence) — derived from the functions by introspection,
  for agents, docs, and clients.
- **`configure_clock(fn)`** supplies "today" (timezone-correct, testable);
  date-meets-datetime coerces instead of crashing.
- Live form feedback: expose the same check list twice — a `/check` endpoint
  that enforces without saving (called as the user types) and the real save.
  Same rules at both moments by construction.

## The declared-rules engine (unchanged)

The original engine remains as-is for rules attached to entities as data:
`declare_rules([Rule(...)])` + `register_fields` + `assert_rules_known()` at
boot, `raise_if_invalid(entity, record, changes)` in routers — edit-aware
(partial updates check only the rules the edit touches), with `build_router()`
serving the declared rule set (ETag-cached). DR 0004 phase 2 rebases its four
kinds onto the check library with no behavior change.

**→ Full documentation**: docstrings on every public name; the 44-check
catalog prints itself: `python -c "import asas_validation, json;
print(json.dumps(asas_validation.catalog(), indent=2, default=str))"`.
