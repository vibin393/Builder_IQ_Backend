"""Advanced warranty: the claim lifecycle, the SLA clock, coverage, appointments,
notes, back-charges and the manager's dashboard.

These assert specific numbers, not just status codes: the SLA due date is the exact
day, the coverage decision is the exact enum, and the callback report counts the exact
back-charges - because a warranty screen that shows the wrong figure is worse than one
that shows nothing.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.models.warranty import WarrantyCategory, WarrantyRequest
from app.services import warranty as ws
from tests.conftest import post

# --- pure coverage / SLA rules (no HTTP) --------------------------------------


def test_sla_due_dates_step_down_by_priority():
    from app.models.warranty import Priority

    today = date(2026, 1, 1)
    assert ws.due_date_for(today, Priority.EMERGENCY) == today + timedelta(days=1)
    assert ws.due_date_for(today, Priority.HIGH) == today + timedelta(days=3)
    assert ws.due_date_for(today, Priority.NORMAL) == today + timedelta(days=14)
    assert ws.due_date_for(today, Priority.LOW) == today + timedelta(days=30)


def test_coverage_windows_follow_nhbc_2_and_10_years():
    closing = date(2024, 6, 1)
    w = ws.coverage_windows(closing)
    assert w["defects_until"] == date(2026, 6, 1)      # 2-year workmanship
    assert w["structural_until"] == date(2034, 6, 1)   # 10-year structural


def test_coverage_suggestion_splits_structural_from_workmanship():
    closing = date(2020, 1, 1)
    on = date(2026, 1, 1)  # 6 years later: past defects, inside structural
    # A plastering (interior) defect this far out is out of the 2-year cover.
    assert ws.suggest_coverage(WarrantyCategory.INTERIOR, closing, on) == "out_of_warranty"
    # A structural defect is still inside the 10-year cover.
    assert ws.suggest_coverage(WarrantyCategory.STRUCTURAL, closing, on) == "in_warranty"


def test_coverage_is_pending_without_a_completion_date():
    assert ws.suggest_coverage(WarrantyCategory.PLUMBING, None) == "pending"


# --- full lifecycle over HTTP -------------------------------------------------


async def test_claim_lifecycle_acknowledge_assess_dispatch_resolve_close(client, sold_job):
    h = sold_job["headers"]

    req = await post(
        client, h, "/api/v1/warranty/requests",
        {
            "job_id": sold_job["job"]["id"],
            "title": "No hot water",
            "location": "Utility cupboard",
            "priority": "emergency",
            "category": "heating",
            "reported_via": "phone",
        },
    )
    assert req["status"] == "submitted"
    assert req["buyer_id"] is not None
    assert req["category"] == "heating"
    assert req["coverage"] == "pending"
    # Emergency SLA is same-day-plus-one from submission.
    assert req["due_date"] == (date.today() + timedelta(days=1)).isoformat()

    # Acknowledge: status and date move, SLA unchanged.
    ack = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/acknowledge", None, expect=200
    )
    assert ack["status"] == "acknowledged"
    assert ack["acknowledged_date"] == date.today().isoformat()

    # Assess: it is in cover, and assessing triages it.
    assessed = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/assess",
        {"coverage": "in_warranty", "assessment_notes": "Faulty diverter valve."},
        expect=200,
    )
    assert assessed["coverage"] == "in_warranty"
    assert assessed["status"] == "triaged"

    # Dispatch a trade with a not-to-exceed.
    order = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"], "instructions": "Replace valve",
         "nte_amount": "250.00"},
    )
    assert order["nte_amount"] == "250.00"

    # Complete with an actual cost, charged back to the trade.
    done = await post(
        client, h,
        f"/api/v1/warranty/service-orders/{order['id']}/complete"
        "?resolution=Valve+replaced&cost_amount=180.50&back_charge=true",
        None, expect=200,
    )
    assert done["status"] == "complete"
    assert done["cost_amount"] == "180.50"
    assert done["back_charge"] is True

    resolved = (await client.get(f"/api/v1/warranty/requests/{req['id']}", headers=h)).json()
    assert resolved["status"] == "resolved"
    assert resolved["resolved_date"] is not None

    closed = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/close", None, expect=200
    )
    assert closed["status"] == "closed"
    assert closed["closed_date"] == date.today().isoformat()


async def test_assess_with_decline_reason_declines_the_claim(client, sold_job):
    h = sold_job["headers"]
    req = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": sold_job["job"]["id"], "title": "Scuffed skirting", "priority": "low",
         "category": "interior"},
    )
    declined = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/assess",
        {"coverage": "out_of_warranty",
         "decline_reason": "Cosmetic, reported after the 2-year period."},
        expect=200,
    )
    assert declined["status"] == "declined"
    assert declined["coverage"] == "out_of_warranty"
    assert "2-year" in declined["decline_reason"]

    # A declined claim is terminal: you cannot dispatch work on it, or close it.
    await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"]}, expect=409,
    )
    await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/close", None, expect=409
    )


async def test_partial_completion_holds_the_claim_in_progress(client, sold_job):
    """Two trades on one claim: the claim only resolves when both are done."""
    h = sold_job["headers"]
    req = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": sold_job["job"]["id"], "title": "Leak damaged two rooms", "priority": "high",
         "category": "plumbing"},
    )
    o1 = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"], "instructions": "Fix the leak"},
    )
    o2 = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"], "instructions": "Re-plaster ceiling"},
    )

    await post(
        client, h, f"/api/v1/warranty/service-orders/{o1['id']}/complete?resolution=Done",
        None, expect=200,
    )
    mid = (await client.get(f"/api/v1/warranty/requests/{req['id']}", headers=h)).json()
    assert mid["status"] == "in_progress"  # one still open

    await post(
        client, h, f"/api/v1/warranty/service-orders/{o2['id']}/complete?resolution=Done",
        None, expect=200,
    )
    end = (await client.get(f"/api/v1/warranty/requests/{req['id']}", headers=h)).json()
    assert end["status"] == "resolved"


async def test_cancelled_order_does_not_block_resolution(client, sold_job):
    h = sold_job["headers"]
    req = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": sold_job["job"]["id"], "title": "Two quotes, one used", "priority": "normal"},
    )
    o1 = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"]},
    )
    o2 = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"]},
    )
    await post(client, h, f"/api/v1/warranty/service-orders/{o2['id']}/cancel", None, expect=200)
    await post(
        client, h, f"/api/v1/warranty/service-orders/{o1['id']}/complete?resolution=Done",
        None, expect=200,
    )
    end = (await client.get(f"/api/v1/warranty/requests/{req['id']}", headers=h)).json()
    assert end["status"] == "resolved"


async def test_service_visit_appointment_moves_claim_in_progress(client, sold_job):
    h = sold_job["headers"]
    req = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": sold_job["job"]["id"], "title": "Sticking window", "priority": "normal"},
    )
    # Triage first so the transition to in_progress is meaningful.
    await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/assess",
        {"coverage": "in_warranty"}, expect=200,
    )
    appt = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/appointments",
        {"kind": "service_visit", "scheduled_for": date.today().isoformat(), "slot": "AM",
         "vendor_id": sold_job["vendor"]["id"]},
    )
    assert appt["kind"] == "service_visit"
    assert appt["status"] == "scheduled"

    after = (await client.get(f"/api/v1/warranty/requests/{req['id']}", headers=h)).json()
    assert after["status"] == "in_progress"
    assert len(after["appointments"]) == 1

    # Complete the visit with an outcome.
    resp = await client.patch(
        f"/api/v1/warranty/appointments/{appt['id']}",
        headers=h,
        json={"status": "completed", "outcome": "Adjusted hinges, window closes cleanly."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"


async def test_notes_activity_log_records_author_and_visibility(client, sold_job):
    # Newest-first ordering is asserted against Postgres in the live run; SQLite's
    # second-precision now() ties two notes written in the same second.
    h = sold_job["headers"]
    req = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": sold_job["job"]["id"], "title": "Boiler pressure", "priority": "normal"},
    )
    await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/notes",
        {"body": "Internal: rang the homeowner, no answer.", "visibility": "internal"},
    )
    await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/notes",
        {"body": "We have booked an engineer for Thursday.", "visibility": "homeowner"},
    )
    notes = (await client.get(f"/api/v1/warranty/requests/{req['id']}/notes", headers=h)).json()
    assert len(notes) == 2
    assert {n["visibility"] for n in notes} == {"internal", "homeowner"}
    assert all(n["author_id"] is not None for n in notes)


async def test_overdue_filter_and_summary(client, sold_job, db_session):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    # One emergency (in warranty), one normal that we will force overdue.
    emergency = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": job_id, "title": "Active leak", "priority": "emergency", "category": "plumbing"},
    )
    stale = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": job_id, "title": "Old snag", "priority": "normal", "category": "interior"},
    )

    # Backdate the normal claim's SLA so it is genuinely overdue.
    row = await db_session.scalar(
        select(WarrantyRequest).where(WarrantyRequest.id == uuid.UUID(stale["id"]))
    )
    row.due_date = date.today() - timedelta(days=2)
    await db_session.commit()

    overdue = (await client.get("/api/v1/warranty/requests?overdue=true", headers=h)).json()
    assert {r["id"] for r in overdue} == {stale["id"]}

    # A back-charged, completed order feeds the callback report.
    order = await post(
        client, h, f"/api/v1/warranty/requests/{emergency['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"]},
    )
    await post(
        client, h,
        f"/api/v1/warranty/service-orders/{order['id']}/complete?cost_amount=90&back_charge=true",
        None, expect=200,
    )

    summary = (await client.get("/api/v1/warranty/summary", headers=h)).json()
    assert summary["total"] == 2
    assert summary["overdue"] == 1
    assert summary["emergency_open"] == 0  # the emergency resolved once its order completed
    assert summary["unassessed"] == 1      # the stale one is still pending coverage
    assert summary["by_category"]["plumbing"] == 1
    assert summary["by_coverage"]["pending"] == 2
    trades = summary["callbacks_by_trade"]
    assert len(trades) == 1
    assert trades[0]["service_orders"] == 1
    assert trades[0]["back_charges"] == 1


async def test_coverage_endpoint_reports_windows(client, sold_job):
    h = sold_job["headers"]
    # The sold job has no closing date yet: cover cannot be measured.
    resp = await client.get(f"/api/v1/warranty/coverage?job_id={sold_job['job']['id']}", headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["closing_date"] is None
    assert body["defects_active"] is False
    assert body["structural_active"] is False


async def test_reprioritising_resets_the_sla_from_submission(client, sold_job):
    h = sold_job["headers"]
    req = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": sold_job["job"]["id"], "title": "Reclassified", "priority": "low"},
    )
    assert req["due_date"] == (date.today() + timedelta(days=30)).isoformat()

    resp = await client.patch(
        f"/api/v1/warranty/requests/{req['id']}", headers=h, json={"priority": "emergency"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["priority"] == "emergency"
    assert resp.json()["due_date"] == (date.today() + timedelta(days=1)).isoformat()
