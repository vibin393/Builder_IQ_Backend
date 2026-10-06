"""House plans: UK specification, standard costs that can be corrected, elevations,
price history, copying, archiving, and the pricing guide a sale is written from."""

from decimal import Decimal

from tests.conftest import post

API = "/api/v1"


async def test_either_measure_fills_the_other_and_specification_saves(client, builder):
    h = builder["headers"]
    plan = await post(
        client, h, f"{API}/plans",
        {"code": "b1", "name": "The Birch", "floor_area_sqm": "100", "bedrooms": 3,
         "bathrooms": "2", "en_suites": 1, "has_cloakroom": True, "house_type": "semi_detached",
         "storeys": 2, "garage": "single", "parking_spaces": 2, "epc_target": "b",
         "base_price": "325000"},
    )
    assert plan["code"] == "B1" and plan["epc_target"] == "B"
    assert plan["square_feet"] == 1076                     # 100 m² in sq ft
    r = await client.patch(f"{API}/plans/{plan['id']}", headers=h, json={"square_feet": 2000})
    assert Decimal(r.json()["floor_area_sqm"]) == Decimal("185.8")


async def test_standard_costs_can_be_corrected_and_carry_hours_to_the_budget(
    client, builder, db_session
):
    """Lines could only ever be added, and their hours never reached a budget."""
    h = builder["headers"]
    plan = builder["plan"]
    lines = (await client.get(f"{API}/plans/{plan['id']}/cost-lines", headers=h)).json()
    framing = next(ln for ln in lines if ln["cost_code_id"] == builder["framing"]["id"])

    fixed = await client.patch(
        f"{API}/plan-cost-lines/{framing['id']}", headers=h,
        json={"amount": "60000.00", "labour_hours": "240"},
    )
    assert fixed.status_code == 200 and fixed.json()["labour_hours"] == "240.00"

    dup = await client.post(
        f"{API}/plans/{plan['id']}/cost-lines", headers=h,
        json={"cost_code_id": builder["framing"]["id"], "amount": "1.00"},
    )
    assert dup.status_code == 409                          # was a 500 from the unique index

    job = await post(
        client, h, f"{API}/jobs",
        {"community_id": builder["community"]["id"], "lot_id": builder["lot"]["id"],
         "plan_id": plan["id"]},
    )
    budget = await post(client, h, f"{API}/jobs/{job['id']}/budget", None)
    line = next(ln for ln in budget["lines"] if ln["cost_code_id"] == builder["framing"]["id"])
    assert Decimal(line["original_amount"]) == Decimal("60000.00")
    # The target the labour report measures booked hours against.
    import uuid

    from app.models.budget import BudgetLine

    row = await db_session.get(BudgetLine, uuid.UUID(line["id"]))
    assert row.budgeted_hours == Decimal("240")

    elec = next(ln for ln in lines if ln["cost_code_id"] == builder["electrical"]["id"])
    r = await client.delete(f"{API}/plan-cost-lines/{elec['id']}", headers=h)
    assert r.status_code == 204
    after = (await client.get(f"{API}/plans/{plan['id']}", headers=h)).json()
    assert after["stats"]["cost_lines"] == 1
    assert Decimal(after["stats"]["standard_cost"]) == Decimal("60000.00")


async def test_price_changes_are_logged_with_who_and_why(client, builder):
    h = builder["headers"]
    plan = builder["plan"]
    r = await client.patch(
        f"{API}/plans/{plan['id']}", headers=h,
        json={"base_price": "429950.00", "price_reason": "Q4 price review"},
    )
    assert r.status_code == 200
    await client.patch(f"{API}/plans/{plan['id']}", headers=h, json={"name": "The Maple II"})
    history = (await client.get(f"{API}/plans/{plan['id']}/price-history", headers=h)).json()
    assert len(history) == 1                               # a name change is not a price change
    assert history[0]["old_price"] == "420000.00" and history[0]["new_price"] == "429950.00"
    assert history[0]["reason"] == "Q4 price review" and history[0]["changed_by_id"]

    s = (await client.get(f"{API}/plans/summary", headers=h)).json()
    assert s["price_changes_90_days"] == 1


async def test_elevations_are_enforced_once_a_plan_has_them(client, builder):
    h = builder["headers"]
    plan = builder["plan"]
    base = {"community_id": builder["community"]["id"], "lot_id": builder["lot"]["id"],
            "plan_id": plan["id"]}

    await post(client, h, f"{API}/plans/{plan['id']}/elevations", {"code": "a", "name": "Brick"})
    await post(
        client, h, f"{API}/plans/{plan['id']}/elevations",
        {"code": "B", "name": "Render", "price_adjustment": "4500.00"},
    )
    dup = await client.post(
        f"{API}/plans/{plan['id']}/elevations", headers=h, json={"code": "A", "name": "Again"}
    )
    assert dup.status_code == 409

    bad = await client.post(f"{API}/jobs", headers=h, json={**base, "elevation": "Z"})
    assert bad.status_code in (409, 422) and "A, B" in bad.text

    job = await post(client, h, f"{API}/jobs", {**base, "elevation": " b "})
    assert job["elevation"] == "B"

    els = (await client.get(f"{API}/plans/{plan['id']}/elevations", headers=h)).json()
    render = next(e for e in els if e["code"] == "B")
    assert render["homes"] == 1
    r = await client.delete(f"{API}/plan-elevations/{render['id']}", headers=h)
    assert r.status_code == 409                            # a home is built with it
    r = await client.patch(f"{API}/plan-elevations/{render['id']}", headers=h, json={"code": "C"})
    assert r.status_code == 409                            # would detach the home


async def test_pricing_guide_builds_the_price_up(client, builder):
    h = builder["headers"]
    plan = builder["plan"]
    c = builder["community"]
    await post(
        client, h, f"{API}/plans/{plan['id']}/elevations",
        {"code": "B", "name": "Render", "price_adjustment": "4500.00"},
    )
    await client.put(
        f"{API}/communities/{c['id']}/plans", headers=h,
        json={"plan_id": plan["id"], "base_price": "435000.00"},
    )
    g = (
        await client.get(
            f"{API}/plans/{plan['id']}/pricing?community_id={c['id']}&elevation=b"
            f"&lot_id={builder['lot']['id']}",
            headers=h,
        )
    ).json()
    assert Decimal(g["house_price"]) == Decimal("435000.00")    # the development's price
    assert Decimal(g["elevation_adjustment"]) == Decimal("4500.00")
    assert Decimal(g["plot_premium"]) == Decimal("15000.00")
    assert Decimal(g["base_price"]) == Decimal("439500.00")
    assert Decimal(g["total"]) == Decimal("454500.00")

    plain = (await client.get(f"{API}/plans/{plan['id']}/pricing", headers=h)).json()
    assert Decimal(plain["total"]) == Decimal("420000.00")


async def test_copy_brings_costs_and_elevations(client, builder):
    h = builder["headers"]
    plan = builder["plan"]
    await post(client, h, f"{API}/plans/{plan['id']}/elevations", {"code": "A", "name": "Brick"})
    copy = await post(
        client, h, f"{API}/plans/{plan['id']}/copy", {"code": "P3G", "name": "The Maple + garage"}
    )
    assert copy["stats"]["cost_lines"] == 2 and copy["stats"]["elevations"] == 1
    assert copy["base_price"] == plan["base_price"]
    taken = await client.post(
        f"{API}/plans/{plan['id']}/copy", headers=h, json={"code": "p3", "name": "Clash"}
    )
    assert taken.status_code == 409


async def test_an_archived_plan_takes_no_new_homes(client, builder):
    h = builder["headers"]
    plan = builder["plan"]
    await post(client, h, f"{API}/plans/{plan['id']}/archive", None, expect=200)
    r = await client.post(
        f"{API}/jobs", headers=h,
        json={"community_id": builder["community"]["id"], "lot_id": builder["lot"]["id"],
              "plan_id": plan["id"]},
    )
    assert r.status_code in (409, 422) and "archived" in r.text
    active = (await client.get(f"{API}/plans?active=true", headers=h)).json()
    assert all(p["id"] != plan["id"] for p in active)
    await post(client, h, f"{API}/plans/{plan['id']}/restore", None, expect=200)


async def test_margin_and_summary(client, builder):
    h = builder["headers"]
    thin = await post(
        client, h, f"{API}/plans", {"code": "T1", "name": "Thin", "base_price": "100000"}
    )
    await post(
        client, h, f"{API}/plans/{thin['id']}/cost-lines",
        {"cost_code_id": builder["framing"]["id"], "amount": "95000"},
    )
    await post(client, h, f"{API}/plans", {"code": "E1", "name": "Empty", "base_price": "1"})

    maple = (await client.get(f"{API}/plans/{builder['plan']['id']}", headers=h)).json()
    assert Decimal(maple["stats"]["margin"]) == Decimal("340000.00")      # 420k - 80k
    assert Decimal(maple["stats"]["margin_pct"]) == Decimal("81.0")

    s = (await client.get(f"{API}/plans/summary", headers=h)).json()
    assert s["active"] == 3
    assert s["low_margin"] == 1                                          # Thin, at 5%
    assert s["no_costs"] == 1                                            # Empty
