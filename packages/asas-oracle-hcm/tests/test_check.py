"""The gateway check: every read once, the writes never, no body printed."""

import asyncio

import httpx

from asas_oracle_hcm import OracleSettings
from asas_oracle_hcm.check import BY_KEY, LISTS, NOT_CALLED, check, render

from conftest import BASE, collection

ENCLOSURE = "/recruitingCandidates/C2/child/attachments/ABC123/enclosure/FileContents"


def _instance(oracle):
    oracle.route("/recruitingJobRequisitions", collection({"RequisitionNumber": "7001"}))
    oracle.route("/recruitingCandidates", collection({"CandidateNumber": "C1"}, {"CandidateNumber": "C2"}))
    oracle.route("/recruitingJobRequisitions/7001", (200, {"RequisitionNumber": "7001"}))
    oracle.route("/recruitingCandidates/C1", (200, {"CandidateNumber": "C1"}))
    oracle.route("/recruitingCandidates/C2", (200, {"CandidateNumber": "C2"}))
    oracle.route("/recruitingCandidates/C1/child/attachments", collection())
    oracle.route(
        "/recruitingCandidates/C2/child/attachments",
        collection({"links": [{"name": "FileContents", "href": f"{BASE}{ENCLOSURE}"}]}),
    )
    oracle.route(ENCLOSURE, lambda rec: httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}))


def test_every_read_is_called_once_and_no_write_is(client, oracle):
    _instance(oracle)
    results = asyncio.run(check(client))
    assert len(results) == len(LISTS) + len(BY_KEY) + len(NOT_CALLED) == 20
    assert all(r.ok for r in results if r.status not in ("not called", "skipped")), results
    assert {c.method for c in oracle.calls} == {"GET"}, "a check never writes"
    file_read = next(r for r in results if r.operation.endswith("/enclosure/FileContents"))
    assert file_read.detail == "8 bytes", "the first candidate WITH an attachment was found"


def test_a_refusal_is_reported_with_its_status_and_one_line(client, oracle):
    _instance(oracle)
    oracle.route("/grades", (500, {"Exception": "API Gateway encountered an error.\n Transport protocol not supported"}))
    results = asyncio.run(check(client))
    grades = next(r for r in results if r.operation == "/grades")
    assert grades.status == "500" and "\n" not in grades.detail and "Transport" in grades.detail
    text = render(OracleSettings(base_url=BASE, username="u", password="p"), results)
    assert "Basic auth sent" in text and "of 18 calls answered 2xx" in text
