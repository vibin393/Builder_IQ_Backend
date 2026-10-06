"""End-to-end test of the option cascade - the property the whole product rests on.

One buyer decision must move the contract price, the job budget (per cost code) and the
construction schedule together. If these can drift apart, the system is just a set of
disconnected screens.
"""

from decimal import Decimal

from tests.conftest import post

# --- The cascade --------------------------------------------------------------


async def test_budget_is_generated_from_plan_standard_costs(client, sold_job):
    h = sold_job["headers"]
    budget = (await client.get(f"/api/v1/jobs/{sold_job['job']['id']}/budget", headers=h)).json()

    assert Decimal(budget["original_total"]) == Decimal("80000.00")  # 58,000 + 22,000
    assert Decimal(budget["revised_total"]) == Decimal("80000.00")
    assert len(budget["lines"]) == 2


async def test_contract_total_reflects_price_components(client, sold_job):
    h = sold_job["headers"]
    contract = (
        await client.get(f"/api/v1/jobs/{sold_job['job']['id']}/contract", headers=h)
    ).json()

    # 420,000 base + 15,000 premium + 0 options - 8,000 incentive
    assert Decimal(contract["total_price"]) == Decimal("427000.00")
    assert Decimal(contract["options_total"]) == Decimal("0")


async def test_confirming_a_selection_moves_contract_budget_and_schedule(client, sold_job):
    """The core assertion: one decision, three systems, one transaction."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    selection = await post(
        client, h, f"/api/v1/jobs/{job_id}/selections",
        {"option_id": sold_job["option"]["id"], "quantity": 1},
    )

    # Snapshotted at selection time, not read live from the catalog.
    assert Decimal(selection["price_at_selection"]) == Decimal("8500.00")
    assert Decimal(selection["cost_at_selection"]) == Decimal("5200.00")
    assert selection["status"] == "pending"

    cascade = await post(
        client, h, f"/api/v1/jobs/{job_id}/selections/{selection['id']}/confirm", None, expect=200
    )

    assert Decimal(cascade["price_delta"]) == Decimal("8500.00")
    assert Decimal(cascade["cost_delta"]) == Decimal("5200.00")
    assert cascade["schedule_days_delta"] == 3
    assert len(cascade["budget_changes"]) == 2

    # 1. Contract price rose by exactly the option price.
    contract = (await client.get(f"/api/v1/jobs/{job_id}/contract", headers=h)).json()
    assert Decimal(contract["options_total"]) == Decimal("8500.00")
    assert Decimal(contract["total_price"]) == Decimal("435500.00")

    # 2. Budget rose by exactly the option cost, on the right cost codes.
    budget = (await client.get(f"/api/v1/jobs/{job_id}/budget", headers=h)).json()
    assert Decimal(budget["original_total"]) == Decimal("80000.00")  # untouched
    assert Decimal(budget["revised_total"]) == Decimal("85200.00")   # +5,200

    by_code = {line["cost_code_id"]: line for line in budget["lines"]}
    assert Decimal(by_code[sold_job["framing"]["id"]]["revised_amount"]) == Decimal("62000.00")
    assert Decimal(by_code[sold_job["electrical"]["id"]]["revised_amount"]) == Decimal("23200.00")

    # 3. Schedule slipped by the option's duration.
    job = (await client.get(f"/api/v1/jobs/{job_id}", headers=h)).json()
    assert job["projected_completion"] == "2026-06-04"  # 1 June + 3 days


async def test_cancelling_a_confirmed_selection_reverses_everything(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    selection = await post(
        client, h, f"/api/v1/jobs/{job_id}/selections", {"option_id": sold_job["option"]["id"]}
    )
    await post(
        client, h, f"/api/v1/jobs/{job_id}/selections/{selection['id']}/confirm", None, expect=200
    )
    await post(
        client, h, f"/api/v1/jobs/{job_id}/selections/{selection['id']}/cancel", None, expect=200
    )

    contract = (await client.get(f"/api/v1/jobs/{job_id}/contract", headers=h)).json()
    budget = (await client.get(f"/api/v1/jobs/{job_id}/budget", headers=h)).json()
    job = (await client.get(f"/api/v1/jobs/{job_id}", headers=h)).json()

    assert Decimal(contract["options_total"]) == Decimal("0")
    assert Decimal(budget["revised_total"]) == Decimal("80000.00")
    assert job["projected_completion"] == "2026-06-01"


async def test_price_snapshot_survives_a_catalog_price_change(client, sold_job):
    """A later catalog price change must not alter an existing selection."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    selection = await post(
        client, h, f"/api/v1/jobs/{job_id}/selections", {"option_id": sold_job["option"]["id"]}
    )
    await post(
        client, h, f"/api/v1/jobs/{job_id}/selections/{selection['id']}/confirm", None, expect=200
    )

    # The builder raises the catalog price for future buyers.
    new_option = await post(
        client, h, "/api/v1/options",
        {"code": "KIT-UP-2", "name": "Gourmet kitchen v2", "price": "11000.00"},
    )
    assert Decimal(new_option["price"]) == Decimal("11000.00")

    selections = (await client.get(f"/api/v1/jobs/{job_id}/selections", headers=h)).json()
    assert Decimal(selections[0]["price_at_selection"]) == Decimal("8500.00")

    contract = (await client.get(f"/api/v1/jobs/{job_id}/contract", headers=h)).json()
    assert Decimal(contract["options_total"]) == Decimal("8500.00")


async def test_budget_cannot_be_generated_twice(client, sold_job):
    h = sold_job["headers"]
    resp = await client.post(f"/api/v1/jobs/{sold_job['job']['id']}/budget", headers=h)
    assert resp.status_code == 409


async def test_duplicate_selection_is_rejected(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    await post(
        client, h, f"/api/v1/jobs/{job_id}/selections",
        {"option_id": sold_job["option"]["id"]},
    )
    resp = await client.post(
        f"/api/v1/jobs/{job_id}/selections",
        headers=h,
        json={"option_id": sold_job["option"]["id"]},
    )
    assert resp.status_code == 409
