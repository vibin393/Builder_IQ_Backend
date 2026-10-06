"""The advanced drawings module: spec supersede lifecycle, transmittal purpose,
procurement of receipt (overdue transmittals), set editing and the register summary.

The drawing-set revise/supersede flow has its own test in test_procore.py; this covers
what grew around it.
"""

from datetime import date, timedelta

from tests.conftest import post


async def _set_with_sheet(client, h, plan_id):
    ds = await post(
        client, h, "/api/v1/drawing-sets",
        {"name": "The Maple — construction issue", "plan_id": plan_id},
    )
    ds = await post(
        client, h, f"/api/v1/drawing-sets/{ds['id']}/drawings",
        {"sheet_number": "A-101", "title": "Ground floor plan", "discipline": "architectural"},
        expect=201,
    )
    return ds


async def test_a_set_with_no_sheets_cannot_be_issued(client, sold_job):
    h = sold_job["headers"]
    ds = await post(client, h, "/api/v1/drawing-sets", {"name": "Empty set"})
    refused = await client.post(f"/api/v1/drawing-sets/{ds['id']}/issue", headers=h)
    assert refused.status_code == 409
    assert "no sheets" in refused.json()["detail"]


async def test_edit_and_remove_sheet(client, sold_job):
    h = sold_job["headers"]
    ds = await _set_with_sheet(client, h, sold_job["plan"]["id"])
    sheet_id = ds["drawings"][0]["id"]

    r = await client.patch(
        f"/api/v1/drawing-sets/{ds['id']}/drawings/{sheet_id}",
        headers=h, json={"discipline": "structural"},
    )
    assert r.status_code == 200
    assert r.json()["drawings"][0]["discipline"] == "structural"

    r = await client.delete(
        f"/api/v1/drawing-sets/{ds['id']}/drawings/{sheet_id}", headers=h
    )
    assert r.status_code == 200
    assert r.json()["sheet_count"] == 0


async def test_issued_set_cannot_be_edited(client, sold_job):
    h = sold_job["headers"]
    ds = await _set_with_sheet(client, h, sold_job["plan"]["id"])
    await post(client, h, f"/api/v1/drawing-sets/{ds['id']}/issue", None, expect=200)

    r = await client.patch(
        f"/api/v1/drawing-sets/{ds['id']}", headers=h, json={"name": "Renamed"}
    )
    assert r.status_code == 409
    assert "superseded, never changed" in r.json()["detail"]


async def test_void_withdraws_a_draft_set(client, sold_job):
    h = sold_job["headers"]
    ds = await post(client, h, "/api/v1/drawing-sets", {"name": "Abandoned"})
    voided = await post(client, h, f"/api/v1/drawing-sets/{ds['id']}/void", None, expect=200)
    assert voided["status"] == "void"


async def test_specification_supersede_lifecycle(client, sold_job):
    """A spec revises like a drawing set: the old one is kept and linked to its
    replacement, and only the new one is current."""
    h = sold_job["headers"]
    spec = await post(
        client, h, "/api/v1/specifications",
        {"section": "09 30 00", "title": "Tiling", "body": "Ceramic, matte.",
         "cost_code_id": sold_job["framing"]["id"]},
    )
    assert spec["status"] == "issued"
    assert spec["division"] == "09"
    assert spec["is_current"] is True

    revised = await post(
        client, h, f"/api/v1/specifications/{spec['id']}/revise",
        {"body": "Porcelain, matte, 300x600."}, expect=200,
    )
    assert revised["revision"] == 2
    assert revised["status"] == "issued"
    assert revised["title"] == "Tiling"                # carried forward
    assert revised["body"] == "Porcelain, matte, 300x600."

    old = (await client.get(f"/api/v1/specifications/{spec['id']}", headers=h)).json()
    assert old["status"] == "superseded"
    assert old["is_current"] is False
    assert old["superseded_by_id"] == revised["id"]

    current = (
        await client.get("/api/v1/specifications", headers=h, params={"current_only": True})
    ).json()
    assert spec["id"] not in [s["id"] for s in current]
    assert revised["id"] in [s["id"] for s in current]


async def test_draft_spec_can_be_issued(client, sold_job):
    h = sold_job["headers"]
    spec = await post(
        client, h, "/api/v1/specifications",
        {"section": "22 40 00", "title": "Sanitaryware", "issue_now": False},
    )
    assert spec["status"] == "draft"
    issued = await post(client, h, f"/api/v1/specifications/{spec['id']}/issue", None, expect=200)
    assert issued["status"] == "issued"


async def test_transmittal_carries_a_purpose_and_flags_overdue(client, sold_job):
    h = sold_job["headers"]
    ds = await _set_with_sheet(client, h, sold_job["plan"]["id"])
    await post(client, h, f"/api/v1/drawing-sets/{ds['id']}/issue", None, expect=200)

    past = (date.today() - timedelta(days=2)).isoformat()
    t = await post(
        client, h, "/api/v1/transmittals",
        {
            "subject": "Issued construction set",
            "purpose": "for_construction",
            "drawing_set_id": ds["id"],
            "vendor_ids": [sold_job["vendor"]["id"]],
            "response_due": past,
        },
    )
    assert t["purpose"] == "for_construction"
    assert t["outstanding"] == 1
    assert t["is_overdue"] is True

    overdue = (
        await client.get("/api/v1/transmittals", headers=h, params={"overdue_only": True})
    ).json()
    assert t["id"] in [x["id"] for x in overdue]

    # Acknowledging clears the overdue flag.
    ack = await post(
        client, h, f"/api/v1/transmittals/{t['id']}/acknowledge/{sold_job['vendor']['id']}",
        None, expect=200,
    )
    assert ack["is_overdue"] is False
    assert ack["acknowledged_count"] == 1


async def test_summary_counts_the_register(client, sold_job):
    h = sold_job["headers"]
    ds = await _set_with_sheet(client, h, sold_job["plan"]["id"])
    await post(client, h, f"/api/v1/drawing-sets/{ds['id']}/issue", None, expect=200)
    await post(client, h, "/api/v1/specifications", {"section": "09 30 00", "title": "Tiling"})

    s = (await client.get("/api/v1/drawings/summary", headers=h)).json()
    assert s["live_sets"] == 1
    assert s["total_sheets"] == 1
    assert s["specifications"] == 1
    assert s["by_discipline"].get("architectural") == 1
