"""Land development: the appraisal, the budget, UK planning and obligations, and
delivering plots to the community."""

from datetime import date
from decimal import Decimal

from tests.conftest import post

API = "/api/v1"


async def _project(client, h, **extra):
    return await post(
        client, h, f"{API}/land-projects",
        {"code": "oak-p2", "name": "Oak Ridge Phase 2", "planned_lots": 100,
         "acquisition_cost": "2000000.00", **extra},
    )


async def test_cost_per_lot_uses_the_forecast(client, builder):
    """Carried over from the original test, plus: an overspend shows at once."""
    h = builder["headers"]
    p = await _project(client, h, acres="10")
    assert p["code"] == "OAK-P2"
    assert Decimal(p["site_area_ha"]) == Decimal("4.05")
    assert Decimal(p["cost_per_lot"]) == Decimal("20000.00")

    p = await post(
        client, h, f"{API}/land-projects/{p['id']}/budget-lines",
        {"category": "Earthworks", "budget_amount": "800000.00"}, expect=200,
    )
    p = await post(
        client, h, f"{API}/land-projects/{p['id']}/budget-lines",
        {"category": "Utilities", "budget_amount": "700000.00"}, expect=200,
    )
    assert Decimal(p["budget_total"]) == Decimal("1500000.00")
    assert Decimal(p["cost_per_lot"]) == Decimal("35000.00")

    earth = next(ln for ln in p["budget_lines"] if ln["category"] == "Earthworks")
    r = await client.patch(
        f"{API}/land-budget-lines/{earth['id']}", headers=h, json={"actual_amount": "900000.00"}
    )
    assert r.status_code == 200 and Decimal(r.json()["variance"]) == Decimal("-100000.00")
    p = (await client.get(f"{API}/land-projects/{p['id']}", headers=h)).json()
    assert Decimal(p["cost_per_lot"]) == Decimal("36000.00")          # forecast, not budget

    r = await client.delete(f"{API}/land-budget-lines/{earth['id']}", headers=h)
    assert r.status_code == 409                                        # money spent on it


async def test_appraisal_margin_on_gdv(client, builder):
    h = builder["headers"]
    p = await _project(client, h, gdv="30000000", build_cost_estimate="20000000")
    # 30m - 2m land - 0 infrastructure - 20m build = 8m, 26.7% on GDV
    assert Decimal(p["profit"]) == Decimal("8000000.00")
    assert Decimal(p["margin_on_gdv"]) == Decimal("26.7")


async def test_works_cannot_start_before_consent_and_pre_commencement(client, builder):
    h = builder["headers"]
    p = await _project(client, h)
    url = f"{API}/land-projects/{p['id']}"

    r = await client.patch(url, headers=h, json={"status": "development"})
    assert r.status_code == 409 and "buildable permission" in r.json()["detail"]

    cond = await post(
        client, h, f"{url}/obligations",
        {"kind": "condition", "reference": "Condition 4", "pre_commencement": True,
         "description": "Construction management plan to be approved"},
    )
    await post(
        client, h, f"{url}/obligations",
        {"kind": "s106", "reference": "Sch 2", "description": "Education contribution",
         "trigger": "Before occupation of the 25th dwelling", "amount": "250000"},
    )

    approved = (await client.patch(url, headers=h, json={"planning_status": "full"})).json()
    assert approved["is_buildable"] is True
    today = date.today()
    assert approved["planning_expires_on"] == today.replace(year=today.year + 3).isoformat()

    r = await client.patch(url, headers=h, json={"status": "development"})
    assert r.status_code == 409 and "Condition 4" in r.json()["detail"]

    await post(client, h, f"{API}/land-obligations/{cond['id']}/submit", None, expect=200)
    done = await post(client, h, f"{API}/land-obligations/{cond['id']}/complete", {}, expect=200)
    assert done["status"] == "discharged"

    started = (await client.patch(url, headers=h, json={"status": "development"})).json()
    assert started["status"] == "development" and started["start_date"] == today.isoformat()

    s = (await client.get(f"{API}/land-projects/summary", headers=h)).json()
    assert s["obligations_open"] == 1 and s["pre_commencement_open"] == 0
    assert Decimal(s["contributions_due"]) == Decimal("250000.00")


async def test_a_payment_obligation_is_paid_not_discharged(client, builder):
    h = builder["headers"]
    p = await _project(client, h)
    o = await post(
        client, h, f"{API}/land-projects/{p['id']}/obligations",
        {"kind": "cil", "description": "CIL instalment 1", "amount": "80000"},
    )
    paid = await post(client, h, f"{API}/land-obligations/{o['id']}/complete", {}, expect=200)
    assert paid["status"] == "paid"
    back = await post(client, h, f"{API}/land-obligations/{o['id']}/reopen", None, expect=200)
    assert back["status"] == "outstanding" and back["completed_on"] is None


async def test_delivering_plots_creates_them_in_the_community(client, builder):
    h = builder["headers"]
    c = builder["community"]
    p = await _project(client, h, community_id=c["id"])
    url = f"{API}/land-projects/{p['id']}/deliver-plots"

    r = await client.post(url, headers=h, json={"start": 1, "end": 10})
    assert r.status_code == 409 and "permission" in r.json()["detail"]

    await client.patch(
        f"{API}/land-projects/{p['id']}", headers=h, json={"planning_status": "reserved_matters"}
    )
    done = await post(client, h, url, {"start": 40, "end": 49}, expect=200)
    # Plot 47 already existed in the builder fixture: skipped, not duplicated.
    assert done["delivered_lots"] == 9
    lots = (await client.get(f"{API}/communities/{c['id']}/lots", headers=h)).json()
    assert sum(1 for lot in lots if lot["status"] == "not_released") == 9
