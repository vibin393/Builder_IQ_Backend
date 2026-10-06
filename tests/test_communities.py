"""Developments: status, phases and releases, plans offered, plots and their holds.

A plot's status is its sales position. Starting a home does not change it - that used to
set the plot to "held" and take every speculative home off the market.
"""

from datetime import date, timedelta

from tests.conftest import post

API = "/api/v1"


async def _community(client, h, code="ASH", **extra):
    return await post(
        client, h, f"{API}/communities",
        {"code": code, "name": f"{code} Park", "city": "Wimborne", "county": "Dorset", **extra},
    )


async def _lots(client, h, cid, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = await client.get(f"{API}/communities/{cid}/lots?{q}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_spec_home_stays_on_sale_and_a_sale_marks_it_sold(client, builder):
    h = builder["headers"]
    job = await post(
        client, h, f"{API}/jobs",
        {
            "community_id": builder["community"]["id"],
            "lot_id": builder["lot"]["id"],
            "plan_id": builder["plan"]["id"],
        },
    )
    [lot] = await _lots(client, h, builder["community"]["id"])
    assert lot["status"] == "available"        # was "held" - off the market
    assert lot["is_spec"] is True
    assert lot["job_number"] == job["job_number"]

    buildable = await _lots(client, h, builder["community"]["id"], buildable="true")
    assert buildable == []                     # a home is already on it

    await post(
        client, h, f"{API}/jobs/{job['id']}/contract",
        {"buyer": {"first_name": "Ana", "last_name": "Silva"}, "base_price": "420000.00"},
    )
    [lot] = await _lots(client, h, builder["community"]["id"])
    assert lot["status"] == "sold" and lot["is_spec"] is False

    c = (await client.get(f"{API}/communities/{builder['community']['id']}", headers=h)).json()
    assert c["stats"]["sales_90_days"] == 1
    assert c["stats"]["spec_homes"] == 0


async def test_an_unreleased_plot_cannot_be_sold(client, builder):
    h = builder["headers"]
    c = builder["community"]
    lot = await post(
        client, h, f"{API}/communities/{c['id']}/lots",
        {"lot_number": "48", "status": "not_released"},
    )
    job = await post(
        client, h, f"{API}/jobs",
        {"community_id": c["id"], "lot_id": lot["id"], "plan_id": builder["plan"]["id"]},
    )
    r = await client.post(
        f"{API}/jobs/{job['id']}/contract", headers=h,
        json={"buyer": {"first_name": "A", "last_name": "B"}, "base_price": "1.00"},
    )
    assert r.status_code == 409 and "released" in r.json()["detail"]


async def test_phases_release_their_plots_and_open_the_development(client, builder):
    h = builder["headers"]
    c = await _community(client, h, status="planning")
    phase = await post(client, h, f"{API}/communities/{c['id']}/phases", {"name": "Phase 1"})
    assert phase["sequence"] == 1

    made = await post(
        client, h, f"{API}/communities/{c['id']}/lots/bulk",
        {"start": 1, "end": 12, "phase_id": phase["id"]},
    )
    assert made["created"] == 12
    again = await post(
        client, h, f"{API}/communities/{c['id']}/lots/bulk", {"start": 10, "end": 14},
    )
    assert again["created"] == 2 and again["skipped"] == ["10", "11", "12"]

    lots = await _lots(client, h, c["id"])
    assert [lot["lot_number"] for lot in lots][:3] == ["1", "2", "3"]   # 10 not before 2
    assert all(lot["status"] == "not_released" for lot in lots)

    released = await post(client, h, f"{API}/phases/{phase['id']}/release", None, expect=200)
    assert released["released_on"] == date.today().isoformat()
    assert released["plots"]["available"] == 12

    after = (await client.get(f"{API}/communities/{c['id']}", headers=h)).json()
    assert after["status"] == "selling" and after["opened_on"] == date.today().isoformat()
    assert after["stats"]["plots"]["available"] == 12
    assert after["stats"]["plots"]["not_released"] == 2

    r = await client.post(f"{API}/phases/{phase['id']}/release", headers=h)
    assert r.status_code == 409                # nothing left to release
    r = await client.delete(f"{API}/phases/{phase['id']}", headers=h)
    assert r.status_code == 409                # plots are in it


async def test_a_hold_has_an_end_date_and_lapses_by_itself(client, builder, db_session):
    h = builder["headers"]
    c = builder["community"]
    lead = await post(
        client, h, f"{API}/leads",
        {"first_name": "Jo", "last_name": "Nkemdirim", "source": "walk_in"},
    )
    lot = builder["lot"]
    today = date.today()

    too_long = await client.post(
        f"{API}/lots/{lot['id']}/hold", headers=h,
        json={"until": (today + timedelta(days=120)).isoformat()},
    )
    assert too_long.status_code == 422

    held = await post(
        client, h, f"{API}/lots/{lot['id']}/hold",
        {"until": (today + timedelta(days=5)).isoformat(), "reason": "Mortgage in principle",
         "lead_id": lead["id"]},
        expect=200,
    )
    assert held["status"] == "held"
    assert held["held_for_name"] == "Jo Nkemdirim"
    assert held["hold_days_left"] == 5

    s = (await client.get(f"{API}/communities/summary", headers=h)).json()
    assert s["holds_expiring_soon"] == 1

    r = await client.post(f"{API}/lots/{lot['id']}/withdraw", headers=h)
    assert r.status_code == 409                # lift the hold first

    freed = await post(client, h, f"{API}/lots/{lot['id']}/unhold", None, expect=200)
    assert freed["status"] == "available" and freed["held_for_lead_id"] is None

    # Put it on a hold that ended yesterday, as if nobody had looked since.
    await post(
        client, h, f"{API}/lots/{lot['id']}/hold",
        {"until": today.isoformat()}, expect=200,
    )
    import uuid

    from app.models.community import Lot

    row = await db_session.get(Lot, uuid.UUID(lot["id"]))
    row.hold_until = today - timedelta(days=1)
    await db_session.commit()

    [after] = await _lots(client, h, c["id"])
    assert after["status"] == "available" and after["hold_until"] is None


async def test_withdraw_and_release_a_single_plot(client, builder):
    h = builder["headers"]
    lot = builder["lot"]
    out = await post(client, h, f"{API}/lots/{lot['id']}/withdraw", None, expect=200)
    assert out["status"] == "not_released"
    back = await post(client, h, f"{API}/lots/{lot['id']}/release", None, expect=200)
    assert back["status"] == "available" and back["released_on"] is not None


async def test_a_sold_plot_keeps_its_premium(client, sold_job):
    h = sold_job["headers"]
    lot = sold_job["lot"]
    r = await client.patch(f"{API}/lots/{lot['id']}", headers=h, json={"premium": "1.00"})
    assert r.status_code == 409
    r = await client.patch(
        f"{API}/lots/{lot['id']}", headers=h, json={"notes": "South-facing garden"}
    )
    assert r.status_code == 200 and r.json()["notes"] == "South-facing garden"


async def test_status_changes_are_checked_against_the_plots(client, sold_job):
    h = sold_job["headers"]
    c = sold_job["community"]
    extra = await post(client, h, f"{API}/communities/{c['id']}/lots", {"lot_number": "48"})

    r = await client.patch(f"{API}/communities/{c['id']}", headers=h, json={"status": "sold_out"})
    assert r.status_code == 409 and "still for sale" in r.json()["detail"]

    await post(client, h, f"{API}/lots/{extra['id']}/withdraw", None, expect=200)
    ok = await client.patch(f"{API}/communities/{c['id']}", headers=h, json={"status": "sold_out"})
    assert ok.status_code == 200

    r = await client.patch(f"{API}/communities/{c['id']}", headers=h, json={"status": "closed"})
    assert r.status_code == 409 and "handed over" in r.json()["detail"]


async def test_plans_offered_at_a_community_price(client, builder):
    h = builder["headers"]
    c = builder["community"]
    plan = builder["plan"]
    r = await client.put(
        f"{API}/communities/{c['id']}/plans", headers=h,
        json={"plan_id": plan["id"], "base_price": "445000.00"},
    )
    assert r.status_code == 200, r.text
    [row] = r.json()
    assert row["effective_price"] == "445000.00" and row["plan_base_price"] == "420000.00"

    r = await client.put(
        f"{API}/communities/{c['id']}/plans", headers=h, json={"plan_id": plan["id"]}
    )
    assert r.json()[0]["effective_price"] == "420000.00"   # blank: the plan's own price

    await post(
        client, h, f"{API}/jobs",
        {"community_id": c["id"], "lot_id": builder["lot"]["id"], "plan_id": plan["id"]},
    )
    r = await client.delete(f"{API}/communities/{c['id']}/plans/{plan['id']}", headers=h)
    assert r.status_code == 409                # a home of it is live here


async def test_edit_community_and_duplicate_codes(client, builder):
    h = builder["headers"]
    await _community(client, h, code="ELM")
    r = await client.post(
        f"{API}/communities", headers=h, json={"code": "elm", "name": "Another Elm"}
    )
    assert r.status_code == 409

    c = builder["community"]
    r = await client.patch(
        f"{API}/communities/{c['id']}", headers=h,
        json={"local_authority": "BCP Council", "planned_plots": 120, "postcode": "bh15 2aa",
              "target_sales_per_month": "4"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["local_authority"] == "BCP Council" and body["postcode"] == "BH15 2AA"
    assert body["stats"]["plots"]["total"] == 1


async def test_sales_can_hold_but_not_lay_out_plots(client, builder):
    h = builder["headers"]
    await post(
        client, h, f"{API}/users",
        {"email": "sales@corman.example.com", "full_name": "Sales", "password": "supersecret123",
         "role": "sales"},
    )
    login = await client.post(
        f"{API}/auth/login",
        json={"email": "sales@corman.example.com", "password": "supersecret123"},
    )
    sh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    held = await client.post(
        f"{API}/lots/{builder['lot']['id']}/hold", headers=sh,
        json={"until": (date.today() + timedelta(days=3)).isoformat()},
    )
    assert held.status_code == 200
    r = await client.post(
        f"{API}/communities/{builder['community']['id']}/lots/bulk", headers=sh,
        json={"start": 1, "end": 3},
    )
    assert r.status_code == 403


async def test_the_report_counts_only_homes_with_a_buyer_as_sold(client, sold_job):
    h = sold_job["headers"]
    c = sold_job["community"]
    spare = await post(client, h, f"{API}/communities/{c['id']}/lots", {"lot_number": "49"})
    await post(
        client, h, f"{API}/jobs",
        {"community_id": c["id"], "lot_id": spare["id"], "plan_id": sold_job["plan"]["id"]},
    )
    report = (await client.get(f"{API}/reports/communities", headers=h)).json()
    row = next(r for r in report["communities"] if r["community_id"] == c["id"])
    assert row["jobs"] == 2
    assert row["sold_or_building"] == 1        # the spec home is not sold
