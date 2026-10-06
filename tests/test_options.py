"""The option catalog: editing, plan availability, quantities, price history, stats -
and the two selection bugs the catalog edits exposed."""

from decimal import Decimal

from tests.conftest import post

API = "/api/v1"


def _sel(job_id):
    return f"{API}/jobs/{job_id}/selections"


async def _budget_total(client, h, job_id) -> Decimal:
    b = (await client.get(f"{API}/jobs/{job_id}/budget", headers=h)).json()
    return Decimal(b["revised_total"])


async def test_cancel_takes_back_exactly_what_confirm_put_on(client, sold_job):
    """Confirm and cancel both read the catalog's *current* lines, so a cost edit in
    between left the budget permanently wrong."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    option = sold_job["option"]
    sel = await post(client, h, _sel(job_id), {"option_id": option["id"]})

    # The catalog changes after the buyer chose it: electrical 1,200 -> 3,000.
    detail = (await client.get(f"{API}/options/{option['id']}", headers=h)).json()
    elec_id = sold_job["electrical"]["id"]
    elec = next(cl for cl in detail["cost_lines"] if cl["cost_code_id"] == elec_id)
    line_url = f"{API}/option-cost-lines/{elec['id']}"
    r = await client.patch(line_url, headers=h, json={"amount": "3000"})
    assert r.status_code == 200

    await post(client, h, f"{_sel(job_id)}/{sel['id']}/confirm", None, expect=200)
    assert await _budget_total(client, h, job_id) == Decimal("85200.00")   # +5,200, as chosen

    # And changes again before the cancel.
    await client.patch(line_url, headers=h, json={"amount": "9000"})
    await post(client, h, f"{_sel(job_id)}/{sel['id']}/cancel", None, expect=200)
    assert await _budget_total(client, h, job_id) == Decimal("80000.00")   # back exactly


async def test_a_retired_option_can_still_be_confirmed_and_cancelled(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    option = sold_job["option"]
    sel = await post(client, h, _sel(job_id), {"option_id": option["id"]})
    await post(client, h, f"{API}/options/{option['id']}/archive", None, expect=200)

    await post(client, h, f"{_sel(job_id)}/{sel['id']}/confirm", None, expect=200)
    await post(client, h, f"{_sel(job_id)}/{sel['id']}/cancel", None, expect=200)

    # But nobody can choose it afresh.
    r = await client.post(_sel(job_id), headers=h, json={"option_id": option["id"]})
    assert r.status_code == 400 and "no longer offered" in r.text


async def test_plan_availability_is_enforced(client, sold_job):
    h = sold_job["headers"]
    other = await post(
        client, h, f"{API}/plans", {"code": "P9", "name": "Other", "base_price": "1"}
    )
    loft = await post(
        client, h, f"{API}/options",
        {"code": "loft", "name": "Loft conversion", "price": "18000", "plan_ids": [other["id"]]},
    )
    assert loft["code"] == "LOFT" and loft["plan_ids"] == [other["id"]]

    r = await client.post(_sel(sold_job["job"]["id"]), headers=h, json={"option_id": loft["id"]})
    assert r.status_code == 400 and "not offered on this house type" in r.text

    # The catalog filtered by plan leaves it out for the Maple, includes it for the other.
    maple = (await client.get(f"{API}/options?plan_id={sold_job['plan']['id']}", headers=h)).json()
    assert all(o["id"] != loft["id"] for o in maple)
    theirs = (await client.get(f"{API}/options?plan_id={other['id']}", headers=h)).json()
    assert any(o["id"] == loft["id"] for o in theirs)

    await client.put(f"{API}/options/{loft['id']}/plans", headers=h, json={"plan_ids": []})
    await post(client, h, _sel(sold_job["job"]["id"]), {"option_id": loft["id"]})   # every plan now


async def test_quantity_limits(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    sockets = await post(
        client, h, f"{API}/options",
        {"code": "SOCK", "name": "Extra double socket", "price": "95", "max_quantity": 20,
         "unit": "each", "category": "electrical"},
    )
    r = await client.post(
        _sel(job_id), headers=h, json={"option_id": sockets["id"], "quantity": 25}
    )
    assert r.status_code == 422 and "1 to 20" in r.text
    ok = await post(client, h, _sel(job_id), {"option_id": sockets["id"], "quantity": 6})
    assert ok["quantity"] == 6

    kitchen = sold_job["option"]                        # max 1
    r = await client.post(
        _sel(job_id), headers=h, json={"option_id": kitchen["id"], "quantity": 2}
    )
    assert r.status_code == 422 and "once" in r.text


async def test_edit_price_history_and_duplicate_cost_code(client, sold_job):
    h = sold_job["headers"]
    option = sold_job["option"]
    r = await client.patch(
        f"{API}/options/{option['id']}", headers=h,
        json={"price": "9250", "price_reason": "Supplier increase", "category": "kitchen"},
    )
    assert r.status_code == 200 and r.json()["category"] == "kitchen"
    hist = (await client.get(f"{API}/options/{option['id']}/price-history", headers=h)).json()
    assert len(hist) == 1 and hist[0]["reason"] == "Supplier increase"

    dup = await client.post(
        f"{API}/options/{option['id']}/cost-lines", headers=h,
        json={"cost_code_id": sold_job["framing"]["id"], "amount": "1"},
    )
    assert dup.status_code == 409


async def test_copy_and_stats(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    option = sold_job["option"]
    copy = await post(
        client, h, f"{API}/options/{option['id']}/copy", {"code": "KIT-UP2", "name": "Kitchen II"}
    )
    assert copy["stats"]["cost_lines"] == 2 and Decimal(copy["total_cost"]) == Decimal("5200.00")

    sel = await post(client, h, _sel(job_id), {"option_id": option["id"]})
    await post(client, h, f"{_sel(job_id)}/{sel['id']}/confirm", None, expect=200)

    d = (await client.get(f"{API}/options/{option['id']}", headers=h)).json()
    assert d["stats"]["confirmed"] == 1
    assert Decimal(d["stats"]["revenue"]) == Decimal("8500.00")
    assert Decimal(d["stats"]["margin"]) == Decimal("3300.00")          # 8,500 - 5,200
    assert Decimal(d["stats"]["attach_rate_pct"]) == Decimal("100.0")   # the one sold home

    used = (await client.get(f"{API}/options/{option['id']}/selections", headers=h)).json()
    assert used[0]["job_number"] == sold_job["job"]["job_number"]

    s = (await client.get(f"{API}/options/summary", headers=h)).json()
    assert s["active"] == 2
    assert s["top"][0]["code"] == "KIT-UP"
    assert Decimal(s["confirmed_revenue"]) == Decimal("8500.00")


async def test_a_priced_option_with_no_costs_is_flagged(client, builder):
    h = builder["headers"]
    await post(client, h, f"{API}/options", {"code": "FREE", "name": "Free money", "price": "5000"})
    s = (await client.get(f"{API}/options/summary", headers=h)).json()
    assert s["no_costs"] == 1
