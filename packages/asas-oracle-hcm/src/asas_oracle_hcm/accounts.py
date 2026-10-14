"""A person's Oracle USER ACCOUNT, and whether it holds a security role.

The question a host asks before letting somebody do something Fusion itself
reserves: "does this person hold role X in Oracle". It is a read of
``/userAccounts`` and its ``userAccountRoles`` child, and it is here rather
than in a host because nothing about it is domain specific: which codes
count is the caller's, the way to ask is Oracle's.

**THE ADDRESS IS THE JOIN.** Nothing carries both a host's own account id and
Oracle's, so an email address is the only fact both systems hold about the
same human. Two routes to the account, cheapest first: ``Username`` (which on
many instances IS the address), then the worker's ``WorkEmail`` to a
``PersonId`` and the account from that.

**A NON-PRODUCTION POD SCRUBS THE USERNAME.** On one measured pod, 1,874 of a
2,000-account sample had a fixed suffix appended after the domain. So a
caller may name that suffix, and the suffixed spellings are tried FIRST,
because on such a pod they are the likely ones. See :func:`usernames_for`.

**ONE EQUALITY PER ROLE CODE, never one query for all of them**, and this is
the trap worth keeping: under Oracle's DEFAULT REST framework version,
``RoleCode IN (a,b)`` matches NOTHING and ``a or b`` honours ``a`` alone.
Neither is refused. Both answer an ordinary 200 with a page that looks
empty, so a person who holds the role reads as not holding it, and a
permission check fails in the SAFE-LOOKING direction while being wrong.
``expand=userAccountRoles`` is no way round it either: before framework
version 3 it stops at 25 children and says nothing about having done so (29
roles held, 25 returned).

Sending ``REST-Framework-Version: 2`` would allow a single query, and
:data:`~asas_oracle_hcm.REST_FRAMEWORK_V2` is there for a caller who has
checked that every hop forwards it. An API gateway in the middle may not,
and an unforwarded header fails the same silent way, so the version 1 form
is what this module uses: it is correct on every route.

**FAIL CLOSED IS THE CALLER'S JOB, NOT THIS MODULE'S.** `holds_any_role`
answers True or False for a question Oracle answered, and RAISES when Oracle
could not be asked. A caller must not read the raise as "no": a permission
rule that treats an unreachable Oracle as a refusal is right, and one that
treats it as a pass is a hole, but either way the decision belongs to the
rule and not to the read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from .errors import OracleNotFoundError, OracleUpstreamError
from .query import eq

logger = logging.getLogger(__name__)

USER_ACCOUNTS = "/userAccounts"
WORKERS = "/publicWorkers"

#: What a non-production pod puts in `WorkEmail` instead of the real address,
#: Oracle's own guard against a test instance mailing real people. Looking it
#: up finds whichever worker happens to sort first, so it is never asked.
SCRUBBED_EMAIL = "sendmail-test-discard@oracle.com"


def usernames_for(address: str, suffix: str = "") -> list[str]:
    """The spellings an address may be stored under as a ``Username``, most
    likely first.

    With a suffix configured the suffixed spellings LEAD, because a pod that
    scrubs stores nearly every username that way. Duplicates are dropped, so
    an already-lowercase address asks two spellings and not four.
    """
    plain = [address, address.lower()]
    if not suffix:
        return list(dict.fromkeys(plain))
    return list(dict.fromkeys([f"{address}{suffix}", f"{address.lower()}{suffix}", *plain]))


async def find_account(client: Any, address: str, suffix: str = "") -> dict[str, Any] | None:
    """The Oracle user account behind ``address``, or None.

    Every username spelling is asked AT ONCE and the first hit in priority
    order wins. Asked one after another, a miss on the likely spelling costs a
    whole round trip: measured at 6.4s for three sequential calls on one pod,
    against a 5s budget in the UI that waited for it.

    A failure on a spelling ranked ABOVE the hit still fails the lookup, which
    is deliberate: that spelling might have been the answer, so a hit from a
    lower-ranked one is not known to be the right account.
    """
    found = await asyncio.gather(
        *(_by_username(client, name) for name in usernames_for(address, suffix)),
        return_exceptions=True,
    )
    for hit in found:
        if isinstance(hit, BaseException):
            raise hit
        if hit:
            return hit
    if address.lower() == SCRUBBED_EMAIL:
        return None
    for value in dict.fromkeys([address, address.lower()]):
        workers, _, _ = await client.get_collection(
            WORKERS,
            {"q": eq("WorkEmail", value, strip_quotes=True), "fields": "PersonId", "limit": 1},
            use_cache=False,
        )
        person_id = str((workers[0] if workers else {}).get("PersonId") or "").strip()
        if not person_id:
            continue
        rows, _, _ = await client.get_collection(
            USER_ACCOUNTS,
            {
                "q": eq("PersonId", person_id, quote=False),
                "fields": "GUID,SuspendedFlag",
                "limit": 1,
            },
            use_cache=False,
        )
        return rows[0] if rows else None
    return None


async def _by_username(client: Any, username: str) -> dict[str, Any] | None:
    rows, _, _ = await client.get_collection(
        USER_ACCOUNTS,
        {
            "q": eq("Username", username, strip_quotes=True),
            "fields": "GUID,SuspendedFlag",
            "limit": 1,
        },
        # NEVER the cache: a permission answer is read as Oracle is NOW, and a
        # caller that keeps it keeps it by its own rules and for its own time.
        use_cache=False,
    )
    return rows[0] if rows else None


async def holds_any_role(
    client: Any, address: str, role_codes: Sequence[str], *, suffix: str = ""
) -> bool:
    """Whether the account behind ``address`` holds ANY of ``role_codes``.

    False for: no account, a SUSPENDED account, an account with no GUID, and
    an account holding none of the codes. RAISES
    :class:`OracleUpstreamError` when Oracle could not answer, which a caller
    must not read as False (see the module note).

    Stops at the first code held, so a deployment naming one or two codes
    makes one or two small reads.
    """
    codes = tuple(c for c in role_codes if c)
    if not codes:
        return False
    try:
        account = await find_account(client, address, suffix)
        if account is None or account.get("SuspendedFlag") is True:
            return False
        guid = str(account.get("GUID") or "").strip()
        if not guid:
            return False
        for code in codes:
            rows, _, _ = await client.get_collection(
                f"{USER_ACCOUNTS}/{guid}/child/userAccountRoles",
                {"q": eq("RoleCode", code), "fields": "RoleCode", "limit": 1},
                use_cache=False,
            )
            # The code is compared again here rather than trusted from the
            # filter: see the module note on what version 1 does with a
            # filter it does not support.
            if any(str(row.get("RoleCode") or "") == code for row in rows):
                return True
        return False
    except OracleNotFoundError as exc:
        # A COLLECTION query never 404s for a person Oracle does not know: it
        # answers an empty page. So a 404 here means the RESOURCE is not
        # reachable at all, typically a gateway that does not carry the
        # operation. That is an unknown answer and never a "no".
        raise OracleUpstreamError("Oracle's user accounts are not reachable here.") from exc
