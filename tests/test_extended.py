"""Bidding, field operations, marketing, design studio, e-signature, land, accounting."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.conftest import post

# --- Bidding ------------------------------------------------------------------


async def test_awarding_a_bid_creates_the_purchase_order_at_the_bid_price(client, sold_job):
    """The point of bidding: the price the trade quoted is the price committed."""
    h = sold_job["headers"]

    pkg = await post(
        client, h, "/api/v1/bid-packages",
        {
            "name": "Electrical rough-in, Plan 3",
            "cost_code_id": sold_job["electrical"]["id"],
            "plan_id": sold_job["plan"]["id"],
        },
    )
    assert pkg["status"] == "draft"

    pkg = await post(
        client, h, f"/api/v1/bid-packages/{pkg['id']}/invite",
        {"vendor_ids": [sold_job["vendor"]["id"]]},
        expect=200,
    )
    assert pkg["status"] == "out_for_bid"
    assert len(pkg["bids"]) == 1

    bid = pkg["bids"][0]
    await post(
        client, h, f"/api/v1/bid-packages/{pkg['id']}/bids/{bid['id']}/submit",
        {"amount": "19500.00"}, expect=200,
    )

    result = await post(
        client, h,
        f"/api/v1/bid-packages/{pkg['id']}/bids/{bid['id']}/award"
        f"?job_id={sold_job['job']['id']}",
        None, expect=200,
    )
    assert Decimal(result["amount"]) == Decimal("19500.00")
    assert result["po_number"] is not None

    pos = (
        await client.get(
            f"/api/v1/purchase-orders?job_id={sold_job['job']['id']}", headers=h
        )
    ).json()
    assert len(pos) == 1
    assert Decimal(pos[0]["total"]) == Decimal("19500.00")


async def test_a_stale_bid_cannot_be_awarded(client, sold_job):
    """A bid priced against old drawings must not be quietly committed to."""
    h = sold_job["headers"]

    pkg = await post(
        client, h, "/api/v1/bid-packages",
        {"name": "Framing", "cost_code_id": sold_job["framing"]["id"]},
    )
    pkg = await post(
        client, h, f"/api/v1/bid-packages/{pkg['id']}/invite",
        {"vendor_ids": [sold_job["vendor"]["id"]]}, expect=200,
    )
    bid = pkg["bids"][0]
    await post(
        client, h, f"/api/v1/bid-packages/{pkg['id']}/bids/{bid['id']}/submit",
        {"amount": "50000.00"}, expect=200,
    )

    # The builder revises the drawings after the price came in.
    await post(client, h, f"/api/v1/bid-packages/{pkg['id']}/revise", None, expect=200)

    resp = await client.post(
        f"/api/v1/bid-packages/{pkg['id']}/bids/{bid['id']}/award", headers=h
    )
    assert resp.status_code == 409
    assert "revision" in resp.json()["detail"]


# --- Field operations ---------------------------------------------------------


async def test_failed_inspection_requires_a_reason(client, sold_job):
    h = sold_job["headers"]
    insp = await post(
        client, h, "/api/v1/field/inspections",
        {"job_id": sold_job["job"]["id"], "name": "Foundation inspection"},
    )

    resp = await client.post(
        f"/api/v1/field/inspections/{insp['id']}/result",
        headers=h,
        json={"result": "failed"},
    )
    assert resp.status_code == 409

    passed = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed", "failure_reason": "Rebar spacing out of tolerance"},
        expect=200,
    )
    assert passed["result"] == "failed"
    assert passed["completed_date"] is not None


async def test_punch_item_lifecycle(client, sold_job):
    h = sold_job["headers"]
    item = await post(
        client, h, "/api/v1/field/punch-items",
        {
            "job_id": sold_job["job"]["id"],
            "description": "Scuff on the hallway wall",
            "location": "Upstairs hallway",
            "vendor_id": sold_job["vendor"]["id"],
            "from_walkthrough": True,
        },
    )
    # Raised against a trade, so it is theirs from the start.
    assert item["status"] == "assigned"
    assert item["ball_in_court"] == "trade"
    assert item["from_walkthrough"] is True

    # It cannot be closed by setting the status. It goes through the lifecycle, so
    # that somebody has looked at the fix before the defect stops counting against
    # the handover.
    refused = await client.patch(
        f"/api/v1/field/punch-items/{item['id']}", headers=h, json={"status": "complete"}
    )
    assert refused.status_code == 422

    await post(client, h, f"/api/v1/field/punch-items/{item['id']}/submit", None, expect=200)
    done = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/verify", None, expect=200
    )
    assert done["status"] == "complete"
    assert done["completed_date"] is not None
    assert done["verified_by_id"] is not None


async def test_daily_log_records_the_day(client, sold_job):
    h = sold_job["headers"]
    log = await post(
        client, h, "/api/v1/field/daily-logs",
        {
            "job_id": sold_job["job"]["id"],
            "weather": "Rain, 12C",
            "crew_count": 6,
            "delays": "Half day lost to rain",
        },
    )
    assert log["log_date"] is not None
    assert log["crew_count"] == 6


# --- Design studio and e-signature --------------------------------------------


async def test_appointment_and_signature_flow(client, sold_job):
    h = sold_job["headers"]

    # A date is required now: an appointment with no time is a note, and the
    # studio list showed a row reading "Not set" for exactly this reason.
    appt = await post(
        client, h, "/api/v1/appointments",
        {
            "job_id": sold_job["job"]["id"],
            "scheduled_for": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
            "location": "Design studio",
            "duration_minutes": 180,
        },
    )
    assert appt["status"] == "scheduled"
    assert appt["buyer_id"] is not None  # picked up from the contract

    done = await post(
        client, h, f"/api/v1/appointments/{appt['id']}/complete", {}, expect=200
    )
    assert done["status"] == "complete"

    sig = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": sold_job["job"]["id"],
            "document_kind": "addendum",
            "document_name": "Design selections addendum",
            "signer_name": "Dana Reyes",
            "signer_email": "dana@example.com",
        },
    )
    assert sig["status"] == "draft"

    sent = await post(client, h, f"/api/v1/signatures/{sig['id']}/send", None, expect=200)
    assert sent["status"] == "sent"

    signed = await post(
        client, h, f"/api/v1/signatures/{sig['id']}/mark-signed", None, expect=200
    )
    assert signed["status"] == "signed"
    assert signed["signed_at"] is not None


# --- Marketing and surveys ----------------------------------------------------


async def test_campaign_attribution_links_to_leads(client, builder):
    h = builder["headers"]

    await post(
        client, h, "/api/v1/campaigns",
        {"code": "spring-parade", "name": "Spring Parade of Homes", "subject": "Come see us"},
    )
    # The list is paged now, and sending is two steps rather than one: launching
    # it is a decision, and the delivery numbers arrive afterwards from a provider.
    campaigns = (await client.get("/api/v1/campaigns", headers=h)).json()["items"]
    cid = campaigns[0]["id"]

    await post(client, h, f"/api/v1/campaigns/{cid}/launch", {}, expect=200)
    sent = await post(
        client, h, f"/api/v1/campaigns/{cid}/delivery",
        {"sent_count": 1000, "opened_count": 240, "clicked_count": 60}, expect=200,
    )
    assert sent["status"] == "running"
    assert sent["open_rate"] == 0.24

    # Two leads attributed to that campaign, one converted. The conversion walks
    # through qualification rather than jumping to it: a lead cannot become a
    # buyer without somebody first checking they can pay.
    for name in ("Ana", "Ben"):
        lead = await post(
            client, h, "/api/v1/leads",
            {
                "first_name": name,
                "last_name": "Test",
                "email": f"{name.lower()}@example.com",
                "campaign": "spring-parade",
            },
        )
    for step in ("contacted", "qualified"):
        await post(client, h, f"/api/v1/leads/{lead['id']}/status", {"status": step}, expect=200)
    await post(client, h, f"/api/v1/leads/{lead['id']}/convert", None, expect=200)

    report = (await client.get("/api/v1/reports/campaign-performance", headers=h)).json()
    row = report["campaigns"][0]
    assert row["code"] == "spring-parade"
    assert row["leads"] == 2
    assert row["converted"] == 1


async def test_survey_nps_banding(client, sold_job):
    h = sold_job["headers"]

    s1 = await post(client, h, "/api/v1/surveys", {"job_id": sold_job["job"]["id"]})
    await post(
        client, h, f"/api/v1/surveys/{s1['id']}/respond",
        {"would_recommend": 10, "rating_construction": 5}, expect=200,
    )

    survey = (await client.get("/api/v1/surveys", headers=h)).json()[0]
    assert survey["nps_band"] == "promoter"

    report = (await client.get("/api/v1/reports/satisfaction", headers=h)).json()
    assert report["responses"] == 1
    assert report["nps"] == 100
    assert report["promoters"] == 1


# Land development moved to tests/test_land.py when it grew planning and obligations.


# Accounting moved to tests/test_accounting.py when the export became batched journals.
