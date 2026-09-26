"""Recruiting helpers that respect Oracle's own caps."""

import asyncio

import httpx

from asas_oracle_hcm import (
    CANDIDATE_OFFSET_CEILING,
    candidate_attachments,
    candidate_page,
    download_attachment,
    enclosure_key,
)

from conftest import collection


def run(coro):
    return asyncio.run(coro)


def test_candidate_page_clamps_the_page_size(client, oracle):
    oracle.route("/recruitingCandidates", collection({"CandidateNumber": "1"}, has_more=True))
    page = run(candidate_page(client, limit=5000, offset=0))
    assert page.limit == 200 and page.has_more and not page.at_ceiling
    assert oracle.calls[0].params["limit"] == "200"


def test_the_last_page_is_trimmed_to_the_ceiling(client, oracle):
    oracle.route("/recruitingCandidates", collection({"c": 1}, has_more=True))
    page = run(candidate_page(client, limit=200, offset=9_900))
    assert page.limit == 100 and page.at_ceiling and page.has_more is False
    assert oracle.calls[0].params["limit"] == "100"


def test_past_the_ceiling_oracle_is_not_asked(client, oracle):
    page = run(candidate_page(client, offset=CANDIDATE_OFFSET_CEILING))
    assert page.items == [] and page.at_ceiling and not page.has_more
    assert oracle.calls == []


def test_attachments_keep_the_links_and_filter_by_category(client, oracle):
    row = {"FileName": "cv.pdf", "links": [{
        "name": "FileContents",
        "href": "https://pod/x/recruitingCandidates/1/child/attachments/00AB12/enclosure/FileContents",
    }]}
    oracle.route("/recruitingCandidates/1/child/attachments", collection(row))
    rows = run(candidate_attachments(client, "1", category="IRC_CANDIDATE_RESUME"))
    params = oracle.calls[0].params
    assert params["onlyData"] == "false" and params["q"] == "CategoryName='IRC_CANDIDATE_RESUME'"
    assert enclosure_key(rows[0]) == "00AB12"


def test_enclosure_key_is_empty_without_a_file_link():
    assert enclosure_key({}) == ""
    assert enclosure_key({"links": [{"name": "self", "href": "x"}]}) == ""
    assert enclosure_key({"links": [{"name": "FileContents", "href": "https://pod/other"}]}) == ""


def test_download_attachment_hits_the_enclosure(client, oracle):
    oracle.route(
        "/recruitingCandidates/1/child/attachments/00AB12/enclosure/FileContents",
        lambda rec: httpx.Response(200, content=b"bytes"),
    )
    content, _ = run(download_attachment(client, "1", "00AB12"))
    assert content == b"bytes"
