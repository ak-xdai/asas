"""Attachments on any Fusion record that has them.

Fusion exposes a record's files the same way on every resource that carries
them: a ``child/attachments`` collection under the record, and each file's
bytes at ``.../attachments/<key>/enclosure/FileContents``. A worker's
documents, a document record, a candidate's CV: one shape, so one helper.

Two traps, both Fusion's:

* **The key exists only in ``links``.** The enclosure key is a long hex string
  that is NOT ``AttachedDocumentId``, and it appears only inside the row's
  HATEOAS ``links``, which ``onlyData=true`` strips. So the listing is the one
  read that must ask for ``onlyData=false`` (:func:`attachments` does).
* **Rows carry a signed ``FileUrl``.** It downloads the file without any
  credential, so never forward it to a browser; serve the bytes yourself
  through :func:`download_enclosure`.
"""

from __future__ import annotations

from typing import Any

from .client import OracleFusionClient
from .query import eq


def _record(record_path: str) -> str:
    return "/" + record_path.strip("/")


async def attachments(
    client: OracleFusionClient,
    record_path: str,
    *,
    category: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """The attachment rows of one record, newest first, WITH their ``links``.

    ``record_path`` is the record itself, for example ``/workers/<id>`` or
    ``/documentRecords/<id>``. ``category`` filters on Fusion's
    ``CategoryName`` (its values are configured per instance and per product,
    so the caller names them). Never cached: an attachment list is read to see
    what is there now."""
    params: dict[str, Any] = {
        "orderBy": "LastUpdateDate:desc",
        "limit": max(1, min(limit, 200)),
        "onlyData": "false",
    }
    if category:
        params["q"] = eq("CategoryName", category)
    rows, _, _ = await client.get_collection(
        f"{_record(record_path)}/child/attachments", params, use_cache=False
    )
    return rows


def enclosure_key(row: dict[str, Any]) -> str:
    """The FileContents enclosure key in an attachment row's ``links``, or
    ``""`` when the row has none (or the links were stripped)."""
    links = row.get("links")
    if not isinstance(links, list):
        return ""
    for link in links:
        if not isinstance(link, dict) or link.get("name") != "FileContents":
            continue
        href = str(link.get("href") or "")
        marker = "/attachments/"
        start = href.find(marker)
        if start == -1:
            continue
        key, _, tail = href[start + len(marker) :].partition("/enclosure/")
        if key and tail:
            return key
    return ""


async def download_enclosure(
    client: OracleFusionClient, record_path: str, key: str
) -> tuple[bytes, str]:
    """An attachment's bytes and Fusion's content type.

    Fusion serves every enclosure as ``application/octet-stream`` whatever it
    is, so prefer the row's own ``UploadedFileContentType``. Check first that
    ``key`` came from :func:`attachments` for the SAME record, or a key can be
    replayed against another record."""
    return await client.get_bytes(
        f"{_record(record_path)}/child/attachments/{key}/enclosure/FileContents"
    )
