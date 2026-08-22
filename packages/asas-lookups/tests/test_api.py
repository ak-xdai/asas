"""Read + admin endpoints through build_routers (host-injected session dependency)."""


def test_list_types(client):
    resp = client.get("/lookup-types")
    assert resp.status_code == 200
    keys = {t["key"] for t in resp.json()}
    assert "gender" in keys


def test_list_values_and_etag(client):
    resp = client.get("/lookups/gender")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    assert {v["code"] for v in body["items"]} >= {"male", "female"}
    etag = resp.headers["ETag"]
    assert client.get("/lookups/gender", headers={"If-None-Match": etag}).status_code == 304


def test_get_value_bilingual(client):
    en = client.get("/lookups/gender/male").json()
    ar = client.get("/lookups/gender/male", params={"lang": "ar"}).json()
    assert en["label"] and ar["label"] and en["label"] != ar["label"]


def test_resolve(client):
    resp = client.post("/lookups/gender/resolve", json={"codes": ["male", "nope"]})
    assert resp.status_code == 200
    labels = resp.json()["labels"]
    assert labels["male"] and labels["nope"] is None


def test_unknown_type_404(client):
    assert client.get("/lookups/never-heard-of-it").status_code == 404


def test_admin_type_and_value_crud(client):
    resp = client.post(
        "/admin/lookup-types",
        json={"key": "widget", "name": "Widget", "is_open": False},
    )
    assert resp.status_code == 201, resp.text
    # duplicate key → 409
    assert (
        client.post("/admin/lookup-types", json={"key": "widget", "name": "W"}).status_code
        == 409
    )

    resp = client.post(
        "/admin/lookups/widget",
        json={
            "code": "sprocket",
            "translations": [
                {"lang": "en", "label": "Sprocket"},
                {"lang": "ar", "label": "مسنن"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    resp = client.patch(
        "/admin/lookups/widget/sprocket",
        json={"translations": [{"lang": "en", "label": "Sprocket v2"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "Sprocket v2"

    # alias add/remove + search hits the alias
    assert (
        client.post(
            "/admin/lookups/widget/sprocket/aliases", json={"alias": "cog"}
        ).status_code
        == 200
    )
    found = client.get("/lookups/widget", params={"q": "cog"}).json()
    assert found["total"] == 1
    assert (
        client.delete("/admin/lookups/widget/sprocket/aliases/cog").status_code == 200
    )

    # deprecate hides from default listing but still resolves
    assert (
        client.post("/admin/lookups/widget/sprocket/deprecate", json={}).status_code
        == 200
    )
    assert client.get("/lookups/widget").json()["total"] == 0
    assert client.get("/lookups/widget/sprocket").status_code == 200


def test_add_alias_is_idempotent(client):
    client.post("/admin/lookup-types", json={"key": "widget", "name": "Widget"})
    client.post(
        "/admin/lookups/widget",
        json={"code": "gear", "translations": [{"lang": "en", "label": "Gear"}]},
    )
    # same alias twice (second with surrounding whitespace) → one row, no 4xx
    assert (
        client.post("/admin/lookups/widget/gear/aliases", json={"alias": "cog"}).status_code
        == 200
    )
    assert (
        client.post("/admin/lookups/widget/gear/aliases", json={"alias": " cog "}).status_code
        == 200
    )
    body = client.get("/lookups/widget/gear").json()
    assert body["aliases"] == ["cog"]
    # a blank alias is rejected, not silently stored
    assert (
        client.post("/admin/lookups/widget/gear/aliases", json={"alias": "   "}).status_code
        == 422
    )


def test_create_value_dedups_aliases(client):
    client.post("/admin/lookup-types", json={"key": "widget", "name": "Widget"})
    resp = client.post(
        "/admin/lookups/widget",
        json={
            "code": "lever",
            "translations": [{"lang": "en", "label": "Lever"}],
            "aliases": [" bar ", "", "bar", "Baz", "baz"],
        },
    )
    assert resp.status_code == 201, resp.text
    assert set(resp.json()["aliases"]) == {"bar", "Baz"}


def test_merge_dedups_same_label_across_langs(client):
    client.post("/admin/lookup-types", json={"key": "widget", "name": "Widget"})
    client.post(
        "/admin/lookups/widget",
        json={"code": "keep", "translations": [{"lang": "en", "label": "Keep"}]},
    )
    # same label in two languages — one alias row on the target, not two
    client.post(
        "/admin/lookups/widget",
        json={
            "code": "dup",
            "translations": [
                {"lang": "en", "label": "Taxi"},
                {"lang": "fr", "label": "Taxi"},
            ],
        },
    )
    resp = client.post("/admin/lookups/widget/dup/merge", json={"into": "keep"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["aliases"] == ["Taxi"]


def test_remove_alias_matches_like_add(client):
    client.post("/admin/lookup-types", json={"key": "widget", "name": "Widget"})
    client.post(
        "/admin/lookups/widget",
        json={"code": "gear", "translations": [{"lang": "en", "label": "Gear"}]},
    )
    client.post("/admin/lookups/widget/gear/aliases", json={"alias": "Baz"})
    # add of 'baz' no-ops (case-insensitive present check), so remove of 'baz'
    # must hit the same row — otherwise 'Baz' is unremovable by that spelling
    resp = client.delete("/admin/lookups/widget/gear/aliases/baz")
    assert resp.status_code == 200, resp.text
    assert resp.json()["aliases"] == []
    # removing an absent alias stays a 200 no-op
    assert (
        client.delete("/admin/lookups/widget/gear/aliases/baz").status_code == 200
    )


def test_list_etag_varies_with_query_shape(client):
    """An ETag names a representation: page 2 or a filtered list is a
    different body than page 1 of the same type version, so a 304 for one
    must not be served for the other."""
    r1 = client.get("/lookups/gender", params={"page_size": 1})
    etag = r1.headers["ETag"]
    assert (
        client.get(
            "/lookups/gender", params={"page_size": 1}, headers={"If-None-Match": etag}
        ).status_code
        == 304
    )
    r2 = client.get(
        "/lookups/gender",
        params={"page_size": 1, "page": 2},
        headers={"If-None-Match": etag},
    )
    assert r2.status_code == 200  # different page: full body, not 304
    r3 = client.get(
        "/lookups/gender", params={"q": "zzzz"}, headers={"If-None-Match": etag}
    )
    assert r3.status_code == 200 and r3.json()["total"] == 0
    r4 = client.get(
        "/lookups/gender", params={"active": "false"}, headers={"If-None-Match": etag}
    )
    assert r4.status_code == 200
