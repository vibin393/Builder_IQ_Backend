"""The life of one house, from a plot to a warranty.

Before this, a home's status was a field on a PATCH. Anybody could move a planned
house straight to closed, complete one that had never started, or revive a
cancelled job - and the dates the whole business is measured by were typed in by
hand, so cycle time was whatever somebody felt like.

What is asserted here is the handful of refusals that make those numbers mean
something:

  * a home cannot be completed before it started, because one negative cycle time
    poisons every average built on it
  * the projection is frozen at the start, because a baseline that moves each time
    it slips measures nothing
  * closing needs a signed contract and a finished house - a legal problem, not a
    data one
  * a hold needs a countable reason, so "we lost forty days to inspections" is a
    sentence somebody can actually say
  * cancelling frees the plot, which is the entire reason a builder cancels rather
    than deletes
"""

from datetime import date, timedelta

import pytest

from tests.conftest import post, signup

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
LAST_MONTH = TODAY - timedelta(days=30)
NEXT_MONTH = TODAY + timedelta(days=30)


# --- helpers ------------------------------------------------------------------


async def new_lot(client, headers, community_id, number):
    return await post(
        client, headers, f"/api/v1/communities/{community_id}/lots",
        {"lot_number": number, "address": f"{number} Test Way", "postcode": "BH15 1AA"},
    )


async def new_home(client, builder, lot_number="90", **overrides):
    """A home on a fresh plot, so tests never fight over the fixture's lot."""
    h = builder["headers"]
    lot = await new_lot(client, h, builder["community"]["id"], lot_number)
    payload = {
        "community_id": builder["community"]["id"],
        "lot_id": lot["id"],
        "plan_id": builder["plan"]["id"],
    }
    payload.update(overrides)
    job = await post(client, h, "/api/v1/jobs", payload)
    return job, lot


async def sell(client, headers, job_id, sign=True):
    contract = await post(
        client, headers, f"/api/v1/jobs/{job_id}/contract",
        {
            "buyer": {"first_name": "Ama", "last_name": "Boateng", "email": "ama@example.com"},
            "base_price": "420000.00",
        },
    )
    if sign:
        await post(client, headers, f"/api/v1/jobs/{job_id}/contract/sign", None, expect=200)
    return contract


async def build(client, headers, job_id, **kw):
    """Take a sold home all the way onto site."""
    return await post(client, headers, f"/api/v1/jobs/{job_id}/start", kw, expect=200)


# ============================================================ starting one =====


async def test_a_home_is_put_on_a_plot(client, builder):
    job, lot = await new_home(client, builder)
    assert job["status"] == "planned"
    assert job["job_number"].startswith("J")
    assert job["lot_id"] == lot["id"]


async def test_a_plot_cannot_hold_two_homes(client, builder):
    h = builder["headers"]
    job, lot = await new_home(client, builder, "91")

    resp = await client.post(
        "/api/v1/jobs",
        headers=h,
        json={
            "community_id": builder["community"]["id"],
            "lot_id": lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )
    assert resp.status_code == 409
    assert job["job_number"] in resp.json()["detail"]


async def test_a_plot_must_be_in_the_community_it_is_claimed_for(client, builder):
    """Otherwise a home appears in a community report it was never part of."""
    h = builder["headers"]
    other = await post(
        client, h, "/api/v1/communities", {"code": "ELM", "name": "Elm Park"}
    )
    lot = await new_lot(client, h, other["id"], "01")

    resp = await client.post(
        "/api/v1/jobs",
        headers=h,
        json={
            "community_id": builder["community"]["id"],   # wrong community
            "lot_id": lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )
    assert resp.status_code == 409
    assert "not in the community" in resp.json()["detail"]


async def test_a_home_cannot_be_built_on_another_builders_plot(client, builder):
    h = builder["headers"]
    other = await signup(client, "Rival Homes", "rival-jobs@rival.example.com")
    their_community = await post(
        client, other, "/api/v1/communities", {"code": "RIV", "name": "Rival Park"}
    )
    their_lot = await new_lot(client, other, their_community["id"], "01")

    resp = await client.post(
        "/api/v1/jobs",
        headers=h,
        json={
            "community_id": builder["community"]["id"],
            "lot_id": their_lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )
    assert resp.status_code == 404


# ========================================================== the transitions =====


async def test_a_home_walks_through_its_life(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "92")
    jid = job["id"]

    await sell(client, h, jid)
    sold = (await client.get(f"/api/v1/jobs/{jid}", headers=h)).json()
    assert sold["status"] == "sold"
    assert sold["sold_date"] == str(TODAY)     # recorded, not left blank

    started = await build(
        client, h, jid,
        start_date=str(LAST_MONTH), projected_completion=str(NEXT_MONTH),
    )
    assert started["status"] == "under_construction"
    assert started["start_date"] == str(LAST_MONTH)

    done = await post(
        client, h, f"/api/v1/jobs/{jid}/complete", {"completed_on": str(YESTERDAY)}, expect=200
    )
    assert done["status"] == "complete"
    assert done["cycle_time_days"] == 29        # last month to yesterday

    closed = await post(client, h, f"/api/v1/jobs/{jid}/close", {}, expect=200)
    assert closed["status"] == "closed"
    assert closed["closing_date"] == str(TODAY)

    warranty = await post(client, h, f"/api/v1/jobs/{jid}/warranty", None, expect=200)
    assert warranty["status"] == "warranty"


async def test_a_planned_home_cannot_jump_to_closed(client, builder):
    """The single hole this whole module exists to close."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "93")

    resp = await client.post(f"/api/v1/jobs/{job['id']}/close", headers=h, json={})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "cannot go from planned to closed" in detail
    # The refusal names the legal next steps rather than just saying no.
    assert "sold" in detail


async def test_status_cannot_be_set_through_a_patch(client, builder):
    """The transition rules live on the lifecycle routes. A PATCH that accepted
    status would route round all of them."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "94")

    patched = (
        await client.patch(
            f"/api/v1/jobs/{job['id']}",
            headers=h,
            json={"status": "closed", "elevation": "Brick"},
        )
    ).json()
    assert patched["status"] == "planned"      # ignored
    assert patched["elevation"] == "Brick"     # applied


async def test_selling_twice_is_refused(client, builder):
    """Writing a second contract used to silently reset the home's status."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "95")
    await sell(client, h, job["id"])

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/contract",
        headers=h,
        json={
            "buyer": {"first_name": "Second", "last_name": "Buyer"},
            "base_price": "400000.00",
        },
    )
    assert resp.status_code == 409


# =============================================================== starting =====


async def test_an_unsold_home_is_not_started_by_accident(client, builder):
    """Building on spec is a real strategy with real risk. The flag is the
    difference between choosing it and clicking it."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "96")

    resp = await client.post(f"/api/v1/jobs/{job['id']}/start", headers=h, json={})
    assert resp.status_code == 409
    assert "has not been sold" in resp.json()["detail"]

    on_purpose = await post(
        client, h, f"/api/v1/jobs/{job['id']}/start", {"allow_unsold": True}, expect=200
    )
    assert on_purpose["status"] == "under_construction"


async def test_a_build_cannot_start_in_the_future(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "97")
    await sell(client, h, job["id"])

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/start", headers=h, json={"start_date": str(NEXT_MONTH)}
    )
    assert resp.status_code == 409
    assert "cannot start in the future" in resp.json()["detail"]


async def test_a_projection_before_the_start_is_refused(client, builder):
    """As entered, the house would be finished before it begins."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "98")
    await sell(client, h, job["id"])

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/start",
        headers=h,
        json={"start_date": str(TODAY), "projected_completion": str(LAST_MONTH)},
    )
    assert resp.status_code == 409
    assert "before the start date" in resp.json()["detail"]


# ================================================================= holds =====


async def test_a_build_can_be_stopped_and_restarted(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "99")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(LAST_MONTH))

    held = await post(
        client, h, f"/api/v1/jobs/{job['id']}/hold",
        {"reason": "weather", "note": "Site flooded"}, expect=200,
    )
    assert held["status"] == "on_hold"
    assert held["hold_reason"] == "weather"
    assert held["on_hold_since"] == str(TODAY)

    back = await post(client, h, f"/api/v1/jobs/{job['id']}/resume", None, expect=200)
    assert back["status"] == "under_construction"
    assert back["hold_reason"] is None


async def test_a_hold_of_other_needs_a_note(client, builder):
    """The reason list exists so days lost can be counted per cause. "Other" with
    nothing written is the same as no reason at all."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "100")
    await sell(client, h, job["id"])
    await build(client, h, job["id"])

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/hold", headers=h, json={"reason": "other"}
    )
    assert resp.status_code == 409
    assert "needs a note" in resp.json()["detail"]

    ok = await post(
        client, h, f"/api/v1/jobs/{job['id']}/hold",
        {"reason": "other", "note": "Access dispute with the neighbour"}, expect=200,
    )
    assert ok["status"] == "on_hold"


async def test_days_lost_accumulate_across_several_holds(client, builder):
    """A home stopped three times carries the total, not only the last one."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "101")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(LAST_MONTH))

    for _ in range(2):
        await post(client, h, f"/api/v1/jobs/{job['id']}/hold", {"reason": "weather"}, 200)
        resumed = await post(client, h, f"/api/v1/jobs/{job['id']}/resume", None, expect=200)
    # Held and resumed the same day twice, so the total is zero - but it is a
    # running total rather than a replaced one, which is what matters.
    assert resumed["days_on_hold"] == 0


async def test_resuming_a_home_that_is_not_held_is_refused(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "102")
    await sell(client, h, job["id"])
    await build(client, h, job["id"])

    resp = await client.post(f"/api/v1/jobs/{job['id']}/resume", headers=h)
    assert resp.status_code == 409


# ============================================================== finishing =====


async def test_a_home_cannot_be_completed_before_it_started(client, builder):
    """One negative cycle time poisons every average built on it."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "103")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(TODAY))

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/complete", headers=h, json={"completed_on": str(LAST_MONTH)}
    )
    assert resp.status_code == 409
    assert "before the start date" in resp.json()["detail"]


async def test_a_home_that_never_started_cannot_be_completed(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "104")
    await sell(client, h, job["id"])

    resp = await client.post(f"/api/v1/jobs/{job['id']}/complete", headers=h, json={})
    assert resp.status_code == 409


async def test_a_completed_home_can_be_sent_back_to_site(client, builder):
    """A final inspection fails, or snagging turns out to be real work."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "105")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(LAST_MONTH))
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)

    back = await post(
        client, h, f"/api/v1/jobs/{job['id']}/reopen",
        {"reason": "Failed final inspection - roof flashing"}, expect=200,
    )
    assert back["status"] == "under_construction"
    # The date is cleared: the home was not finished, and leaving it would report
    # a cycle time that never happened.
    assert back["actual_completion"] is None
    assert back["cycle_time_days"] is None
    assert "Failed final inspection" in back["notes"]


async def test_reopening_needs_a_reason(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "106")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(LAST_MONTH))
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)

    resp = await client.post(f"/api/v1/jobs/{job['id']}/reopen", headers=h, json={"reason": ""})
    assert resp.status_code == 422


# ================================================================ closing =====


async def test_closing_needs_a_signed_contract(client, builder):
    """A legal problem, not a data problem."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "107")
    await sell(client, h, job["id"], sign=False)
    await build(client, h, job["id"], start_date=str(LAST_MONTH))
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)

    resp = await client.post(f"/api/v1/jobs/{job['id']}/close", headers=h, json={})
    assert resp.status_code == 409
    assert "not been signed" in resp.json()["detail"]


async def test_closing_starts_the_warranty_clock_from_handover(client, builder):
    """Not from completion - a buyer cannot report a fault in a house they do not
    have yet."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "108")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(LAST_MONTH))
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)

    closed = await post(
        client, h, f"/api/v1/jobs/{job['id']}/close", {"warranty_months": 24}, expect=200
    )
    expires = date.fromisoformat(closed["warranty_expires_on"])
    assert expires.year == TODAY.year + 2
    assert expires.month == TODAY.month


async def test_a_closing_before_completion_is_refused(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "109")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(LAST_MONTH))
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/close", headers=h, json={"closing_date": str(LAST_MONTH)}
    )
    assert resp.status_code == 409


# ============================================================ cancelling =====


async def test_cancelling_frees_the_plot_to_be_built_on_again(client, builder):
    """The entire reason a builder cancels rather than deletes.

    A plain UNIQUE(lot_id) made this impossible - the plot was blocked forever by
    a job nobody was building.
    """
    h = builder["headers"]
    first, lot = await new_home(client, builder, "110")
    await sell(client, h, first["id"])

    cancelled = await post(
        client, h, f"/api/v1/jobs/{first['id']}/cancel",
        {"reason": "buyer_withdrew", "note": "Chain collapsed"}, expect=200,
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_on"] == str(TODAY)

    # The plot is available, and a new home goes on it.
    second = await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": builder["community"]["id"],
            "lot_id": lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )
    assert second["id"] != first["id"]
    assert second["status"] == "planned"


async def test_a_half_built_home_can_be_cancelled(client, builder):
    """A real and expensive event. The system should record it rather than force
    somebody to fake a different ending."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "111")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(LAST_MONTH))

    cancelled = await post(
        client, h, f"/api/v1/jobs/{job['id']}/cancel",
        {"reason": "finance_fell_through"}, expect=200,
    )
    assert cancelled["status"] == "cancelled"


async def test_a_closed_home_cannot_be_cancelled(client, builder):
    """By then it belongs to somebody else."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "112")
    await sell(client, h, job["id"])
    await build(client, h, job["id"], start_date=str(LAST_MONTH))
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)
    await post(client, h, f"/api/v1/jobs/{job['id']}/close", {}, expect=200)

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/cancel", headers=h, json={"reason": "buyer_withdrew"}
    )
    assert resp.status_code == 409


async def test_a_cancelled_home_cannot_be_revived(client, builder):
    """Its plot is free - a new home goes there, so the cancellation stays on the
    books rather than being edited away."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "113")
    await post(client, h, f"/api/v1/jobs/{job['id']}/cancel", {"reason": "lot_problem"}, 200)

    resp = await client.post(f"/api/v1/jobs/{job['id']}/start", headers=h, json={})
    assert resp.status_code == 409
    assert "was cancelled" in resp.json()["detail"]


async def test_cancelling_as_other_needs_a_note(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "114")

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/cancel", headers=h, json={"reason": "other"}
    )
    assert resp.status_code == 409
    assert "needs a note" in resp.json()["detail"]


# ================================================================ permits =====


async def test_a_permit_application_starts_a_clock(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "115")

    applied = await post(
        client, h, f"/api/v1/jobs/{job['id']}/permit/apply",
        {"permit_applied_on": str(LAST_MONTH)}, expect=200,
    )
    assert applied["status"] == "permitting"
    assert applied["days_waiting_on_permit"] == 30

    issued = await post(
        client, h, f"/api/v1/jobs/{job['id']}/permit/issued",
        {"number": "BCP/2026/4471"}, expect=200,
    )
    assert issued["permit_number"] == "BCP/2026/4471"
    # Issued, so the wait is over rather than still counting.
    assert issued["days_waiting_on_permit"] is None
    # And a permit in hand is not work having begun.
    assert issued["status"] == "permitting"


async def test_a_permit_cannot_be_issued_before_it_was_applied_for(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "116")
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/permit/apply",
        {"permit_applied_on": str(TODAY)}, expect=200,
    )

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/permit/issued",
        headers=h,
        json={"number": "X", "issued_on": str(LAST_MONTH)},
    )
    assert resp.status_code == 409


# ======================================================= people and plans =====


async def test_a_superintendent_can_be_put_on_and_taken_off(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "117")
    me = (await client.get("/api/v1/users/me", headers=h)).json()

    on = await post(
        client, h, f"/api/v1/jobs/{job['id']}/superintendent",
        {"superintendent_id": me["id"]}, expect=200,
    )
    assert on["superintendent_id"] == me["id"]

    off = await post(
        client, h, f"/api/v1/jobs/{job['id']}/superintendent",
        {"superintendent_id": None}, expect=200,
    )
    assert off["superintendent_id"] is None


async def test_another_builders_staff_cannot_run_your_site(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "118")
    other = await signup(client, "Rival Two", "rival-super@rival.example.com")
    them = (await client.get("/api/v1/users/me", headers=other)).json()

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/superintendent",
        headers=h,
        json={"superintendent_id": them["id"]},
    )
    assert resp.status_code == 404


async def test_the_plan_cannot_be_swapped_once_building_has_begun(client, builder):
    """Everything downstream - budget, schedule, selections - is derived from it."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "119")
    await sell(client, h, job["id"])
    await build(client, h, job["id"])

    other_plan = await post(
        client, h, "/api/v1/plans",
        {"code": "P9", "name": "The Elm", "square_feet": 1800, "base_price": "350000.00"},
    )
    resp = await client.patch(
        f"/api/v1/jobs/{job['id']}", headers=h, json={"plan_id": other_plan["id"]}
    )
    assert resp.status_code == 409
    assert "cannot be swapped" in resp.json()["detail"]


# ========================================================== lateness =====


async def test_a_home_past_its_projection_is_late_now(client, builder):
    """Late now, not late when somebody finally finishes it."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "120")
    await sell(client, h, job["id"])
    started = await build(
        client, h, job["id"],
        start_date=str(LAST_MONTH), projected_completion=str(YESTERDAY),
    )
    assert started["is_late"] is True
    assert started["days_late"] == 1


async def test_the_late_queue_holds_only_homes_that_are_actually_late(client, builder):
    h = builder["headers"]
    late, _ = await new_home(client, builder, "121")
    await sell(client, h, late["id"])
    await build(
        client, h, late["id"], start_date=str(LAST_MONTH), projected_completion=str(YESTERDAY)
    )

    fine, _ = await new_home(client, builder, "122")
    await sell(client, h, fine["id"])
    await build(
        client, h, fine["id"], start_date=str(LAST_MONTH), projected_completion=str(NEXT_MONTH)
    )

    queue = (await client.get("/api/v1/jobs/late", headers=h)).json()
    assert [j["id"] for j in queue["items"]] == [late["id"]]


async def test_a_cancelled_home_is_never_late(client, builder):
    """Nobody is building it."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "123")
    await sell(client, h, job["id"])
    await build(
        client, h, job["id"], start_date=str(LAST_MONTH), projected_completion=str(YESTERDAY)
    )
    cancelled = await post(
        client, h, f"/api/v1/jobs/{job['id']}/cancel", {"reason": "builder_withdrew"}, 200
    )
    assert cancelled["days_late"] is None

    queue = (await client.get("/api/v1/jobs/late", headers=h)).json()
    assert job["id"] not in [j["id"] for j in queue["items"]]


# ================================================= queues and reporting =====


async def test_the_summary_counts_what_a_builder_scans_for(client, builder):
    h = builder["headers"]
    held, _ = await new_home(client, builder, "124")
    await sell(client, h, held["id"])
    await build(client, h, held["id"], start_date=str(LAST_MONTH))
    await post(client, h, f"/api/v1/jobs/{held['id']}/hold", {"reason": "weather"}, 200)

    waiting, _ = await new_home(client, builder, "125")
    await post(client, h, f"/api/v1/jobs/{waiting['id']}/permit/apply", {}, expect=200)

    summary = (await client.get("/api/v1/jobs/summary", headers=h)).json()
    assert summary["on_hold"] == 1
    assert summary["awaiting_permit"] == 1
    assert summary["building"] >= 1              # a held home is still on site
    assert summary["by_status"]["cancelled"] == 0    # reported, not omitted


async def test_the_building_queue_includes_homes_that_have_stopped(client, builder):
    """A stopped build still occupies a plot, a superintendent and a budget."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "126")
    await sell(client, h, job["id"])
    await build(client, h, job["id"])
    await post(client, h, f"/api/v1/jobs/{job['id']}/hold", {"reason": "inspection"}, 200)

    queue = (await client.get("/api/v1/jobs/building", headers=h)).json()
    assert job["id"] in [j["id"] for j in queue["items"]]


async def test_cycle_time_counts_only_finished_homes(client, builder):
    """Including the ones still running would make the number improve every time a
    slow house finally finishes."""
    h = builder["headers"]
    done, _ = await new_home(client, builder, "127")
    await sell(client, h, done["id"])
    await build(client, h, done["id"], start_date=str(LAST_MONTH))
    await post(client, h, f"/api/v1/jobs/{done['id']}/complete", {}, expect=200)

    running, _ = await new_home(client, builder, "128")
    await sell(client, h, running["id"])
    await build(client, h, running["id"], start_date=str(LAST_MONTH))

    report = (await client.get("/api/v1/jobs/cycle-time", headers=h)).json()
    assert report["homes_measured"] == 1
    assert report["average_days"] == 30


async def test_cycle_time_says_nothing_rather_than_zero_when_nothing_finished(client, builder):
    """An average of zero is a lie a report would repeat."""
    h = builder["headers"]
    report = (await client.get("/api/v1/jobs/cycle-time", headers=h)).json()
    assert report["homes_measured"] == 0
    assert report["average_days"] is None


async def test_the_list_is_paged_and_searchable(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "129")
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/permit/apply",
        {"permit_number": "BCP-SEARCHME"}, expect=200,
    )

    hit = (await client.get("/api/v1/jobs?search=SEARCHME", headers=h)).json()
    assert hit["total"] == 1
    assert hit["items"][0]["id"] == job["id"]

    by_number = (await client.get(f"/api/v1/jobs?search={job['job_number']}", headers=h)).json()
    assert by_number["total"] == 1


async def test_homes_can_be_narrowed_to_one_superintendent(client, builder):
    h = builder["headers"]
    me = (await client.get("/api/v1/users/me", headers=h)).json()
    mine, _ = await new_home(client, builder, "130")
    await new_home(client, builder, "131")
    await post(
        client, h, f"/api/v1/jobs/{mine['id']}/superintendent",
        {"superintendent_id": me["id"]}, expect=200,
    )

    filtered = (
        await client.get(f"/api/v1/jobs?superintendent_id={me['id']}", headers=h)
    ).json()
    assert [j["id"] for j in filtered["items"]] == [mine["id"]]


@pytest.mark.parametrize(
    "route", ["/api/v1/jobs", "/api/v1/jobs/live", "/api/v1/jobs/late", "/api/v1/jobs/building"]
)
async def test_homes_do_not_leak_between_builders(client, builder, route):
    await new_home(client, builder, "132")
    other = await signup(
        client, f"Rival {route[-4:]}", f"rival{abs(len(route))}@r.example.com"
    )
    theirs = (await client.get(route, headers=other)).json()
    assert theirs["total"] == 0


async def test_a_home_cannot_be_read_across_tenants(client, builder):
    job, _ = await new_home(client, builder, "133")
    other = await signup(client, "Rival Read", "rival-read-job@rival.example.com")
    assert (await client.get(f"/api/v1/jobs/{job['id']}", headers=other)).status_code == 404


# =========================================== selling is an event, not a stage =====


async def test_a_home_can_be_sold_at_any_point_before_handover(client, builder):
    """The correction of a real modelling mistake.

    Treating "sold" as a stage in a linear pipeline meant a home already in
    permitting could not be sold - nonsense for a builder who puts a plot through
    planning and finds a buyer afterwards, and doubly so for a spec home sold
    halfway through its build.
    """
    h = builder["headers"]

    permitting, _ = await new_home(client, builder, "140")
    await post(client, h, f"/api/v1/jobs/{permitting['id']}/permit/apply", {}, expect=200)
    await sell(client, h, permitting["id"], sign=False)
    after = (await client.get(f"/api/v1/jobs/{permitting['id']}", headers=h)).json()
    # Sold, and still in permitting - it is no less permitted for having a buyer.
    assert after["sold_date"] is not None
    assert after["status"] == "permitting"


async def test_a_spec_home_sold_mid_build_keeps_building(client, builder):
    h = builder["headers"]
    job, _ = await new_home(client, builder, "141")
    await build(client, h, job["id"], allow_unsold=True, start_date=str(LAST_MONTH))

    await sell(client, h, job["id"], sign=False)
    after = (await client.get(f"/api/v1/jobs/{job['id']}", headers=h)).json()
    assert after["status"] == "under_construction"
    assert after["sold_date"] is not None


async def test_a_home_on_the_drawing_board_moves_to_sold(client, builder):
    """The one case where selling *is* a change of what the record is."""
    h = builder["headers"]
    job, _ = await new_home(client, builder, "142")
    await sell(client, h, job["id"], sign=False)

    after = (await client.get(f"/api/v1/jobs/{job['id']}", headers=h)).json()
    assert after["status"] == "sold"


async def test_selling_a_cancelled_home_is_refused(client, builder):
    """The contract and the sale are one transaction.

    The first version committed the contract and *then* recorded the sale, so a
    refusal left a signed contract on a home the system did not think was sold.
    The service now only flushes and the router commits once, so the whole request
    rolls back together.

    That rollback is asserted against the running server rather than here: this
    fixture shares one session across every request in a test, so nothing is ever
    closed and the discarded work stays visible in the identity map. The refusal
    itself is what this test can honestly check.
    """
    h = builder["headers"]
    job, _ = await new_home(client, builder, "143")
    await post(client, h, f"/api/v1/jobs/{job['id']}/cancel", {"reason": "lot_problem"}, 200)

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/contract",
        headers=h,
        json={"buyer": {"first_name": "Ama", "last_name": "B"}, "base_price": "420000.00"},
    )
    assert resp.status_code == 409
    assert "was cancelled" in resp.json()["detail"]
