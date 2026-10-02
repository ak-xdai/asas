"""The gateway check: probes from a manifest, values chained between them, the
writes listed and never called, and no body ever printed."""

import asyncio
import json

import httpx
import pytest

from asas_oracle_hcm import REFERENCE_PROBES, OracleSettings, Probe, check, load_manifest
from asas_oracle_hcm.check import main, render

from conftest import BASE, collection


def run(coro):
    return asyncio.run(coro)


def test_the_default_manifest_is_the_hcm_reference_reads(client, oracle):
    results = run(check(client))
    paths = [p.path for p in REFERENCE_PROBES]
    assert paths == ["/grades", "/departments", "/hcmBusinessUnitsLOV", "/organizations",
                     "/jobs", "/positions", "/jobFamilies", "/publicWorkers"]
    assert [r.operation for r in results] == paths and all(r.ok for r in results)
    assert not any("recruit" in p.lower() for p in paths)


def _workers(oracle):
    oracle.route("/workers", collection({"PersonId": "1"}, {"PersonId": "2"}))
    oracle.route("/workers/1/child/attachments", collection())
    oracle.route("/workers/2/child/attachments", collection(
        {"links": [{"name": "FileContents", "href": f"{BASE}/workers/2/child/attachments/K9/enclosure/FileContents"}]}))
    oracle.route("/workers/2/child/attachments/K9/enclosure/FileContents",
                 lambda rec: httpx.Response(200, content=b"%PDF-1", headers={"content-type": "application/pdf"}))


PROBES = [
    Probe(path="/workers", params={"limit": 5}, provides={"PersonId": "PersonId"}),
    Probe(path="/workers/{PersonId}/child/attachments", params={"onlyData": "false"},
          provides={"Key": "@enclosure_key"}, tries=5),
    Probe(path="/workers/{PersonId}/child/attachments/{Key}/enclosure/FileContents", binary=True),
    Probe(label="PATCH /workers/{PersonId}", call=False),
]


def test_values_chain_and_a_probe_walks_until_one_answers(client, oracle):
    _workers(oracle)
    results = run(check(client, PROBES))
    assert [r.status for r in results] == ["200", "200", "200", "not called"]
    assert results[2].detail == "6 bytes", "the second worker, the first with a file"
    assert {c.method for c in oracle.calls} == {"GET"}, "a check never writes"


def test_a_missing_value_skips_and_a_refusal_is_one_line(client, oracle):
    oracle.route("/workers", collection())  # provides nothing
    oracle.route("/grades", (500, {"Exception": "API Gateway encountered an error.\n Transport protocol not supported"}))
    results = run(check(client, [*PROBES[:2], Probe(path="/grades")]))
    assert results[0].ok and "none of 1 provided PersonId" in results[0].detail
    assert results[1].status == "skipped"
    assert results[2].status == "500" and "\n" not in results[2].detail and "Transport" in results[2].detail
    text = render(OracleSettings(base_url=BASE, username="u", password="p"), results)
    assert "Basic auth sent" in text and "1 of 2 calls answered 2xx" in text


def test_a_manifest_loads_from_json_and_refuses_unknown_fields(tmp_path):
    good = tmp_path / "probes.json"
    good.write_text(json.dumps({"probes": [{"path": "/workers", "provides": {"PersonId": "PersonId"}},
                                           {"label": "POST /workers", "call": False}]}))
    probes = load_manifest(good)
    assert probes[0].provides == {"PersonId": "PersonId"} and probes[1].call is False
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"path": "/workers", "retries": 3}]))
    with pytest.raises(ValueError):
        load_manifest(bad)


def test_the_example_recruiting_manifest_is_valid():
    import pathlib

    example = pathlib.Path(__file__).parent.parent / "examples" / "recruiting-probes.json"
    probes = load_manifest(example)
    assert sum(p.call for p in probes) == 18 and sum(not p.call for p in probes) == 2


def test_the_command_refuses_manifest_and_path_together(capsys):
    with pytest.raises(SystemExit):
        main(["--manifest", "x.json", "--path", "/grades"])
