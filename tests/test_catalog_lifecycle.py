"""Editing, searching and retiring catalog records.

Before this, a cost code could be created and listed and nothing else. A typo in
one was permanent, and a trade you stopped using stayed in every picker forever.

The rules worth pinning down are the two refusals. Both exist because the
alternative is silently wrong rather than loudly inconvenient:

  * a cost code still carrying budget or commitment cannot be archived, because
    hiding it from pickers while money keeps landing on it is the worst of both
  * a trade holding open purchase orders cannot be archived, because the people
    chasing those orders would lose sight of them
"""

from decimal import Decimal

from tests.conftest import post

# ============================================================== editing =====

async def test_a_cost_code_can_be_corrected(client, builder):
    """A mistyped cost code is the most expensive typo here - everything
    reconciles through it, so it must be fixable."""
    h = builder["headers"]
    code = await post(client, h, "/api/v1/cost-codes", {"code": "06-999", "name": "Typo"})

    resp = await client.patch(
        f"/api/v1/cost-codes/{code['id']}",
        headers=h,
        json={"code": "06-900", "name": "Rough carpentry"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "06-900"
    assert body["name"] == "Rough carpentry"


async def test_a_partial_update_leaves_the_rest_alone(client, builder):
    """PATCH says what changed, not what everything is."""
    h = builder["headers"]
    code = await post(
        client, h, "/api/v1/cost-codes",
        {"code": "07-100", "name": "Roofing", "division": "Envelope"},
    )

    resp = await client.patch(
        f"/api/v1/cost-codes/{code['id']}", headers=h, json={"name": "Roofing and flashings"}
    )
    body = resp.json()
    assert body["name"] == "Roofing and flashings"
    assert body["code"] == "07-100"          # untouched
    assert body["division"] == "Envelope"    # untouched


async def test_editing_into_an_existing_code_is_refused(client, builder):
    h = builder["headers"]
    await post(client, h, "/api/v1/cost-codes", {"code": "08-100", "name": "Doors"})
    second = await post(client, h, "/api/v1/cost-codes", {"code": "08-200", "name": "Windows"})

    resp = await client.patch(
        f"/api/v1/cost-codes/{second['id']}", headers=h, json={"code": "08-100"}
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


async def test_a_record_may_keep_its_own_code_when_edited(client, builder):
    """Uniqueness must not trip over the record being edited."""
    h = builder["headers"]
    code = await post(client, h, "/api/v1/cost-codes", {"code": "09-100", "name": "Plaster"})

    resp = await client.patch(
        f"/api/v1/cost-codes/{code['id']}",
        headers=h,
        json={"code": "09-100", "name": "Plaster and render"},
    )
    assert resp.status_code == 200


async def test_a_trades_contact_details_can_be_corrected(client, builder):
    """Contact people change. The record should too."""
    h = builder["headers"]
    v = await post(
        client, h, "/api/v1/vendors",
        {"code": "ROOF", "name": "Summit Roofing", "vendor_type": "trade"},
    )

    resp = await client.patch(
        f"/api/v1/vendors/{v['id']}",
        headers=h,
        json={"contact_name": "Dale Morrow", "phone": "01202 555111"},
    )
    assert resp.status_code == 200
    assert resp.json()["contact_name"] == "Dale Morrow"


# ============================================================ archiving =====

async def test_an_unused_cost_code_can_be_archived(client, builder):
    h = builder["headers"]
    code = await post(client, h, "/api/v1/cost-codes", {"code": "99-100", "name": "Unused"})

    resp = await post(
        client, h, f"/api/v1/cost-codes/{code['id']}/archive",
        {"reason": "Superseded by 99-200"}, expect=200,
    )
    assert resp["is_archived"] is True
    assert resp["archive_reason"] == "Superseded by 99-200"


async def test_archived_records_drop_out_of_the_list(client, builder):
    """The whole point: it stops appearing where somebody would pick it."""
    h = builder["headers"]
    code = await post(client, h, "/api/v1/cost-codes", {"code": "99-300", "name": "Retired"})
    await post(client, h, f"/api/v1/cost-codes/{code['id']}/archive", {}, expect=200)

    codes = (await client.get("/api/v1/cost-codes", headers=h)).json()
    assert code["id"] not in [c["id"] for c in codes]


async def test_an_archived_record_can_be_restored(client, builder):
    h = builder["headers"]
    code = await post(client, h, "/api/v1/cost-codes", {"code": "99-400", "name": "Oops"})
    await post(client, h, f"/api/v1/cost-codes/{code['id']}/archive", {}, expect=200)

    restored = await post(
        client, h, f"/api/v1/cost-codes/{code['id']}/restore", None, expect=200
    )
    assert restored["is_archived"] is False
    assert restored["archive_reason"] is None

    codes = (await client.get("/api/v1/cost-codes", headers=h)).json()
    assert code["id"] in [c["id"] for c in codes]


async def test_a_cost_code_carrying_money_cannot_be_archived(client, sold_job):
    """Hiding a code that budget lines still point at is the worst of both."""
    h = sold_job["headers"]
    framing = sold_job["framing"]["id"]

    resp = await client.post(
        f"/api/v1/cost-codes/{framing}/archive", headers=h, json={"reason": "tidying up"}
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "still used by" in detail
    assert "budget line" in detail


async def test_a_trade_with_open_commitments_cannot_be_archived(client, sold_job):
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": vendor_id,
            "lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "1000.00"}],
        },
    )
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)

    resp = await client.post(f"/api/v1/vendors/{vendor_id}/archive", headers=h, json={})
    assert resp.status_code == 409
    assert "open purchase order" in resp.json()["detail"]


async def test_a_trade_with_only_closed_history_can_be_archived(client, sold_job):
    """History must survive archiving - that is the difference from deleting."""
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": vendor_id,
            "lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "1000.00"}],
        },
    )
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/void", None, expect=200)

    archived = await post(
        client, h, f"/api/v1/vendors/{vendor_id}/archive", {"reason": "No longer trading"},
        expect=200,
    )
    assert archived["is_archived"] is True

    # The purchase order is still there, still pointing at them.
    still = (await client.get(f"/api/v1/purchase-orders/{po['id']}", headers=h)).json()
    assert still["vendor_id"] == vendor_id


async def test_archiving_twice_is_refused(client, builder):
    h = builder["headers"]
    code = await post(client, h, "/api/v1/cost-codes", {"code": "99-500", "name": "Once"})
    await post(client, h, f"/api/v1/cost-codes/{code['id']}/archive", {}, expect=200)

    again = await client.post(f"/api/v1/cost-codes/{code['id']}/archive", headers=h, json={})
    assert again.status_code == 409


# ============================================== searching and paging =====

async def test_search_matches_code_and_name(client, builder):
    h = builder["headers"]
    await post(client, h, "/api/v1/cost-codes", {"code": "22-100", "name": "Sanitaryware"})
    await post(client, h, "/api/v1/cost-codes", {"code": "22-200", "name": "Hot water"})

    by_name = (await client.get("/api/v1/cost-codes/search?search=sanitary", headers=h)).json()
    assert by_name["total"] == 1
    assert by_name["items"][0]["code"] == "22-100"

    by_code = (await client.get("/api/v1/cost-codes/search?search=22-", headers=h)).json()
    assert by_code["total"] == 2


async def test_search_is_case_insensitive(client, builder):
    h = builder["headers"]
    await post(client, h, "/api/v1/cost-codes", {"code": "23-100", "name": "Ventilation"})

    hit = (await client.get("/api/v1/cost-codes/search?search=VENTIL", headers=h)).json()
    assert hit["total"] == 1


async def test_paging_returns_a_slice_and_the_full_total(client, builder):
    """The total is the match count, not the page size - a UI needs both."""
    h = builder["headers"]
    for i in range(7):
        await post(client, h, "/api/v1/cost-codes", {"code": f"31-{i:03d}", "name": f"Page {i}"})

    first = (
        await client.get("/api/v1/cost-codes/search?search=31-&page=1&size=3", headers=h)
    ).json()
    assert len(first["items"]) == 3
    assert first["total"] == 7
    assert first["page"] == 1

    third = (
        await client.get("/api/v1/cost-codes/search?search=31-&page=3&size=3", headers=h)
    ).json()
    assert len(third["items"]) == 1


async def test_archived_records_are_excluded_from_search_unless_asked_for(client, builder):
    h = builder["headers"]
    code = await post(client, h, "/api/v1/cost-codes", {"code": "41-100", "name": "Hidden"})
    await post(client, h, f"/api/v1/cost-codes/{code['id']}/archive", {}, expect=200)

    hidden = (await client.get("/api/v1/cost-codes/search?search=41-100", headers=h)).json()
    assert hidden["total"] == 0

    shown = (
        await client.get(
            "/api/v1/cost-codes/search?search=41-100&include_archived=true", headers=h
        )
    ).json()
    assert shown["total"] == 1


async def test_search_is_scoped_to_the_tenant(client, builder):
    """The obvious one, asserted because paging rewrote the query."""
    h = builder["headers"]
    await post(client, h, "/api/v1/cost-codes", {"code": "51-100", "name": "Ours"})

    signup = await client.post(
        "/api/v1/auth/register",
        json={
            "company_name": "Rival",
            "full_name": "Owner",
            "email": "rival3@rival.example.com",
            "password": "supersecret123",
        },
    )
    other = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    theirs = (await client.get("/api/v1/cost-codes/search?search=51-100", headers=other)).json()
    assert theirs["total"] == 0


# ================================================================ audit =====

async def test_who_created_and_who_edited_is_recorded(client, builder):
    """When a number looks wrong, "who touched this" is the first question."""
    h = builder["headers"]
    code = await post(client, h, "/api/v1/cost-codes", {"code": "61-100", "name": "Audited"})
    assert code["created_by_id"] is not None
    assert code["updated_by_id"] is None      # not edited yet

    edited = (
        await client.patch(
            f"/api/v1/cost-codes/{code['id']}", headers=h, json={"name": "Changed"}
        )
    ).json()
    assert edited["updated_by_id"] == code["created_by_id"]


async def test_money_still_reconciles_after_a_code_is_renamed(client, sold_job):
    """Renaming must not orphan the budget lines pointing at it."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    before = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()

    await client.patch(
        f"/api/v1/cost-codes/{sold_job['framing']['id']}",
        headers=h,
        json={"name": "Carpentry, structural"},
    )

    after = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(after["revised_total"]) == Decimal(before["revised_total"])
    renamed = next(
        line for line in after["lines"] if line["cost_code_id"] == sold_job["framing"]["id"]
    )
    assert renamed["name"] == "Carpentry, structural"
