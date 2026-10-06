"""Cost codes as a ledger: money per code, usage, import, and the archive guard."""

from decimal import Decimal

from tests.conftest import post

API = "/api/v1"


async def test_a_code_on_a_plans_standard_costs_cannot_be_archived(client, builder):
    """The guard looked only at budgets and POs; a plan's code slipped through."""
    h = builder["headers"]
    code_url = f"{API}/cost-codes/{builder['framing']['id']}"
    r = await client.post(f"{code_url}/archive", headers=h, json={})
    assert r.status_code in (409, 422) and "plan standard cost" in r.text

    usage = (await client.get(f"{code_url}/usage", headers=h)).json()
    assert {"where": "Plan standard costs", "count": 1} in usage
    assert any(u["where"] == "Option cost lines" for u in usage)


async def test_money_per_code_across_homes(client, sold_job):
    h = sold_job["headers"]
    stats = (await client.get(f"{API}/cost-codes/stats", headers=h)).json()
    framing = stats[sold_job["framing"]["id"]]
    assert Decimal(framing["budget_revised"]) == Decimal("58000.00")
    assert framing["homes"] == 1 and framing["plans"] == 1

    po = await post(
        client, h, f"{API}/purchase-orders",
        {"job_id": sold_job["job"]["id"], "vendor_id": sold_job["vendor"]["id"],
         "lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "60000.00"}]},
    )
    await post(
        client, h,
        f"{API}/purchase-orders/{po['id']}/issue?allow_over_budget=true&skip_compliance=true",
        None, expect=200,
    )
    stats = (await client.get(f"{API}/cost-codes/stats", headers=h)).json()
    framing = stats[sold_job["framing"]["id"]]
    assert Decimal(framing["committed"]) == Decimal("60000.00")
    assert Decimal(framing["remaining"]) == Decimal("-2000.00")

    s = (await client.get(f"{API}/cost-codes/summary", headers=h)).json()
    assert s["over_budget"] == 1


async def test_import_skips_existing_codes(client, builder):
    h = builder["headers"]
    r = await post(
        client, h, f"{API}/cost-codes/import",
        {"rows": [
            {"code": "06-100", "name": "Already here"},
            {"code": "11-100", "name": "Groundworks", "division": "Substructure",
             "cost_type": "subcontract"},
            {"code": "11-200", "name": "Scaffold hire", "cost_type": "plant"},
        ]},
    )
    assert r["created"] == 2 and r["skipped"] == ["06-100"]
    codes = {c["code"]: c for c in (await client.get(f"{API}/cost-codes", headers=h)).json()}
    assert codes["11-100"]["cost_type"] == "subcontract"

    s = (await client.get(f"{API}/cost-codes/summary", headers=h)).json()
    assert s["by_type"]["plant"] == 1 and s["unused"] >= 2


async def test_cost_type_can_be_set_and_the_stats_path_is_not_swallowed(client, builder):
    h = builder["headers"]
    r = await client.patch(
        f"{API}/cost-codes/{builder['electrical']['id']}", headers=h,
        json={"cost_type": "subcontract"},
    )
    assert r.status_code == 200 and r.json()["cost_type"] == "subcontract"
    # /cost-codes/summary must reach its own route, not /cost-codes/{code_id}.
    assert (await client.get(f"{API}/cost-codes/summary", headers=h)).status_code == 200
