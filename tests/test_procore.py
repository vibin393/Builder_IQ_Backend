"""The Procore-shaped modules.

The tests that matter most are the two that change behaviour elsewhere:
the compliance gate refusing a purchase order, and a drawing revision
superseding rather than overwriting the set a trade priced from.
"""

from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import post

TODAY = date.today()
PAST = (TODAY - timedelta(days=10)).isoformat()
SOON = (TODAY + timedelta(days=14)).isoformat()
FUTURE = (TODAY + timedelta(days=300)).isoformat()


# ==================================================== prequalification =====

async def _prequalify(client, h, vendor_id, expires=FUTURE):
    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": vendor_id})
    await post(
        client, h, f"/api/v1/prequalification/{pq['id']}/documents",
        {
            "document_type": "general_liability",
            "reference": "GL-1001",
            "issuer": "Acme Insurance",
            "coverage_amount": "2000000.00",
            "expires_on": expires,
        },
    )
    return pq


async def test_approving_requires_nothing_to_have_lapsed(client, sold_job):
    h = sold_job["headers"]
    pq = await _prequalify(client, h, sold_job["vendor"]["id"], expires=PAST)

    refused = await client.post(f"/api/v1/prequalification/{pq['id']}/approve", headers=h)
    assert refused.status_code == 409
    assert "lapsed" in refused.json()["detail"]


async def test_compliance_check_reports_why(client, sold_job):
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    # No record at all warns but does not block, so the module can be adopted
    # without freezing every existing trade on day one.
    before = (await client.get(f"/api/v1/prequalification/check/{vendor_id}", headers=h)).json()
    assert before["ok"] is True
    assert "no prequalification" in before["warning"]

    pq = await _prequalify(client, h, vendor_id)
    await post(client, h, f"/api/v1/prequalification/{pq['id']}/approve", None, expect=200)

    after = (await client.get(f"/api/v1/prequalification/check/{vendor_id}", headers=h)).json()
    assert after["ok"] is True


async def test_purchase_order_is_refused_for_a_rejected_trade(client, sold_job):
    """The gate that makes prequalification worth keeping.

    It refuses what it has checked and found wanting. A trade with no record at
    all only warns - see test_compliance_check_reports_why.
    """
    h = sold_job["headers"]
    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": sold_job["vendor"]["id"]})
    await post(client, h, f"/api/v1/prequalification/{pq['id']}/reject", None, expect=200)

    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": sold_job["vendor"]["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "5000.00"}],
        },
    )

    refused = await client.post(f"/api/v1/purchase-orders/{po['id']}/issue", headers=h)
    assert refused.status_code == 409
    assert "not cleared for work" in refused.json()["detail"]

    # The documented override lets work continue, deliberately.
    override = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/issue?skip_compliance=true", headers=h
    )
    assert override.status_code == 200
    assert override.json()["status"] == "issued"


async def test_purchase_order_issues_once_the_trade_is_cleared(client, sold_job):
    h = sold_job["headers"]
    pq = await _prequalify(client, h, sold_job["vendor"]["id"])
    await post(client, h, f"/api/v1/prequalification/{pq['id']}/approve", None, expect=200)

    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": sold_job["vendor"]["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "5000.00"}],
        },
    )
    issued = await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)
    assert issued["status"] == "issued"


async def test_lapsed_insurance_stops_a_previously_cleared_trade(client, sold_job):
    """Approved in January, insurance lapsed in June. The gate must notice."""
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": vendor_id})
    await post(
        client, h, f"/api/v1/prequalification/{pq['id']}/documents",
        {"document_type": "workers_comp", "expires_on": FUTURE},
    )
    await post(client, h, f"/api/v1/prequalification/{pq['id']}/approve", None, expect=200)

    # A second certificate is added that has already lapsed.
    await post(
        client, h, f"/api/v1/prequalification/{pq['id']}/documents",
        {"document_type": "general_liability", "expires_on": PAST},
    )

    check = (await client.get(f"/api/v1/prequalification/check/{vendor_id}", headers=h)).json()
    assert check["ok"] is False
    assert check["expired"]


async def test_expiring_list_is_ordered_by_urgency(client, sold_job):
    h = sold_job["headers"]
    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": sold_job["vendor"]["id"]})
    for doc_type, expires in [
        ("general_liability", FUTURE),
        ("workers_comp", SOON),
        ("trade_licence", PAST),
    ]:
        await post(
            client, h, f"/api/v1/prequalification/{pq['id']}/documents",
            {"document_type": doc_type, "expires_on": expires},
        )

    rows = (await client.get("/api/v1/prequalification/expiring?within_days=30", headers=h)).json()
    assert [r["document_type"] for r in rows] == ["trade_licence", "workers_comp"]
    assert rows[0]["is_expired"] is True


# ================================================================ RFIs =====

async def test_rfi_moves_through_its_lifecycle(client, sold_job):
    h = sold_job["headers"]
    rfi = await post(
        client, h, "/api/v1/rfis",
        {
            "job_id": sold_job["job"]["id"],
            "subject": "Beam size at the rear opening",
            "question": "Drawing shows 2x10 but the span calls for more. Confirm.",
            "priority": "high",
            "is_blocking": True,
            "due_on": SOON,
        },
    )
    assert rfi["status"] == "open"
    assert rfi["ball_in_court"] == "builder"

    answered = await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/answer",
        {"answer": "Use a 2x12. No cost impact.", "schedule_impact_days": 1},
        expect=200,
    )
    assert answered["status"] == "answered"
    assert answered["answered_on"] is not None

    closed = await post(client, h, f"/api/v1/rfis/{rfi['id']}/close", None, expect=200)
    assert closed["status"] == "closed"
    assert closed["ball_in_court"] == "closed"


async def test_an_rfi_cannot_be_closed_before_it_is_answered(client, sold_job):
    h = sold_job["headers"]
    rfi = await post(
        client, h, "/api/v1/rfis",
        {"job_id": sold_job["job"]["id"], "subject": "x", "question": "y"},
    )
    resp = await client.post(f"/api/v1/rfis/{rfi['id']}/close", headers=h)
    assert resp.status_code == 409


async def test_overdue_rfis_can_be_isolated(client, sold_job):
    h = sold_job["headers"]
    await post(
        client, h, "/api/v1/rfis",
        {"job_id": sold_job["job"]["id"], "subject": "late", "question": "q", "due_on": PAST},
    )
    await post(
        client, h, "/api/v1/rfis",
        {"job_id": sold_job["job"]["id"], "subject": "fine", "question": "q", "due_on": FUTURE},
    )

    overdue = (await client.get("/api/v1/rfis?overdue_only=true", headers=h)).json()
    assert len(overdue) == 1
    assert overdue[0]["subject"] == "late"


# ========================================================== submittals =====

async def test_revise_and_resubmit_bumps_the_revision(client, sold_job):
    """A resubmission has to be traceable, not a silent overwrite."""
    h = sold_job["headers"]
    sub = await post(
        client, h, "/api/v1/submittals",
        {
            "title": "Kitchen tile — Gourmet package",
            "job_id": sold_job["job"]["id"],
            "manufacturer": "Marazzi",
            "option_id": sold_job["option"]["id"],
        },
    )
    assert sub["revision"] == 1
    assert sub["status"] == "submitted"

    revised = await post(
        client, h, f"/api/v1/submittals/{sub['id']}/review",
        {"status": "revise_and_resubmit", "review_notes": "Wrong finish. Buyer chose matte."},
        expect=200,
    )
    assert revised["revision"] == 2
    assert revised["ball_in_court"] == "trade"

    # The trade sends the corrected revision back before it can be reviewed again -
    # a re-review needs something new to look at, not a silent re-approval.
    await post(
        client, h, f"/api/v1/submittals/{sub['id']}/resubmit",
        {"note": "Matte finish."}, expect=200,
    )

    approved = await post(
        client, h, f"/api/v1/submittals/{sub['id']}/review",
        {"status": "approved_as_noted", "review_notes": "Matte confirmed."},
        expect=200,
    )
    assert approved["status"] == "approved_as_noted"
    assert approved["ball_in_court"] == "closed"


# ============================================ drawings and transmittals ====

async def test_revising_a_set_supersedes_rather_than_overwrites(client, sold_job):
    """The old set is the evidence of what a trade priced from."""
    h = sold_job["headers"]
    ds = await post(
        client, h, "/api/v1/drawing-sets",
        {"name": "The Maple — permit set", "plan_id": sold_job["plan"]["id"]},
    )
    ds = await post(
        client, h, f"/api/v1/drawing-sets/{ds['id']}/drawings",
        {"sheet_number": "A-101", "title": "Ground floor plan"},
        expect=201,
    )
    await post(client, h, f"/api/v1/drawing-sets/{ds['id']}/issue", None, expect=200)

    new = await post(client, h, f"/api/v1/drawing-sets/{ds['id']}/revise", None, expect=200)
    assert new["revision"] == 2
    assert new["status"] == "draft"
    assert len(new["drawings"]) == 1  # sheets carried forward

    sets = (await client.get("/api/v1/drawing-sets", headers=h)).json()
    old = next(s for s in sets if s["id"] == ds["id"])
    assert old["status"] == "superseded"
    assert old["superseded_by_id"] == new["id"]


async def test_transmittal_records_who_was_told_and_who_confirmed(client, sold_job):
    h = sold_job["headers"]
    ds = await post(client, h, "/api/v1/drawing-sets", {"name": "Revised structural"})

    t = await post(
        client, h, "/api/v1/transmittals",
        {
            "subject": "Revised structural drawings — rev 2",
            "drawing_set_id": ds["id"],
            "vendor_ids": [sold_job["vendor"]["id"]],
        },
    )
    assert t["outstanding"] == 1
    assert t["acknowledged_count"] == 0

    acked = await post(
        client, h,
        f"/api/v1/transmittals/{t['id']}/acknowledge/{sold_job['vendor']['id']}",
        None, expect=200,
    )
    assert acked["acknowledged_count"] == 1
    assert acked["outstanding"] == 0


# =================================================== safety and quality ====

async def test_observation_links_back_to_the_inspection_that_failed(client, sold_job):
    """Procore's best idea: a failed inspection raises a tracked corrective action."""
    h = sold_job["headers"]

    insp = await post(
        client, h, "/api/v1/field/inspections",
        {"job_id": sold_job["job"]["id"], "name": "Framing inspection"},
    )
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed", "failure_reason": "Missing hurricane ties"},
        expect=200,
    )

    obs = await post(
        client, h, "/api/v1/observations",
        {
            "job_id": sold_job["job"]["id"],
            "title": "Install hurricane ties at four truss connections",
            "inspection_id": insp["id"],
            "observation_type": "safety",
            "severity": "high",
            "assigned_vendor_id": sold_job["vendor"]["id"],
            "due_on": SOON,
        },
    )
    assert obs["inspection_id"] == insp["id"]
    assert obs["status"] == "initiated"

    # It cannot be closed by setting the status. The fix is claimed, then somebody
    # other than whoever did the work accepts it - an observation that closed on
    # the word of the person who owed it recorded an intention, not an outcome.
    refused = await client.patch(
        f"/api/v1/observations/{obs['id']}",
        headers=h,
        json={"status": "closed", "resolution": "Ties fitted and re-inspected."},
    )
    assert refused.status_code == 422

    await post(
        client, h, f"/api/v1/observations/{obs['id']}/submit",
        {"resolution": "Ties fitted and re-inspected."}, expect=200,
    )
    closed = await post(
        client, h, f"/api/v1/observations/{obs['id']}/close", None, expect=200
    )
    assert closed["status"] == "closed"
    assert closed["closed_on"] is not None
    assert closed["closed_by_id"] is not None


async def test_incident_records_a_near_miss(client, sold_job):
    h = sold_job["headers"]
    inc = await post(
        client, h, "/api/v1/incidents",
        {
            "job_id": sold_job["job"]["id"],
            "incident_type": "near_miss",
            "title": "Unsecured ladder slipped",
            "severity": "high",
            "immediate_action": "Ladder tied off; toolbox talk held.",
        },
    )
    assert inc["incident_type"] == "near_miss"
    assert inc["occurred_on"] is not None
    assert inc["reportable"] is False


async def test_a_plan_is_not_complete_while_a_step_awaits_its_witness(client, sold_job):
    """Completing every step is not the same as finishing the plan.

    The last step here needs a witness, and counting it as finished would report
    the plan complete while the thing it exists to guarantee had not happened.
    """
    h = sold_job["headers"]
    plan = await post(
        client, h, "/api/v1/action-plans",
        {"name": "Pre-drywall walk", "job_id": sold_job["job"]["id"]},
    )
    for step in ("Check rough-in", "Photograph wall cavities", "Buyer walk"):
        plan = await post(
            client, h, f"/api/v1/action-plans/{plan['id']}/items",
            {"description": step, "requires_signoff": step == "Buyer walk"},
            expect=200,
        )
    assert plan["percent_complete"] == 0

    for item in plan["items"]:
        plan = await post(
            client, h, f"/api/v1/action-plans/{plan['id']}/items/{item['id']}/complete",
            None, expect=200,
        )

    assert plan["completed_items"] == 3        # all marked done
    assert plan["done_items"] == 2             # but one is not finished
    # The meter measures finished steps, so it never reads 100% beside a status
    # that says the plan is still running.
    assert plan["percent_complete"] == 67
    assert plan["status"] == "active"
    assert plan["items"][2]["awaiting_signoff"] is True


# ====================================================== meetings, time =====

async def test_meeting_tracks_open_actions(client, sold_job):
    h = sold_job["headers"]
    m = await post(
        client, h, "/api/v1/meetings",
        {"title": "Weekly site meeting", "job_id": sold_job["job"]["id"]},
    )
    m = await post(
        client, h, f"/api/v1/meetings/{m['id']}/items",
        {"topic": "Framing progress", "is_action": False}, expect=200,
    )
    m = await post(
        client, h, f"/api/v1/meetings/{m['id']}/items",
        {"topic": "Chase the electrical submittal", "is_action": True, "due_on": SOON},
        expect=200,
    )
    assert m["open_actions"] == 1
    assert len(m["items"]) == 2


async def test_productivity_rolls_hours_up_by_cost_code(client, sold_job):
    h = sold_job["headers"]
    for hours, rate in [("8", "45.00"), ("6.5", "45.00")]:
        await post(
            client, h, "/api/v1/timecards",
            {
                "job_id": sold_job["job"]["id"],
                "worker_name": "Crew A",
                "hours": hours,
                "hourly_rate": rate,
                "cost_code_id": sold_job["framing"]["id"],
            },
        )

    report = (await client.get("/api/v1/timecards/productivity", headers=h)).json()
    assert Decimal(report["total_hours"]) == Decimal("14.5")
    assert Decimal(report["total_labour_cost"]) == Decimal("652.50")
    assert report["rows"][0]["cost_code"] == "06-100"


async def test_overtime_is_paid_at_time_and_a_half(client, sold_job):
    h = sold_job["headers"]
    await post(
        client, h, "/api/v1/timecards",
        {
            "job_id": sold_job["job"]["id"],
            "worker_name": "Crew B",
            "hours": "8",
            "overtime_hours": "2",
            "hourly_rate": "40.00",
        },
    )
    cards = (await client.get("/api/v1/timecards", headers=h)).json()["items"]
    # 8 * 40 + 2 * 40 * 1.5
    assert Decimal(cards[0]["labour_cost"]) == Decimal("440.00")
    # The hours are not free just because nobody has signed them off yet, but
    # they are not a cost either until somebody does.
    assert cards[0]["is_approved"] is False
