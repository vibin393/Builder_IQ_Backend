"""Retainage, progress billing and pending-change forecasting.

These three close the gaps between what Procore does with money and what the
system did before. Two of them change reported numbers, so they are the ones
worth pinning down:

  * retainage must reduce what is *paid* without reducing what is *costed* -
    getting that backwards understates the job and overstates the margin
  * a change order that is raised but not yet approved must show up in the
    forecast, because a cost you can see coming is only useful before it lands
"""

from datetime import date
from decimal import Decimal

from tests.conftest import post

TODAY = date.today()


async def _po(client, h, job_id, vendor_id, cost_code_id, amount, retainage="0"):
    """An issued PO. Invoices bill against a commitment, and a draft is not one."""
    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": job_id,
            "vendor_id": vendor_id,
            "retainage_percent": retainage,
            "lines": [{"cost_code_id": cost_code_id, "amount": amount, "description": "Work"}],
        },
    )
    return await post(
        client, h,
        f"/api/v1/purchase-orders/{po['id']}/issue?allow_over_budget=true&skip_compliance=true",
        None, expect=200,
    )


async def _invoice(client, h, po, number, amount, **extra):
    payload = {
        "invoice_number": number,
        "purchase_order_id": po["id"],
        "lines": [
            {"cost_code_id": po["lines"][0]["cost_code_id"], "amount": amount},
        ],
        **extra,
    }
    return await post(client, h, "/api/v1/invoices", payload)


# ========================================================== retainage =====

async def test_purchase_order_reports_what_it_will_hold_back(client, sold_job):
    h = sold_job["headers"]
    po = await _po(
        client, h, sold_job["job"]["id"], sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "10000.00", retainage="10",
    )
    assert Decimal(po["retainage_percent"]) == Decimal("10")
    assert Decimal(po["retainage_on_total"]) == Decimal("1000.00")


async def test_invoice_inherits_retainage_from_its_purchase_order(client, sold_job):
    h = sold_job["headers"]
    po = await _po(
        client, h, sold_job["job"]["id"], sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "10000.00", retainage="10",
    )
    inv = await _invoice(client, h, po, "INV-R1", "10000.00")

    assert Decimal(inv["retainage_percent"]) == Decimal("10")
    assert Decimal(inv["retainage_amount"]) == Decimal("1000.00")
    # The trade is paid the net; the job is costed the whole thing.
    assert Decimal(inv["net_payable"]) == Decimal("9000.00")
    assert Decimal(inv["total"]) == Decimal("10000.00")


async def test_retainage_withholds_payment_without_reducing_cost(client, sold_job):
    """The distinction the whole feature turns on."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    po = await _po(
        client, h, job_id, sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "10000.00", retainage="10",
    )
    inv = await _invoice(client, h, po, "INV-R2", "10000.00")
    await post(client, h, f"/api/v1/invoices/{inv['id']}/approve", None, expect=200)

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()

    # Cost is whole: the work was done.
    assert Decimal(cost["actual_total"]) == Decimal("10000.00")
    # But a thousand pounds of it has not been handed over.
    assert Decimal(cost["retainage_held"]) == Decimal("1000.00")
    assert Decimal(cost["retainage_released"]) == Decimal("0")


async def test_releasing_retainage_needs_a_lien_waiver(client, sold_job):
    """Retainage is the leverage for getting the waiver. Releasing it first wastes that."""
    h = sold_job["headers"]
    po = await _po(
        client, h, sold_job["job"]["id"], sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "10000.00", retainage="10",
    )
    inv = await _invoice(client, h, po, "INV-R3", "10000.00")
    await post(client, h, f"/api/v1/invoices/{inv['id']}/approve", None, expect=200)

    refused = await client.post(f"/api/v1/invoices/{inv['id']}/release-retainage", headers=h)
    assert refused.status_code == 409
    assert "lien waiver" in refused.json()["detail"].lower()


async def test_released_retainage_stops_being_held(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    po = await _po(
        client, h, job_id, sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "10000.00", retainage="10",
    )
    inv = await _invoice(client, h, po, "INV-R4", "10000.00", lien_waiver_received=True)
    await post(client, h, f"/api/v1/invoices/{inv['id']}/approve", None, expect=200)

    released = await post(
        client, h, f"/api/v1/invoices/{inv['id']}/release-retainage", None, expect=200
    )
    assert released["retainage_released_date"] is not None
    assert Decimal(released["retainage_amount"]) == Decimal("0")
    assert Decimal(released["net_payable"]) == Decimal("10000.00")

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["retainage_held"]) == Decimal("0")
    assert Decimal(cost["retainage_released"]) == Decimal("1000.00")
    # Releasing money changes nothing about what the job cost.
    assert Decimal(cost["actual_total"]) == Decimal("10000.00")


async def test_retainage_cannot_be_released_twice(client, sold_job):
    h = sold_job["headers"]
    po = await _po(
        client, h, sold_job["job"]["id"], sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "5000.00", retainage="5",
    )
    inv = await _invoice(client, h, po, "INV-R5", "5000.00", lien_waiver_received=True)
    await post(client, h, f"/api/v1/invoices/{inv['id']}/approve", None, expect=200)
    await post(client, h, f"/api/v1/invoices/{inv['id']}/release-retainage", None, expect=200)

    again = await client.post(f"/api/v1/invoices/{inv['id']}/release-retainage", headers=h)
    assert again.status_code == 409


async def test_held_retainage_list_is_the_release_queue(client, sold_job):
    h = sold_job["headers"]
    po = await _po(
        client, h, sold_job["job"]["id"], sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "8000.00", retainage="10",
    )
    inv = await _invoice(client, h, po, "INV-R6", "8000.00")
    await post(client, h, f"/api/v1/invoices/{inv['id']}/approve", None, expect=200)

    held = (await client.get("/api/v1/invoices/retainage/held", headers=h)).json()
    assert [i["invoice_number"] for i in held] == ["INV-R6"]


async def test_an_invoice_with_no_retainage_is_paid_in_full(client, sold_job):
    """Materials are not retained. The default must stay zero."""
    h = sold_job["headers"]
    po = await _po(
        client, h, sold_job["job"]["id"], sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "3000.00",
    )
    inv = await _invoice(client, h, po, "INV-R7", "3000.00")
    assert Decimal(inv["retainage_amount"]) == Decimal("0")
    assert Decimal(inv["net_payable"]) == Decimal("3000.00")


# =================================================== progress billing =====

async def test_an_invoice_line_can_name_the_po_line_it_bills(client, sold_job):
    h = sold_job["headers"]
    po = await _po(
        client, h, sold_job["job"]["id"], sold_job["vendor"]["id"],
        sold_job["framing"]["id"], "12000.00",
    )
    po_line = po["lines"][0]

    inv = await post(
        client, h, "/api/v1/invoices",
        {
            "invoice_number": "INV-P1",
            "purchase_order_id": po["id"],
            "lines": [
                {
                    "cost_code_id": po_line["cost_code_id"],
                    "amount": "4000.00",
                    "purchase_order_line_id": po_line["id"],
                    "description": "First third",
                }
            ],
        },
    )
    assert inv["lines"][0]["purchase_order_line_id"] == po_line["id"]
    assert Decimal(inv["total"]) == Decimal("4000.00")


# ================================================== pending forecast =====

async def _change_order(client, h, job_id, cost_code_id, amount, party="builder"):
    return await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders",
        {
            "description": "Rock encountered in the footing trench",
            "responsible_party": party,
            "price_delta": "0.00",
            "schedule_days": 2,
            "cost_lines": [{"cost_code_id": cost_code_id, "amount": amount}],
        },
    )


async def test_a_raised_change_order_shows_in_the_forecast_before_approval(client, sold_job):
    """The early warning. `exposure` cannot give it, because it only moves on approval."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    before = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(before["pending_change_total"]) == Decimal("0")

    await _change_order(client, h, job_id, sold_job["framing"]["id"], "9000.00")

    after = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(after["pending_change_total"]) == Decimal("9000.00")
    # Nothing is committed yet, so exposure is untouched.
    assert Decimal(after["exposure_total"]) == Decimal(before["exposure_total"])
    # But the forecast has moved.
    assert Decimal(after["forecast_total"]) == Decimal(after["exposure_total"]) + Decimal("9000.00")


async def test_forecast_warns_before_the_job_is_actually_over(client, sold_job):
    """`will_go_over` is the whole point: inside budget now, outside it if this lands."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    headroom = Decimal(cost["revised_total"]) - Decimal(cost["exposure_total"])
    assert headroom > 0, "fixture should start inside budget"

    await _change_order(client, h, job_id, sold_job["framing"]["id"], str(headroom + 5000))

    after = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert after["over_budget"] is False          # nothing committed yet
    assert after["forecast_over_budget"] is True  # but it is coming
    assert after["will_go_over"] is True


async def test_a_backcharged_change_order_never_raises_the_forecast(client, sold_job):
    """Trade-responsible cost is recovered from the trade, not absorbed by the builder."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    await _change_order(client, h, job_id, sold_job["framing"]["id"], "7000.00", party="trade")

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["pending_change_total"]) == Decimal("0")
    assert cost["forecast_over_budget"] is False


async def test_approving_a_change_order_moves_it_out_of_pending(client, sold_job):
    """Once approved it is in the budget, so counting it as pending would double it."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    co = await _change_order(client, h, job_id, sold_job["framing"]["id"], "6000.00")
    await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders/{co['id']}/approve", None, expect=200
    )

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["pending_change_total"]) == Decimal("0")
    assert Decimal(cost["forecast_total"]) == Decimal(cost["exposure_total"])
