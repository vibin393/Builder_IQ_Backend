"""The advanced bidding module: levelling, coverage, the estimate benchmark, decline,
overdue and the summary. The award->PO flow has its own test in test_extended.py.
"""

from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import post

BASE = "/api/v1/bid-packages"


async def _vendor(client, h, code, name):
    return await post(
        client, h, "/api/v1/vendors", {"code": code, "name": name, "vendor_type": "trade"}
    )


async def _package(client, h, sold_job, **extra):
    return await post(
        client, h, BASE,
        {"name": "Electrical rough-in", "cost_code_id": sold_job["electrical"]["id"], **extra},
    )


async def _invite(client, h, pid, vendor_ids):
    return await post(client, h, f"{BASE}/{pid}/invite", {"vendor_ids": vendor_ids}, expect=200)


async def _submit(client, h, pid, bid_id, amount):
    return await post(
        client, h, f"{BASE}/{pid}/bids/{bid_id}/submit", {"amount": amount}, expect=200
    )


async def _award(client, h, pid, bid_id, job_id=None):
    url = f"{BASE}/{pid}/bids/{bid_id}/award"
    if job_id:
        url += f"?job_id={job_id}"
    return await post(client, h, url, None, expect=200)


async def test_levelling_and_coverage(client, sold_job):
    """Three trades invited, two price it and one declines: the package levels the
    prices and reports how much of the field responded."""
    h = sold_job["headers"]
    v2 = await _vendor(client, h, "SPARK2", "Volt Electrical")
    v3 = await _vendor(client, h, "SPARK3", "Amp Electrical")
    pkg = await _package(client, h, sold_job, estimate="21000.00")
    pid = pkg["id"]

    pkg = await _invite(client, h, pid, [sold_job["vendor"]["id"], v2["id"], v3["id"]])
    assert pkg["invited_count"] == 3

    by_vendor = {b["vendor_id"]: b["id"] for b in pkg["bids"]}
    await _submit(client, h, pid, by_vendor[sold_job["vendor"]["id"]], "22800.00")
    await _submit(client, h, pid, by_vendor[v2["id"]], "19500.00")
    await post(client, h, f"{BASE}/{pid}/bids/{by_vendor[v3['id']]}/decline", None, expect=200)

    pkg = (await client.get(f"{BASE}/{pid}", headers=h)).json()
    assert Decimal(pkg["low_bid"]) == Decimal("19500.00")
    assert Decimal(pkg["high_bid"]) == Decimal("22800.00")
    assert Decimal(pkg["spread"]) == Decimal("3300.00")
    assert Decimal(pkg["average_bid"]) == Decimal("21150.00")
    assert pkg["submitted_count"] == 2
    assert pkg["declined_count"] == 1
    assert pkg["response_rate"] == 1.0        # 3 of 3 responded (2 priced, 1 declined)
    # Savings before award levels against the low bid: 21000 - 19500 = 1500.
    assert Decimal(pkg["savings_vs_estimate"]) == Decimal("1500.00")


async def test_declining_clears_the_price_and_cannot_award(client, sold_job):
    h = sold_job["headers"]
    pkg = await _package(client, h, sold_job)
    pkg = await _invite(client, h, pkg["id"], [sold_job["vendor"]["id"]])
    bid = pkg["bids"][0]
    declined = await post(
        client, h, f"{BASE}/{pkg['id']}/bids/{bid['id']}/decline", None, expect=200
    )
    assert declined["status"] == "declined"
    assert declined["amount"] is None


async def test_savings_recorded_on_award(client, sold_job):
    h = sold_job["headers"]
    pkg = await _package(client, h, sold_job, estimate="20000.00")
    pkg = await _invite(client, h, pkg["id"], [sold_job["vendor"]["id"]])
    bid = pkg["bids"][0]
    await _submit(client, h, pkg["id"], bid["id"], "19500.00")
    result = await _award(client, h, pkg["id"], bid["id"], sold_job["job"]["id"])
    assert Decimal(result["savings_vs_estimate"]) == Decimal("500.00")


async def test_overdue_package_is_flagged(client, sold_job):
    h = sold_job["headers"]
    past = (date.today() - timedelta(days=1)).isoformat()
    pkg = await _package(client, h, sold_job, due_date=past)
    pkg = await _invite(client, h, pkg["id"], [sold_job["vendor"]["id"]])
    assert pkg["is_overdue"] is True

    overdue = (await client.get(BASE, headers=h, params={"overdue_only": True})).json()
    assert pkg["id"] in [p["id"] for p in overdue]


async def test_cancel_package(client, sold_job):
    h = sold_job["headers"]
    pkg = await _package(client, h, sold_job)
    cancelled = await post(client, h, f"{BASE}/{pkg['id']}/cancel", None, expect=200)
    assert cancelled["status"] == "cancelled"


async def test_awarded_package_cannot_be_cancelled(client, sold_job):
    h = sold_job["headers"]
    pkg = await _package(client, h, sold_job)
    pkg = await _invite(client, h, pkg["id"], [sold_job["vendor"]["id"]])
    bid = pkg["bids"][0]
    await _submit(client, h, pkg["id"], bid["id"], "19500.00")
    await _award(client, h, pkg["id"], bid["id"])
    refused = await client.post(f"{BASE}/{pkg['id']}/cancel", headers=h)
    assert refused.status_code == 409


async def test_update_package_estimate(client, sold_job):
    h = sold_job["headers"]
    pkg = await _package(client, h, sold_job)
    updated = await client.patch(f"{BASE}/{pkg['id']}", headers=h, json={"estimate": "18000.00"})
    assert updated.status_code == 200
    assert Decimal(updated.json()["estimate"]) == Decimal("18000.00")


async def test_summary(client, sold_job):
    h = sold_job["headers"]
    p1 = await _package(client, h, sold_job, estimate="20000.00")
    p1 = await _invite(client, h, p1["id"], [sold_job["vendor"]["id"]])
    bid = p1["bids"][0]
    await _submit(client, h, p1["id"], bid["id"], "18500.00")
    await _award(client, h, p1["id"], bid["id"])
    await _package(client, h, sold_job)  # a draft

    s = (await client.get(f"{BASE}/summary", headers=h)).json()
    assert s["packages"] == 2
    assert s["awarded"] == 1
    assert s["draft"] == 1
    assert Decimal(s["awarded_value"]) == Decimal("18500.00")
    assert Decimal(s["savings"]) == Decimal("1500.00")
