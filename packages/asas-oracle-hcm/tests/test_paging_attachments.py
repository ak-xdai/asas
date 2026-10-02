"""Fusion's paging limits and attachments, on any resource."""

import asyncio

import httpx
import pytest

from asas_oracle_hcm import (
    DEFAULT_OFFSET_CEILING,
    attachments,
    capped_page,
    download_enclosure,
    enclosure_key,
)

from conftest import collection


def run(coro):
    return asyncio.run(coro)


def test_a_page_is_clamped_to_the_page_cap(client, oracle):
    oracle.route("/workers", collection({"PersonId": "1"}, has_more=True))
    page = run(capped_page(client, "/workers", limit=5000))
    assert page.limit == 200 and page.has_more and not page.at_ceiling
    assert oracle.calls[0].params["limit"] == "200"


def test_without_a_ceiling_paging_is_only_clamped(client, oracle):
    oracle.route("/workers", collection({"PersonId": "1"}, has_more=True))
    page = run(capped_page(client, "/workers", limit=50, offset=50_000))
    assert page.limit == 50 and page.has_more and not page.at_ceiling
    assert oracle.calls[0].params["offset"] == "50000"


def test_the_last_page_is_trimmed_to_the_ceiling(client, oracle):
    oracle.route("/documentRecords", collection({"c": 1}, has_more=True))
    page = run(capped_page(client, "/documentRecords", limit=200, offset=9_900, offset_ceiling=DEFAULT_OFFSET_CEILING))
    assert page.limit == 100 and page.at_ceiling and page.has_more is False
    assert oracle.calls[0].params["limit"] == "100"


def test_past_the_ceiling_oracle_is_not_asked(client, oracle):
    page = run(capped_page(client, "/documentRecords", offset=DEFAULT_OFFSET_CEILING, offset_ceiling=DEFAULT_OFFSET_CEILING))
    assert page.items == [] and page.at_ceiling and not page.has_more
    assert oracle.calls == []


def test_params_ride_along_and_limit_offset_win(client, oracle):
    run(capped_page(client, "/workers", limit=10, offset=20, params={"q": "PersonNumber='7'", "limit": 999}))
    params = oracle.calls[0].params
    assert params["q"] == "PersonNumber='7'" and params["limit"] == "10" and params["offset"] == "20"


def test_a_page_cap_below_one_is_refused(client):
    with pytest.raises(ValueError):
        run(capped_page(client, "/workers", page_cap=0))


def test_attachments_keep_the_links_filter_by_category_and_skip_the_cache(client, oracle):
    row = {"FileName": "contract.pdf", "links": [{
        "name": "FileContents",
        "href": "https://pod/x/workers/300/child/attachments/00AB12/enclosure/FileContents",
    }]}
    oracle.route("/workers/300/child/attachments", collection(row))
    rows = run(attachments(client, "workers/300", category="CONTRACT"))
    run(attachments(client, "/workers/300/", category="CONTRACT"))
    params = oracle.calls[0].params
    assert params["onlyData"] == "false" and params["q"] == "CategoryName='CONTRACT'"
    assert enclosure_key(rows[0]) == "00AB12"
    assert len(oracle.calls_to("/workers/300/child/attachments")) == 2, "never cached"


def test_enclosure_key_is_empty_without_a_file_link():
    assert enclosure_key({}) == ""
    assert enclosure_key({"links": [{"name": "self", "href": "x"}]}) == ""
    assert enclosure_key({"links": [{"name": "FileContents", "href": "https://pod/other"}]}) == ""


def test_download_enclosure_hits_the_records_enclosure(client, oracle):
    oracle.route(
        "/documentRecords/9/child/attachments/00AB12/enclosure/FileContents",
        lambda rec: httpx.Response(200, content=b"bytes"),
    )
    content, _ = run(download_enclosure(client, "/documentRecords/9", "00AB12"))
    assert content == b"bytes"
