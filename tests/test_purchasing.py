"""Purchase orders and job costing.

The guard under test: committed cost is what a PO creates, and the system refuses to
commit past the budget without someone saying so explicitly. That single check is most
of what "margin leakage" means in practice.
"""

from decimal import Decimal

from httpx import AsyncClient

from tests.conftest import post


async def _po(client: AsyncClient, headers, job_id: str, vendor_id: str, lines) -> dict:
    return await post(
        client, headers, "/api/v1/purchase-orders",
        {"job_id": job_id, "vendor_id": vendor_id, "lines": lines},
    )


async def test_draft_po_is_not_a_commitment(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    po = await _po(
        client, h, job_id, sold_job["vendor"]["id"],
        [{"cost_code_id": sold_job["electrical"]["id"], "amount": "20000.00"}],
    )
    assert po["status"] == "draft"
    assert Decimal(po["total"]) == Decimal("20000.00")

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["committed_total"]) == Decimal("0")


async def test_issuing_a_po_commits_the_money(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    po = await _po(
        client, h, job_id, sold_job["vendor"]["id"],
        [{"cost_code_id": sold_job["electrical"]["id"], "amount": "20000.00"}],
    )
    issued = await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)
    assert issued["status"] == "issued"
    assert issued["issued_date"] is not None

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["revised_total"]) == Decimal("80000.00")
    assert Decimal(cost["committed_total"]) == Decimal("20000.00")
    assert Decimal(cost["uncommitted_total"]) == Decimal("60000.00")
    assert cost["over_budget"] is False

    electrical = next(
        line for line in cost["lines"] if line["cost_code_id"] == sold_job["electrical"]["id"]
    )
    assert Decimal(electrical["committed_amount"]) == Decimal("20000.00")
    assert Decimal(electrical["uncommitted"]) == Decimal("2000.00")  # 22,000 - 20,000


async def test_po_over_budget_is_refused(client, sold_job):
    """Electrical is budgeted at 22,000. A 25,000 PO must not go out quietly."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    po = await _po(
        client, h, job_id, sold_job["vendor"]["id"],
        [{"cost_code_id": sold_job["electrical"]["id"], "amount": "25000.00"}],
    )
    resp = await client.post(f"/api/v1/purchase-orders/{po['id']}/issue", headers=h)
    assert resp.status_code == 409
    assert "remaining budget" in resp.json()["detail"]

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["committed_total"]) == Decimal("0")


async def test_over_budget_can_be_overridden_deliberately(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    po = await _po(
        client, h, job_id, sold_job["vendor"]["id"],
        [{"cost_code_id": sold_job["electrical"]["id"], "amount": "25000.00"}],
    )
    issued = await post(
        client, h,
        f"/api/v1/purchase-orders/{po['id']}/issue?allow_over_budget=true", None, expect=200,
    )
    assert issued["status"] == "issued"

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert cost["over_budget"] is False  # still under the 80,000 job total
    electrical = next(
        line for line in cost["lines"] if line["cost_code_id"] == sold_job["electrical"]["id"]
    )
    assert Decimal(electrical["uncommitted"]) == Decimal("-3000.00")  # over on this code


async def test_a_confirmed_selection_raises_the_ceiling_for_purchasing(client, sold_job):
    """The cascade is what lets purchasing legitimately commit more money."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    # 25,000 on electrical is over the 22,000 budget...
    po = await _po(
        client, h, job_id, sold_job["vendor"]["id"],
        [{"cost_code_id": sold_job["electrical"]["id"], "amount": "23000.00"}],
    )
    blocked = await client.post(f"/api/v1/purchase-orders/{po['id']}/issue", headers=h)
    assert blocked.status_code == 409

    # ...until the buyer confirms the upgrade, which adds 1,200 to electrical.
    selection = await post(
        client, h, f"/api/v1/jobs/{job_id}/selections", {"option_id": sold_job["option"]["id"]}
    )
    await post(
        client, h, f"/api/v1/jobs/{job_id}/selections/{selection['id']}/confirm", None, expect=200
    )

    issued = await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)
    assert issued["status"] == "issued"


async def test_voiding_a_po_releases_the_commitment(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    po = await _po(
        client, h, job_id, sold_job["vendor"]["id"],
        [{"cost_code_id": sold_job["electrical"]["id"], "amount": "20000.00"}],
    )
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/void", None, expect=200)

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["committed_total"]) == Decimal("0")


async def test_a_po_cannot_be_issued_twice(client, sold_job):
    h = sold_job["headers"]
    po = await _po(
        client, h, sold_job["job"]["id"], sold_job["vendor"]["id"],
        [{"cost_code_id": sold_job["electrical"]["id"], "amount": "1000.00"}],
    )
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)
    resp = await client.post(f"/api/v1/purchase-orders/{po['id']}/issue", headers=h)
    assert resp.status_code == 409
