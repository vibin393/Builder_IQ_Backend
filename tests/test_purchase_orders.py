"""The advanced purchase-order module: the full lifecycle, draft editing, recorded
overrides, billing progress, and the guards that keep a commitment honest.

The budget gate and job costing have their own tests in test_purchasing.py.
"""

from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import post

PO = "/api/v1/purchase-orders"


async def _po(client, h, sold_job, amount="20000.00", **extra):
    return await post(
        client, h, PO,
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": sold_job["vendor"]["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": amount}],
            **extra,
        },
    )


async def _issue(client, h, po_id, query=""):
    return await post(client, h, f"{PO}/{po_id}/issue{query}", None, expect=200)


async def test_a_draft_can_be_edited_and_its_lines_replaced(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job)
    r = await client.patch(
        f"{PO}/{po['id']}", headers=h,
        json={
            "notes": "Second fix only",
            "required_by": (date.today() + timedelta(days=14)).isoformat(),
            "lines": [
                {"cost_code_id": sold_job["electrical"]["id"], "amount": "8000.00"},
                {"cost_code_id": sold_job["electrical"]["id"], "amount": "4000.00"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notes"] == "Second fix only"
    assert len(body["lines"]) == 2
    assert Decimal(body["total"]) == Decimal("12000.00")


async def test_an_issued_po_cannot_be_edited(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job)
    await _issue(client, h, po["id"])
    r = await client.patch(f"{PO}/{po['id']}", headers=h, json={"notes": "changed"})
    assert r.status_code == 409
    assert "change order" in r.json()["detail"]


async def test_budget_override_is_recorded_with_its_reason(client, sold_job):
    """25,000 against a 22,000 electrical budget: refused, then issued deliberately,
    and the PO carries the evidence."""
    h = sold_job["headers"]
    po = await _po(client, h, sold_job, amount="25000.00")

    refused = await client.post(f"{PO}/{po['id']}/issue", headers=h)
    assert refused.status_code == 409

    issued = await _issue(
        client, h, po["id"], "?allow_over_budget=true&override_reason=Buyer%20upgrade%20agreed"
    )
    assert issued["budget_override"] is True
    assert issued["compliance_override"] is False
    assert issued["override_reason"] == "Buyer upgrade agreed"
    assert issued["issued_by_id"] is not None


async def test_an_unneeded_override_flag_is_not_recorded(client, sold_job):
    """Passing allow_over_budget on a PO that fits is not a decision worth recording."""
    h = sold_job["headers"]
    po = await _po(client, h, sold_job, amount="20000.00")
    issued = await _issue(client, h, po["id"], "?allow_over_budget=true")
    assert issued["budget_override"] is False


async def test_acknowledge_then_complete(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job)

    refused = await client.post(f"{PO}/{po['id']}/complete", headers=h)
    assert refused.status_code == 409          # a draft cannot be completed

    await _issue(client, h, po["id"])
    acked = await post(client, h, f"{PO}/{po['id']}/acknowledge", None, expect=200)
    assert acked["status"] == "acknowledged"
    assert acked["acknowledged_on"] is not None

    done = await post(client, h, f"{PO}/{po['id']}/complete", None, expect=200)
    assert done["status"] == "complete"
    assert done["completed_on"] is not None


async def test_an_open_po_past_its_required_date_is_overdue(client, sold_job):
    h = sold_job["headers"]
    past = (date.today() - timedelta(days=2)).isoformat()
    po = await _po(client, h, sold_job, required_by=past)
    assert po["is_overdue"] is False               # a draft is not late — nothing is owed yet
    issued = await _issue(client, h, po["id"])
    assert issued["is_overdue"] is True

    overdue = (await client.get(PO, headers=h, params={"overdue_only": True})).json()
    assert po["id"] in [p["id"] for p in overdue]


async def test_an_invoice_cannot_be_raised_against_a_draft(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job)
    r = await client.post(
        "/api/v1/invoices", headers=h,
        json={
            "invoice_number": "INV-DRAFT-1",
            "purchase_order_id": po["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "1000.00"}],
        },
    )
    assert r.status_code == 409
    assert "issued purchase order" in r.json()["detail"]


async def test_billing_progress_and_void_refused_once_billed(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job, amount="20000.00")
    await _issue(client, h, po["id"])

    inv = await post(
        client, h, "/api/v1/invoices",
        {
            "invoice_number": "INV-BILL-1",
            "purchase_order_id": po["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "5000.00"}],
        },
    )
    await post(client, h, f"/api/v1/invoices/{inv['id']}/approve", None, expect=200)

    got = (await client.get(f"{PO}/{po['id']}", headers=h)).json()
    assert Decimal(got["billed_to_date"]) == Decimal("5000.00")
    assert Decimal(got["remaining_to_bill"]) == Decimal("15000.00")
    assert got["percent_billed"] == 25.0

    refused = await client.post(f"{PO}/{po['id']}/void", headers=h)
    assert refused.status_code == 409
    assert "approved invoices" in refused.json()["detail"]


async def test_summary(client, sold_job):
    h = sold_job["headers"]
    a = await _po(client, h, sold_job, amount="10000.00")
    await _issue(client, h, a["id"])
    await _po(client, h, sold_job, amount="3000.00")      # a draft

    s = (await client.get(f"{PO}/summary", headers=h)).json()
    assert s["total"] == 2
    assert s["drafts"] == 1
    assert Decimal(s["draft_value"]) == Decimal("3000.00")
    assert Decimal(s["committed"]) == Decimal("10000.00")
    assert s["open"] == 1
    assert s["awaiting_acknowledgement"] == 1
    assert Decimal(s["remaining_to_bill"]) == Decimal("10000.00")
