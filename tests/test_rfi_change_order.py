"""Raising a change order from an RFI answer.

The join neither source product can make. Procore tracks the question and the
answer but has no buyer to bill; a homebuilder system has the buyer and the
contract but never captured the question. Here a costly answer becomes a priced
change against a named party, with the RFI quoted as the reason.
"""

from decimal import Decimal

from tests.conftest import post


async def _rfi(client, h, sold_job, **extra):
    return await post(
        client, h, "/api/v1/rfis",
        {
            "job_id": sold_job["job"]["id"],
            "subject": "Beam bearing at grid C",
            "question": "Structural and architectural disagree on the head height. Which governs?",
            **extra,
        },
    )


async def test_an_unanswered_rfi_cannot_raise_a_change_order(client, sold_job):
    """A change order has to say what was decided. Nothing has been decided yet."""
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job)

    refused = await client.post(
        f"/api/v1/rfis/{rfi['id']}/change-order", headers=h, json={"price_delta": "500.00"}
    )
    assert refused.status_code == 409
    assert "Answer the RFI first" in refused.json()["detail"]


async def test_an_answer_with_no_cost_impact_cannot_raise_one(client, sold_job):
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job)
    await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/answer",
        {"answer": "Architectural governs. No change.", "cost_impact": False},
        expect=200,
    )

    refused = await client.post(f"/api/v1/rfis/{rfi['id']}/change-order", headers=h, json={})
    assert refused.status_code == 409
    assert "no cost impact" in refused.json()["detail"]


async def test_a_costly_answer_becomes_a_priced_change_order(client, sold_job):
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job)
    await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/answer",
        {
            "answer": "Drop the header and pack out. Additional steel required.",
            "cost_impact": True,
            "schedule_impact_days": 4,
        },
        expect=200,
    )

    result = await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/change-order",
        {
            "responsible_party": "builder",
            "price_delta": "0.00",
            "cost_lines": [
                {"cost_code_id": sold_job["framing"]["id"], "amount": "3200.00",
                 "description": "Additional steel and packing"},
            ],
        },
    )

    assert result["co_number"].startswith("CO")
    assert result["rfi_number"] == rfi["rfi_number"]
    # The schedule impact rides across from the answer rather than being retyped.
    assert result["schedule_days"] == 4
    assert result["responsible_party"] == "builder"

    orders = (
        await client.get(f"/api/v1/jobs/{sold_job['job']['id']}/change-orders", headers=h)
    ).json()
    raised = next(o for o in orders if o["co_number"] == result["co_number"])
    # The RFI is quoted, so in six months it is still obvious why the price moved.
    assert rfi["rfi_number"] in raised["description"]
    assert raised["status"] == "pending_approval"
    assert Decimal(raised["cost_delta"]) == Decimal("3200.00")


async def test_a_buyer_billable_answer_moves_the_contract_when_approved(client, sold_job):
    """The whole point: a question asked on site ends up on the buyer's contract."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    before = (await client.get(f"/api/v1/jobs/{job_id}/contract", headers=h)).json()

    rfi = await _rfi(client, h, sold_job, subject="Buyer asked for a wider opening")
    await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/answer",
        {"answer": "Buyer wants the opening widened. Chargeable.", "cost_impact": True},
        expect=200,
    )
    result = await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/change-order",
        {
            "responsible_party": "buyer",
            "price_delta": "4500.00",
            "cost_lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "2800.00"}],
        },
    )

    await post(
        client, h,
        f"/api/v1/jobs/{job_id}/change-orders/{result['change_order_id']}/approve",
        None, expect=200,
    )

    after = (await client.get(f"/api/v1/jobs/{job_id}/contract", headers=h)).json()
    assert Decimal(after["total_price"]) == Decimal(before["total_price"]) + Decimal("4500.00")


async def test_the_change_order_shows_in_the_forecast_before_approval(client, sold_job):
    """Raised from an RFI, and immediately visible as cost coming."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    rfi = await _rfi(client, h, sold_job)
    await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/answer",
        {"answer": "Extra work needed.", "cost_impact": True},
        expect=200,
    )
    await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/change-order",
        {
            "responsible_party": "builder",
            "cost_lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "6000.00"}],
        },
    )

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["pending_change_total"]) == Decimal("6000.00")
