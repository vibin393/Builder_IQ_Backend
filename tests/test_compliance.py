"""The advanced trade-compliance module: the prequalification lifecycle, required-cover
coverage, the review clock, document removal and the summary. The purchase-order gate
and cover-gaps have their own tests (test_procore.py, test_cover_gaps.py).
"""

from datetime import date, timedelta

from tests.conftest import post

PQ = "/api/v1/prequalification"


async def _pq(client, h, vendor_id):
    return await post(client, h, PQ, {"vendor_id": vendor_id})


async def test_lifecycle_invite_submit_review_approve(client, sold_job):
    h = sold_job["headers"]
    pq = await _pq(client, h, sold_job["vendor"]["id"])
    assert pq["status"] == "not_started"

    pq = await post(client, h, f"{PQ}/{pq['id']}/invite", {}, expect=200)
    assert pq["status"] == "invited"
    pq = await post(client, h, f"{PQ}/{pq['id']}/submit", None, expect=200)
    assert pq["status"] == "submitted"
    pq = await post(client, h, f"{PQ}/{pq['id']}/start-review", None, expect=200)
    assert pq["status"] == "under_review"

    pq = await post(client, h, f"{PQ}/{pq['id']}/approve",
                    {"conditional": True, "single_job_limit": "50000"}, expect=200)
    assert pq["status"] == "conditional"
    assert pq["single_job_limit"] == "50000.00"
    assert pq["is_compliant"] is True            # conditional still clears the gate
    assert pq["review_due"] is not None          # the annual clock was set


async def test_missing_required_cover_is_flagged(client, sold_job):
    """A fresh record is missing both required covers; adding one shrinks the gap."""
    h = sold_job["headers"]
    pq = await _pq(client, h, sold_job["vendor"]["id"])
    assert set(pq["missing_required"]) == {"public_liability", "employers_liability"}

    pq = await post(
        client, h, f"{PQ}/{pq['id']}/documents",
        {"document_type": "public_liability", "issuer": "Aviva",
         "expires_on": (date.today() + timedelta(days=200)).isoformat()},
        expect=201,
    )
    assert pq["missing_required"] == ["employers_liability"]


async def test_overdue_review_blocks_and_renew_clears_it(client, sold_job):
    h = sold_job["headers"]
    pq = await _pq(client, h, sold_job["vendor"]["id"])
    await post(client, h, f"{PQ}/{pq['id']}/approve", None, expect=200)

    # Push the review into the past: the gate now refuses even though it is approved.
    past = (date.today() - timedelta(days=1)).isoformat()
    r = await client.patch(f"{PQ}/{pq['id']}", headers=h, json={"review_due": past})
    body = r.json()
    assert body["review_overdue"] is True
    assert body["is_compliant"] is False
    assert "review is overdue" in body["blocking_reason"]

    renewed = await post(client, h, f"{PQ}/{pq['id']}/renew-review", None, expect=200)
    assert renewed["review_overdue"] is False
    assert renewed["is_compliant"] is True
    assert renewed["blocking_reason"] is None


async def test_a_document_can_be_removed(client, sold_job):
    h = sold_job["headers"]
    pq = await _pq(client, h, sold_job["vendor"]["id"])
    pq = await post(
        client, h, f"{PQ}/{pq['id']}/documents",
        {"document_type": "public_liability"}, expect=201,
    )
    doc_id = pq["documents"][0]["id"]
    after = await client.delete(f"{PQ}/{pq['id']}/documents/{doc_id}", headers=h)
    assert after.status_code == 200
    assert after.json()["documents"] == []


async def test_a_fresh_certificate_lifts_an_expired_record(client, sold_job):
    """An approved record with a lapsed cert reads as expired; a new cert restores it."""
    h = sold_job["headers"]
    pq = await _pq(client, h, sold_job["vendor"]["id"])
    # Add a still-valid cert, approve, then add a lapsed one and expire the record.
    past = (date.today() - timedelta(days=5)).isoformat()
    pq = await post(client, h, f"{PQ}/{pq['id']}/approve", None, expect=200)
    pq = await post(
        client, h, f"{PQ}/{pq['id']}/documents",
        {"document_type": "public_liability", "expires_on": past}, expect=201,
    )
    # Lapsed doc → gate refuses even though status is still approved.
    assert pq["is_compliant"] is False
    # Replace with an in-date cert and remove the lapsed one.
    doc_id = pq["documents"][0]["id"]
    await client.delete(f"{PQ}/{pq['id']}/documents/{doc_id}", headers=h)
    pq = await post(
        client, h, f"{PQ}/{pq['id']}/documents",
        {"document_type": "public_liability",
         "expires_on": (date.today() + timedelta(days=300)).isoformat()},
        expect=201,
    )
    assert pq["is_compliant"] is True


async def test_summary_counts_the_board(client, sold_job):
    h = sold_job["headers"]
    pq = await _pq(client, h, sold_job["vendor"]["id"])
    await post(client, h, f"{PQ}/{pq['id']}/approve", None, expect=200)

    s = (await client.get(f"{PQ}/summary", headers=h)).json()
    assert s["prequalified"] == 1
    assert s["cleared"] == 1
    assert s["approved"] == 1
    assert s["would_be_refused"] == 0
    # The builder fixture has one trade with a record, so no_record is 0 here.
    assert s["no_record"] == 0
    assert s["missing_required"] == 1            # approved but no cover on file
    assert s["by_status"]["approved"] == 1
