"""The advanced invoice module: the waiver dead-end fix, disputes, void, the recorded
over-PO override, UK CIS deductions, payment terms and the payables summary.

Retention and progress billing have their own tests in test_money_controls.py.
"""

from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import post

INV = "/api/v1/invoices"


async def _issued_po(client, h, sold_job, amount="20000.00", retainage="0"):
    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": sold_job["vendor"]["id"],
            "retainage_percent": retainage,
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": amount}],
        },
    )
    return await post(
        client, h,
        f"/api/v1/purchase-orders/{po['id']}/issue?allow_over_budget=true&skip_compliance=true",
        None, expect=200,
    )


async def _invoice(client, h, po, number, amount, **extra):
    return await post(
        client, h, INV,
        {
            "invoice_number": number,
            "purchase_order_id": po["id"],
            "lines": [{"cost_code_id": po["lines"][0]["cost_code_id"], "amount": amount}],
            **extra,
        },
    )


async def test_a_waiver_can_be_recorded_after_receipt_and_then_paid(client, sold_job):
    """Logged without a waiver, the invoice used to be unpayable for ever."""
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job)
    inv = await _invoice(client, h, po, "INV-W1", "5000.00")
    await post(client, h, f"{INV}/{inv['id']}/approve", None, expect=200)

    refused = await client.post(f"{INV}/{inv['id']}/pay", headers=h)
    assert refused.status_code == 409

    waived = await post(client, h, f"{INV}/{inv['id']}/lien-waiver", None, expect=200)
    assert waived["lien_waiver_received"] is True
    assert waived["lien_waiver_date"] is not None

    paid = await post(
        client, h, f"{INV}/{inv['id']}/pay?payment_reference=BACS-4471", None, expect=200
    )
    assert paid["status"] == "paid"
    assert paid["payment_reference"] == "BACS-4471"
    assert paid["paid_by_id"] is not None


async def test_a_dispute_holds_approval_until_resolved(client, sold_job):
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job)
    inv = await _invoice(client, h, po, "INV-D1", "5000.00")

    disputed = await post(
        client, h, f"{INV}/{inv['id']}/dispute", {"reason": "Second fix not yet done"}, expect=200
    )
    assert disputed["status"] == "disputed"

    refused = await client.post(f"{INV}/{inv['id']}/approve", headers=h)
    assert refused.status_code == 409
    assert "dispute" in refused.json()["detail"]

    resolved = await post(client, h, f"{INV}/{inv['id']}/resolve", None, expect=200)
    assert resolved["status"] == "received"
    assert resolved["dispute_reason"] == "Second fix not yet done"   # kept on record
    await post(client, h, f"{INV}/{inv['id']}/approve", None, expect=200)


async def test_void_only_before_approval(client, sold_job):
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job)
    a = await _invoice(client, h, po, "INV-V1", "1000.00")
    voided = await post(client, h, f"{INV}/{a['id']}/void", None, expect=200)
    assert voided["status"] == "void"

    b = await _invoice(client, h, po, "INV-V2", "1000.00")
    await post(client, h, f"{INV}/{b['id']}/approve", None, expect=200)
    refused = await client.post(f"{INV}/{b['id']}/void", headers=h)
    assert refused.status_code == 409


async def test_over_po_override_is_recorded_only_when_over(client, sold_job):
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job, amount="10000.00")

    fits = await _invoice(client, h, po, "INV-O1", "4000.00")
    ok = await post(client, h, f"{INV}/{fits['id']}/approve?allow_over_po=true", None, expect=200)
    assert ok["over_po_override"] is False            # it fitted, so nothing to record

    over = await _invoice(client, h, po, "INV-O2", "8000.00")   # 4k + 8k > 10k
    refused = await client.post(f"{INV}/{over['id']}/approve", headers=h)
    assert refused.status_code == 409
    query = "allow_over_po=true&override_reason=Extra%20sockets%20agreed"
    approved = await post(client, h, f"{INV}/{over['id']}/approve?{query}", None, expect=200)
    assert approved["over_po_override"] is True
    assert approved["override_reason"] == "Extra sockets agreed"
    assert approved["approved_by_id"] is not None


async def test_cis_is_deducted_from_labour_only(client, sold_job):
    """A net-registered trade: 20% of the labour element goes to HMRC, not materials."""
    h = sold_job["headers"]
    r = await client.patch(
        f"/api/v1/vendors/{sold_job['vendor']['id']}", headers=h,
        json={"cis_status": "net", "utr": "1234567890"},
    )
    assert r.status_code == 200, r.text

    po = await _issued_po(client, h, sold_job, retainage="5")
    inv = await _invoice(client, h, po, "INV-C1", "12000.00", labour_amount="10000.00")
    assert Decimal(inv["cis_rate"]) == Decimal("20")
    assert inv["cis_verified"] is True
    assert Decimal(inv["cis_deduction"]) == Decimal("2000.00")
    # 12,000 − 600 retention (5%) − 2,000 CIS
    assert Decimal(inv["net_payable"]) == Decimal("9400.00")


async def test_an_unverified_trade_is_flagged_not_guessed(client, sold_job):
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job)
    inv = await _invoice(client, h, po, "INV-C2", "6000.00", labour_amount="4000.00")
    assert inv["cis_verified"] is False
    assert Decimal(inv["cis_deduction"]) == Decimal("0")

    s = (await client.get(f"{INV}/summary", headers=h)).json()
    assert s["cis_unverified"] == 1


async def test_labour_cannot_exceed_the_total(client, sold_job):
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job)
    r = await client.post(
        INV, headers=h,
        json={
            "invoice_number": "INV-C3",
            "purchase_order_id": po["id"],
            "labour_amount": "9000.00",
            "lines": [{"cost_code_id": po["lines"][0]["cost_code_id"], "amount": "5000.00"}],
        },
    )
    assert r.status_code == 422


async def test_default_terms_and_overdue_to_pay(client, sold_job):
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job)
    today = date.today()
    fresh = await _invoice(client, h, po, "INV-T1", "1000.00")
    assert fresh["due_date"] == (today + timedelta(days=30)).isoformat()

    old = await _invoice(
        client, h, po, "INV-T2", "1000.00",
        invoice_date=(today - timedelta(days=45)).isoformat(),
    )
    approved = await post(client, h, f"{INV}/{old['id']}/approve", None, expect=200)
    assert approved["is_overdue"] is True
    assert approved["days_overdue"] == 15


async def test_summary(client, sold_job):
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job)
    a = await _invoice(client, h, po, "INV-S1", "3000.00")
    await post(client, h, f"{INV}/{a['id']}/approve", None, expect=200)
    await _invoice(client, h, po, "INV-S2", "2000.00")
    b = await _invoice(client, h, po, "INV-S3", "500.00")
    await post(client, h, f"{INV}/{b['id']}/dispute", {"reason": "Wrong rate"}, expect=200)

    s = (await client.get(f"{INV}/summary", headers=h)).json()
    assert s["total"] == 3
    assert Decimal(s["actual_cost"]) == Decimal("3000.00")
    assert s["awaiting_approval"] == 1
    assert Decimal(s["awaiting_value"]) == Decimal("2000.00")
    assert s["disputed"] == 1
    assert s["approved_unpaid"] == 1
    assert s["waivers_missing"] == 1
