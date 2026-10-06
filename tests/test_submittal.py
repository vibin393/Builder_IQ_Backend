"""The advanced submittal module: the review workflow, revisions and procurement.

Submittals had no tests. This covers the lifecycle (draft -> submit -> review ->
revise-and-resubmit loop), the revision history each round leaves behind, the
procurement at-risk warning, and the register summary.
"""

from datetime import date, timedelta

from tests.conftest import post


async def _sub(client, h, **extra):
    """Create a submittal. Defaults to going straight into the review queue."""
    return await post(
        client, h, "/api/v1/submittals",
        {"title": "Kitchen tap — brushed nickel", "manufacturer": "Delta",
         "submittal_type": "product_data", **extra},
    )


async def test_creating_submits_by_default_and_logs_it(client, sold_job):
    h = sold_job["headers"]
    sub = await _sub(client, h, job_id=sold_job["job"]["id"], priority="high")

    assert sub["status"] == "submitted"
    assert sub["ball_in_court"] == "builder"
    assert sub["submitted_on"] is not None
    # High priority is a 7-day review promise, so a due date is set even with none given.
    assert sub["due_on"] is not None
    kinds = [a["kind"] for a in sub["activity"]]
    assert "created" in kinds and "submitted" in kinds


async def test_a_draft_can_be_held_then_submitted(client, sold_job):
    h = sold_job["headers"]
    draft = await _sub(client, h, submit_now=False)
    assert draft["status"] == "draft"
    assert draft["ball_in_court"] == "trade"

    submitted = await post(client, h, f"/api/v1/submittals/{draft['id']}/submit", None, expect=200)
    assert submitted["status"] == "submitted"
    assert submitted["submitted_on"] is not None


async def test_approve_closes_it_and_records_the_round(client, sold_job):
    h = sold_job["headers"]
    sub = await _sub(client, h)

    started = await post(
        client, h, f"/api/v1/submittals/{sub['id']}/start-review", None, expect=200
    )
    assert started["status"] == "under_review"

    approved = await post(
        client, h, f"/api/v1/submittals/{sub['id']}/review",
        {"status": "approved", "review_notes": "Matches the selection. Fit it."},
        expect=200,
    )
    assert approved["status"] == "approved"
    assert approved["ball_in_court"] == "closed"
    assert approved["approved_on"] is not None
    # The round was written to the history.
    assert len(approved["revisions"]) == 1
    assert approved["revisions"][0]["outcome"] == "approved"


async def test_revise_and_resubmit_loops_and_keeps_every_round(client, sold_job):
    """Revise-and-resubmit bumps the revision, hands it to the trade, and the trade's
    resubmission comes back for another decision — each round preserved."""
    h = sold_job["headers"]
    sub = await _sub(client, h)

    revised = await post(
        client, h, f"/api/v1/submittals/{sub['id']}/review",
        {"status": "revise_and_resubmit",
         "review_notes": "Submitted 600×600; selection is 300×600."},
        expect=200,
    )
    assert revised["status"] == "revise_and_resubmit"
    assert revised["ball_in_court"] == "trade"
    assert revised["revision"] == 2
    assert revised["submitted_on"] is None          # the new round has not been submitted
    assert len(revised["revisions"]) == 1

    resubmitted = await post(
        client, h, f"/api/v1/submittals/{sub['id']}/resubmit",
        {"note": "Corrected to 300×600."},
        expect=200,
    )
    assert resubmitted["status"] == "submitted"
    assert resubmitted["revision"] == 2
    assert resubmitted["submitted_on"] is not None
    assert resubmitted["ball_in_court"] == "builder"

    approved = await post(
        client, h, f"/api/v1/submittals/{sub['id']}/review",
        {"status": "approved"}, expect=200,
    )
    assert approved["status"] == "approved"
    # Two rounds are now on record: the revise, then the approve.
    assert len(approved["revisions"]) == 2
    assert [r["outcome"] for r in approved["revisions"]] == ["revise_and_resubmit", "approved"]


async def test_review_refused_on_a_draft(client, sold_job):
    h = sold_job["headers"]
    draft = await _sub(client, h, submit_now=False)
    refused = await client.post(
        f"/api/v1/submittals/{draft['id']}/review", headers=h, json={"status": "approved"}
    )
    assert refused.status_code == 409
    assert "submitted or under-review" in refused.json()["detail"]


async def test_procurement_at_risk_when_runway_runs_out(client, sold_job):
    """A long lead time against a near required-on-site date leaves no runway to review
    and order — the register flags it before the review due date ever passes."""
    h = sold_job["headers"]
    soon = (date.today() + timedelta(days=5)).isoformat()
    sub = await _sub(client, h, required_on_site=soon, lead_time_days=30)

    assert sub["required_on_site"] == soon
    assert sub["order_by_date"] is not None
    assert sub["is_at_risk"] is True

    summary = (await client.get("/api/v1/submittals/summary", headers=h)).json()
    assert summary["at_risk"] >= 1


async def test_summary_counts_the_register(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    a = await _sub(client, h, job_id=job_id, submittal_type="sample")
    await _sub(client, h, job_id=job_id, submit_now=False)      # a draft
    await post(client, h, f"/api/v1/submittals/{a['id']}/review",
               {"status": "approved"}, expect=200)

    s = (
        await client.get("/api/v1/submittals/summary", headers=h, params={"job_id": job_id})
    ).json()
    assert s["total"] == 2
    assert s["draft"] == 1
    assert s["approved"] == 1
    assert s["by_type"]["sample"] == 1
    assert s["avg_review_days"] is not None


async def test_reassign_moves_the_ball_and_logs_it(client, sold_job):
    h = sold_job["headers"]
    sub = await _sub(client, h)
    moved = await post(
        client, h, f"/api/v1/submittals/{sub['id']}/reassign",
        {"ball_in_court": "architect", "note": "Over to design to check the spec."},
        expect=200,
    )
    assert moved["ball_in_court"] == "architect"
    reassigned = [a for a in moved["activity"] if a["kind"] == "reassigned"]
    assert reassigned and "architect" in reassigned[0]["detail"]
