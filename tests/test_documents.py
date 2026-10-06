"""The advanced document register: expiry tracking, the supersede chain, archive and
the analytics that make it more than a shared drive.
"""

from datetime import date, timedelta

from tests.conftest import post


async def _doc(client, h, **extra):
    body = {
        "name": "Signed purchase agreement",
        "category": "contract",
        "storage_key": "sharepoint://contracts/J00001-agreement.pdf",
        **extra,
    }
    return await post(client, h, "/api/v1/documents", body)


async def test_registering_a_document_starts_it_active(client, sold_job):
    h = sold_job["headers"]
    doc = await _doc(client, h, job_id=sold_job["job"]["id"], reference="C-2026-014")
    assert doc["status"] == "active"
    assert doc["is_critical"] is True          # a contract carries legal weight
    assert doc["reference"] == "C-2026-014"
    assert doc["revision"] == 1


async def test_expiry_flags_expiring_and_expired(client, sold_job):
    """A permit lapsing inside the window is 'expiring'; one already past is 'expired'."""
    h = sold_job["headers"]
    soon = (date.today() + timedelta(days=10)).isoformat()
    past = (date.today() - timedelta(days=3)).isoformat()

    expiring = await _doc(client, h, name="Building permit", category="permit", expires_on=soon)
    expired = await _doc(client, h, name="Old permit", category="permit", expires_on=past)

    assert expiring["is_expiring"] is True
    assert expiring["is_expired"] is False
    assert expired["is_expired"] is True
    assert expired["is_expiring"] is False

    only_expiring = (
        await client.get("/api/v1/documents", headers=h, params={"expiring_only": True})
    ).json()
    assert expiring["id"] in [d["id"] for d in only_expiring]
    assert expired["id"] not in [d["id"] for d in only_expiring]

    only_expired = (
        await client.get("/api/v1/documents", headers=h, params={"expired_only": True})
    ).json()
    assert expired["id"] in [d["id"] for d in only_expired]


async def test_supersede_keeps_the_old_version_and_carries_forward(client, sold_job):
    h = sold_job["headers"]
    doc = await _doc(client, h, reference="C-2026-014")

    next_year = (date.today() + timedelta(days=365)).isoformat()
    new = await post(
        client, h, f"/api/v1/documents/{doc['id']}/supersede",
        {"storage_key": "sharepoint://contracts/J00001-agreement-v2.pdf", "expires_on": next_year},
        expect=201,
    )
    assert new["revision"] == 2
    assert new["status"] == "active"
    assert new["reference"] == "C-2026-014"     # carried forward
    assert new["category"] == "contract"

    superseded = (
        await client.get("/api/v1/documents", headers=h, params={"status": "superseded"})
    ).json()
    old = next(d for d in superseded if d["id"] == doc["id"])
    assert old["status"] == "superseded"
    assert old["superseded_by_id"] == new["id"]

    # The register (default view) shows the new one, not the superseded one.
    register = (await client.get("/api/v1/documents", headers=h)).json()
    ids = [d["id"] for d in register]
    assert new["id"] in ids
    assert doc["id"] not in ids


async def test_archive_and_restore(client, sold_job):
    h = sold_job["headers"]
    doc = await _doc(client, h)

    archived = await post(client, h, f"/api/v1/documents/{doc['id']}/archive", None, expect=200)
    assert archived["status"] == "archived"

    register = (await client.get("/api/v1/documents", headers=h)).json()
    assert doc["id"] not in [d["id"] for d in register]

    restored = await post(client, h, f"/api/v1/documents/{doc['id']}/restore", None, expect=200)
    assert restored["status"] == "active"


async def test_search_matches_name_and_reference(client, sold_job):
    h = sold_job["headers"]
    await _doc(client, h, name="Building permit", category="permit", reference="BP-2026-778")

    by_ref = (
        await client.get("/api/v1/documents", headers=h, params={"search": "778"})
    ).json()
    assert any(d["reference"] == "BP-2026-778" for d in by_ref)


async def test_summary_counts_the_register(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    past = (date.today() - timedelta(days=1)).isoformat()

    await _doc(client, h, job_id=job_id)                                    # contract, critical
    await _doc(client, h, name="Site photo", category="photo")             # not tied, not critical
    await _doc(client, h, name="Lapsed permit", category="permit", job_id=job_id, expires_on=past)

    s = (await client.get("/api/v1/documents/summary", headers=h)).json()
    assert s["total"] == 3
    assert s["critical"] == 2                    # contract + permit
    assert s["not_tied_to_home"] == 1            # the photo
    assert s["expired"] == 1
    assert s["by_category"]["contract"] == 1
    assert s["categories_in_use"] == 3


async def test_superseded_document_cannot_be_edited(client, sold_job):
    h = sold_job["headers"]
    doc = await _doc(client, h)
    await post(
        client, h, f"/api/v1/documents/{doc['id']}/supersede",
        {"storage_key": "sharepoint://v2.pdf"}, expect=201,
    )
    r = await client.patch(f"/api/v1/documents/{doc['id']}", headers=h, json={"name": "Nope"})
    assert r.status_code == 409
