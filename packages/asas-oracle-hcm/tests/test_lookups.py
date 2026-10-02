"""Id-to-name lookups, people, workers by address, and departments by set."""

import asyncio

import httpx
import pytest

from asas_oracle_hcm import (
    NullCache,
    OracleFusionClient,
    OracleLookups,
    OracleSettings,
    Person,
    WorkerDepartment,
    worker_address,
)

from conftest import collection


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def lookups(make_client):
    # NullCache so these tests exercise the lookups' own memo, not the client's.
    return OracleLookups(make_client(cache=NullCache()))


def test_names_ask_once_per_id_with_a_bare_numeric_filter(lookups, oracle):
    oracle.route(
        "/grades",
        lambda rec: httpx.Response(200, json={"items": [{"GradeName": f"Grade {rec.params['q'][-1]}"}]}),
    )
    assert run(lookups.names("grade", ["1", "2", "1", ""])) == {"1": "Grade 1", "2": "Grade 2"}
    assert sorted(c.params["q"] for c in oracle.calls) == ["GradeId=1", "GradeId=2"]
    assert oracle.calls[0].params["fields"] == "GradeId,GradeName"


def test_departments_are_keyed_on_organization_id(lookups, oracle):
    oracle.route("/departments", collection({"Name": "Digital Office"}))
    assert run(lookups.names("department", ["300000001"])) == {"300000001": "Digital Office"}
    assert oracle.calls[0].params["q"] == "OrganizationId=300000001"


def test_names_are_remembered_and_unknown_or_failed_ids_are_absent(lookups, oracle):
    def answer(rec):
        value = rec.params["q"].split("=")[1]
        if value == "boom":
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"items": [{"Name": "Architect"}] if value == "7" else []})

    oracle.route("/jobs", answer)
    assert run(lookups.names("job", ["7", "8", "boom"])) == {"7": "Architect"}
    oracle.calls.clear()
    assert run(lookups.names("job", ["7"])) == {"7": "Architect"}
    assert oracle.calls == []  # served from the memo
    lookups.clear()
    run(lookups.names("job", ["7"]))
    assert len(oracle.calls) == 1


def test_unknown_kind_is_a_programming_error(lookups):
    with pytest.raises(ValueError):
        run(lookups.names("colour", ["1"]))


def test_the_concurrency_cap_is_respected_across_kinds(make_client, oracle):
    in_flight = 0
    peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json={"items": [{"GradeName": "g", "Name": "n"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    client = OracleFusionClient(
        OracleSettings(base_url="https://pod.example.com/r", username="u", password="p"),
        http=http, cache=NullCache(),
    )
    lookups = OracleLookups(client, concurrency=3)
    result = run(lookups.names_many({"grade": [str(i) for i in range(6)], "job": [str(i) for i in range(6)]}))
    assert len(result["grade"]) == 6 and len(result["job"]) == 6
    assert peak == 3


def test_people_by_person_id_prefer_work_email_then_username(lookups, oracle):
    def answer(rec):
        pid = rec.params["q"].split("=")[1]
        rows = {
            "1": [{"PersonId": "1", "DisplayName": "Real", "WorkEmail": "real@x.ae"}],
            "2": [{"PersonId": "2", "DisplayName": "Scrubbed",
                   "WorkEmail": "sendmail-test-discard@oracle.com", "Username": "kept@x.ae"}],
            "3": [{"PersonId": "3", "DisplayName": "Login", "Username": "JDOE"}],
        }.get(pid, [])
        return httpx.Response(200, json={"items": rows})

    oracle.route("/publicWorkers", answer)
    people = run(lookups.people(["1", "2", "3", "4"]))
    assert people == {
        "1": Person("1", "Real", "real@x.ae"),
        "2": Person("2", "Scrubbed", "kept@x.ae"),
        "3": Person("3", "Login", ""),
    }
    oracle.calls.clear()
    run(lookups.people(["1", "2"]))
    assert oracle.calls == []


def test_worker_address_rules():
    assert worker_address({"WorkEmail": "SENDMAIL-TEST-DISCARD@oracle.com", "Username": "a@b.c"}) == "a@b.c"
    assert worker_address({"WorkEmail": " w@b.c "}) == "w@b.c"
    assert worker_address({"Username": "login"}) == ""


def _assignment_worker(*assignments):
    return {"PersonId": "300000001", "assignments": {"items": list(assignments)}}


def test_worker_department_reads_the_primary_assignment_by_work_email(lookups, oracle):
    oracle.route("/publicWorkers", lambda rec: httpx.Response(200, json={"items": [
        _assignment_worker(
            {"DepartmentId": "11", "DepartmentName": "Old", "PrimaryFlag": False},
            {"DepartmentId": "22", "DepartmentName": " Digital Office ", "PrimaryFlag": True},
        )
    ]} if rec.params["q"] == "WorkEmail='jane@example.gov'" else {"items": []}))
    found = run(lookups.worker_department("jane@example.gov"))
    assert found == WorkerDepartment("300000001", "22", "Digital Office")
    assert oracle.calls[0].params["expand"] == "assignments"


def test_worker_lookup_falls_back_to_username_lowercased(lookups, oracle):
    oracle.route("/publicWorkers", lambda rec: httpx.Response(200, json={"items": [
        _assignment_worker({"DepartmentId": "22", "DepartmentName": "D"})
    ]} if rec.params["q"] == "Username='jane@example.gov'" else {"items": []}))
    found = run(lookups.worker_department("Jane@Example.gov"))
    assert found is not None and found.department_id == "22"
    assert [c.params["q"] for c in oracle.calls] == [
        "WorkEmail='Jane@Example.gov'",
        "WorkEmail='jane@example.gov'",
        "Username='Jane@Example.gov'",
        "Username='jane@example.gov'",
    ]


def test_first_placed_assignment_wins_without_a_primary_flag(lookups, oracle):
    oracle.route("/publicWorkers", collection({"PersonId": "1", "assignments": [
        {"DepartmentId": None}, {"DepartmentId": "33", "DepartmentName": "Audit"},
    ]}))
    found = run(lookups.worker_department("a@x.ae"))
    assert found is not None and found.department_id == "33"


def test_worker_department_is_fail_soft(lookups, oracle, make_client):
    oracle.route("/publicWorkers", collection({"PersonId": "1", "assignments": []}))
    assert run(lookups.worker_department("a@x.ae")) is None
    oracle.route("/publicWorkers", (500, {}))
    assert run(lookups.worker_department("a@x.ae")) is None
    oracle.calls.clear()
    assert run(lookups.worker_department("not-an-address")) is None
    assert run(OracleLookups(OracleFusionClient(OracleSettings())).worker_department("a@x.ae")) is None
    assert oracle.calls == []


def test_departments_in_set_pages_in_id_order_and_filters_active(lookups, oracle):
    def pages(rec):
        offset = int(rec.params["offset"])
        rows = [{"OrganizationId": str(offset + i), "Name": f"D{offset + i}"} for i in range(200)]
        if offset:
            rows = [{"OrganizationId": "900", "Name": "Last"}, {"OrganizationId": "901", "Name": " "}]
        return httpx.Response(200, json={"items": rows, "hasMore": offset == 0})

    oracle.route("/departments", pages)
    found = run(lookups.departments_in_set("CORP_SET"))
    assert len(found) == 201 and found[-1] == ("900", "Last")
    first = oracle.calls[0].params
    assert first["q"] == "SetCode='CORP_SET';ActiveStatus='A'"
    assert first["orderBy"] == "OrganizationId:asc"
    oracle.calls.clear()
    run(lookups.departments_in_set("CORP_SET", active_only=False))
    assert oracle.calls[0].params["q"] == "SetCode='CORP_SET'"
    assert run(lookups.departments_in_set("  ")) == []
