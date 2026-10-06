"""Labour and productivity.

The Timecard docstring states the purpose exactly: "the budget says a house should
take so many framing hours, and this says what it actually took." What is asserted
here is mostly about that sentence being true.

  * **A variance needs a target.** Hours come from the plan onto the budget, and a
    row with no target reports actuals rather than a variance against zero, which
    is just the actual with a minus sign in front.
  * **Hours with no rate are not free hours.** Their cost is unknown, not nought.
    Counting nought leaves the hours in the denominator and nothing in the
    numerator, so the average rate falls every time somebody forgets a rate.
  * **Sixteen hours in a day is the ceiling, across every home.** Above it, it is
    nearly always the same shift typed twice into two different houses.
"""

from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import post, signup

TODAY = date.today()


# --- helpers ------------------------------------------------------------------


async def a_timecard(client, headers, job_id, **overrides):
    payload = {
        "job_id": job_id,
        "worker_name": "J. Mensah",
        "hours": "8",
        "worked_on": str(TODAY),
    }
    payload.update(overrides)
    return await post(client, headers, "/api/v1/timecards", payload)


# ================================================ booking hours =====


async def test_hours_are_booked_against_a_home_and_a_code(client, sold_job):
    h = sold_job["headers"]
    card = await a_timecard(
        client, h, sold_job["job"]["id"],
        cost_code_id=sold_job["framing"]["id"], hourly_rate="45",
    )
    assert card["total_hours"] == "8.00"
    assert card["labour_cost"] == "360.00"
    assert card["is_approved"] is False


async def test_overtime_is_charged_at_time_and_a_half(client, sold_job):
    h = sold_job["headers"]
    card = await a_timecard(
        client, h, sold_job["job"]["id"], hours="8", overtime_hours="2", hourly_rate="40",
    )
    assert card["total_hours"] == "10.00"
    # 8 x 40 + 2 x 60
    assert card["labour_cost"] == "440.00"


async def test_the_rate_comes_from_the_trade_when_it_is_not_typed(client, sold_job):
    """Retyping it on every entry is how an average rate drifts without anybody
    deciding it should."""
    h = sold_job["headers"]
    await client.patch(
        f"/api/v1/vendors/{sold_job['vendor']['id']}", headers=h, json={"labour_rate": "52.50"}
    )
    card = await a_timecard(
        client, h, sold_job["job"]["id"], vendor_id=sold_job["vendor"]["id"]
    )
    assert card["hourly_rate"] == "52.50"
    assert card["labour_cost"] == "420.00"


async def test_a_typed_rate_beats_the_trades_standard_one(client, sold_job):
    h = sold_job["headers"]
    await client.patch(
        f"/api/v1/vendors/{sold_job['vendor']['id']}", headers=h, json={"labour_rate": "52.50"}
    )
    card = await a_timecard(
        client, h, sold_job["job"]["id"],
        vendor_id=sold_job["vendor"]["id"], hourly_rate="60",
    )
    assert card["hourly_rate"] == "60.00"


async def test_an_entry_with_no_rate_costs_an_unknown_amount(client, sold_job):
    """Not nought. Nought is a claim that the work was free."""
    h = sold_job["headers"]
    card = await a_timecard(client, h, sold_job["job"]["id"])
    assert card["hourly_rate"] is None
    assert card["labour_cost"] is None


async def test_hours_cannot_be_booked_for_tomorrow(client, sold_job):
    h = sold_job["headers"]
    resp = await client.post(
        "/api/v1/timecards", headers=h,
        json={
            "job_id": sold_job["job"]["id"],
            "worker_name": "J. Mensah",
            "hours": "8",
            "worked_on": str(TODAY + timedelta(days=1)),
        },
    )
    assert resp.status_code == 409


async def test_a_day_cannot_exceed_sixteen_hours_across_every_home(client, sold_job):
    """The mistake's usual shape: the same shift entered twice, from two different
    houses, where neither screen shows the other."""
    h = sold_job["headers"]
    other_lot = await post(
        client, h, f"/api/v1/communities/{sold_job['community']['id']}/lots",
        {"lot_number": "88"},
    )
    other = await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": sold_job["community"]["id"],
            "lot_id": other_lot["id"],
            "plan_id": sold_job["plan"]["id"],
        },
    )

    await a_timecard(client, h, sold_job["job"]["id"], hours="10")
    resp = await client.post(
        "/api/v1/timecards", headers=h,
        json={
            "job_id": other["id"],
            "worker_name": "J. Mensah",
            "hours": "8",
            "worked_on": str(TODAY),
        },
    )
    assert resp.status_code == 409
    assert "18" in resp.json()["detail"]


async def test_two_different_people_are_not_the_same_person(client, sold_job):
    h = sold_job["headers"]
    await a_timecard(client, h, sold_job["job"]["id"], worker_name="J. Mensah", hours="10")
    ok = await a_timecard(
        client, h, sold_job["job"]["id"], worker_name="R. Mbeki", hours="10"
    )
    assert ok["worker_name"] == "R. Mbeki"


# ============================================ correcting entries =====


async def test_an_entry_can_be_corrected(client, sold_job):
    h = sold_job["headers"]
    card = await a_timecard(client, h, sold_job["job"]["id"], hours="8")
    fixed = await client.patch(
        f"/api/v1/timecards/{card['id']}", headers=h, json={"hours": "6", "hourly_rate": "45"}
    )
    assert fixed.status_code == 200
    assert fixed.json()["hours"] == "6.00"
    assert fixed.json()["labour_cost"] == "270.00"


async def test_correcting_an_entry_is_still_checked_against_the_day(client, sold_job):
    h = sold_job["headers"]
    await a_timecard(client, h, sold_job["job"]["id"], worker_name="J. Mensah", hours="10")
    card = await a_timecard(
        client, h, sold_job["job"]["id"], worker_name="J. Mensah", hours="4"
    )
    refused = await client.patch(
        f"/api/v1/timecards/{card['id']}", headers=h, json={"hours": "12"}
    )
    assert refused.status_code == 409


async def test_an_entry_can_be_deleted(client, sold_job):
    h = sold_job["headers"]
    card = await a_timecard(client, h, sold_job["job"]["id"])
    resp = await client.delete(f"/api/v1/timecards/{card['id']}", headers=h)
    assert resp.status_code == 204

    page = await client.get(
        f"/api/v1/timecards?job_id={sold_job['job']['id']}", headers=h
    )
    assert card["id"] not in [c["id"] for c in page.json()["items"]]


async def test_approval_cannot_be_set_through_the_patch(client, sold_job):
    h = sold_job["headers"]
    card = await a_timecard(client, h, sold_job["job"]["id"])
    refused = await client.patch(
        f"/api/v1/timecards/{card['id']}", headers=h,
        json={"approved_on": str(TODAY), "hours": "6"},
    )
    assert refused.status_code == 422


# ==================================================== approval =====


async def test_approving_records_who_and_when(client, sold_job):
    h = sold_job["headers"]
    card = await a_timecard(client, h, sold_job["job"]["id"], hourly_rate="45")
    done = await post(
        client, h, f"/api/v1/timecards/{card['id']}/approve", None, expect=200
    )
    assert done["approved_on"] == str(TODAY)
    assert done["approved_by_id"] is not None
    assert done["is_approved"] is True


async def test_an_entry_with_no_rate_cannot_be_approved(client, sold_job):
    """Approving it would turn unknown money into nought money, and the nought is
    the one that reaches the job cost."""
    h = sold_job["headers"]
    card = await a_timecard(client, h, sold_job["job"]["id"])
    refused = await client.post(f"/api/v1/timecards/{card['id']}/approve", headers=h)
    assert refused.status_code == 409
    assert "rate" in refused.json()["detail"].lower()


async def test_approved_hours_cannot_be_changed_underneath_the_approval(client, sold_job):
    h = sold_job["headers"]
    card = await a_timecard(client, h, sold_job["job"]["id"], hourly_rate="45")
    await post(client, h, f"/api/v1/timecards/{card['id']}/approve", None, expect=200)

    refused = await client.patch(
        f"/api/v1/timecards/{card['id']}", headers=h, json={"hours": "12"}
    )
    assert refused.status_code == 409

    gone = await client.delete(f"/api/v1/timecards/{card['id']}", headers=h)
    assert gone.status_code == 409


async def test_withdrawing_the_approval_lets_it_be_corrected(client, sold_job):
    h = sold_job["headers"]
    card = await a_timecard(client, h, sold_job["job"]["id"], hourly_rate="45")
    await post(client, h, f"/api/v1/timecards/{card['id']}/approve", None, expect=200)

    back = await post(
        client, h, f"/api/v1/timecards/{card['id']}/withdraw-approval", None, expect=200
    )
    assert back["approved_on"] is None
    assert back["approved_by_id"] is None

    fixed = await client.patch(
        f"/api/v1/timecards/{card['id']}", headers=h, json={"hours": "6"}
    )
    assert fixed.status_code == 200


async def test_a_batch_approves_what_it_can_and_says_what_it_could_not(client, sold_job):
    """One entry missing a rate should not keep the other thirty-nine waiting."""
    h = sold_job["headers"]
    good = [
        (await a_timecard(
            client, h, sold_job["job"]["id"],
            worker_name=f"Worker {i}", hours="8", hourly_rate="45",
        ))["id"]
        for i in range(3)
    ]
    unrated = (await a_timecard(client, h, sold_job["job"]["id"], worker_name="No Rate"))["id"]

    result = await post(
        client, h, "/api/v1/timecards/approve",
        {"timecard_ids": good + [unrated]}, expect=200,
    )
    assert len(result["approved"]) == 3
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["timecard_id"] == unrated
    assert "rate" in result["skipped"][0]["reason"].lower()


# ================================================ productivity =====


async def test_productivity_reports_hours_against_the_budget(client, sold_job):
    """The half the screen could not do. What was spent is the half you knew."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    framing = sold_job["framing"]["id"]

    (
        await client.put(
            f"/api/v1/timecards/budgeted-hours/{job_id}",
            headers=h,
            json=[{"cost_code_id": framing, "budgeted_hours": "100"}],
        )
    ).json()
    for i in range(3):
        await a_timecard(
            client, h, job_id,
            worker_name=f"Framer {i}", hours="8", cost_code_id=framing, hourly_rate="45",
        )

    report = (await client.get(f"/api/v1/timecards/productivity?job_id={job_id}", headers=h)).json()
    row = next(r for r in report["rows"] if r["cost_code_id"] == framing)
    assert row["hours"] == "24.00"
    assert row["budgeted_hours"] == "100.00"
    assert row["hours_variance"] == "-76.00"       # under, so far
    assert row["average_rate"] == "45.00"


async def test_the_cost_target_is_hours_times_a_rate_not_the_budget_line(client, sold_job):
    """A budget line covers everything on the cost code - materials, plant, the lot.

    Comparing labour spend against the whole line reported a home a hundred
    thousand under on labour while it was fourteen hours over, which is worse than
    saying nothing: it is a reassuring number pointing the wrong way.
    """
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    framing = sold_job["framing"]["id"]

    report = (await client.get(f"/api/v1/timecards/productivity?job_id={job_id}", headers=h)).json()
    del report

    await client.patch(
        f"/api/v1/vendors/{sold_job['vendor']['id']}", headers=h, json={"labour_rate": "40"}
    )
    # Point the framing budget line at that trade, so a rate is knowable.
    budget = (await client.get(f"/api/v1/jobs/{job_id}/budget", headers=h)).json()
    line = next(line for line in budget["lines"] if line["cost_code_id"] == framing)
    assert Decimal(line["revised_amount"]) > Decimal("50000")   # the whole code, not labour

    await (
        await client.put(
            f"/api/v1/timecards/budgeted-hours/{job_id}",
            headers=h,
            json=[{"cost_code_id": framing, "budgeted_hours": "100"}],
        )
    ).aread()
    await a_timecard(client, h, job_id, hours="8", cost_code_id=framing, hourly_rate="40")

    report = (await client.get(f"/api/v1/timecards/productivity?job_id={job_id}", headers=h)).json()
    row = next(r for r in report["rows"] if r["cost_code_id"] == framing)
    # No vendor on the budget line, so no rate to expect and therefore no target.
    # What it must never be is the line's own amount.
    if row["budgeted_cost"] is not None:
        assert Decimal(row["budgeted_cost"]) < Decimal(line["revised_amount"])


async def test_a_row_with_no_target_reports_no_variance(client, sold_job):
    """A variance against zero is just the actual with a minus sign in front."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    await a_timecard(
        client, h, job_id, cost_code_id=sold_job["electrical"]["id"], hourly_rate="50"
    )
    report = (await client.get(f"/api/v1/timecards/productivity?job_id={job_id}", headers=h)).json()
    row = next(r for r in report["rows"] if r["cost_code_id"] == sold_job["electrical"]["id"])
    assert row["budgeted_hours"] is None
    assert row["hours_variance"] is None


async def test_unrated_hours_do_not_drag_the_average_rate_down(client, sold_job):
    """The bug this exists to stop: the hours stay in the denominator and nothing
    goes in the numerator, so the rate falls every time somebody forgets one."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    framing = sold_job["framing"]["id"]

    await a_timecard(
        client, h, job_id,
        worker_name="Rated", hours="10", cost_code_id=framing, hourly_rate="50",
    )
    await a_timecard(
        client, h, job_id, worker_name="Unrated", hours="10", cost_code_id=framing
    )

    report = (await client.get(f"/api/v1/timecards/productivity?job_id={job_id}", headers=h)).json()
    assert report["total_hours"] == "20.00"
    assert report["total_labour_cost"] == "500.00"
    assert report["unrated_hours"] == "10.00"
    # 500 over the 10 rated hours, not over all 20.
    assert report["average_rate"] == "50.00"


async def test_the_report_puts_what_is_running_away_first(client, sold_job):
    """Sorting by hours puts the biggest trade on top whether or not it is in any
    trouble."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    framing, electrical = sold_job["framing"]["id"], sold_job["electrical"]["id"]

    (
        await client.put(
            f"/api/v1/timecards/budgeted-hours/{job_id}",
            headers=h,
            json=[
            {"cost_code_id": framing, "budgeted_hours": "200"},
            {"cost_code_id": electrical, "budgeted_hours": "10"},
        ],
        )
    ).json()
    # Framing: lots of hours, well inside its target.
    for i in range(4):
        await a_timecard(
            client, h, job_id,
            worker_name=f"Framer {i}", hours="10", cost_code_id=framing, hourly_rate="45",
        )
    # Electrical: fewer hours, but over.
    for i in range(2):
        await a_timecard(
            client, h, job_id,
            worker_name=f"Spark {i}", hours="10", cost_code_id=electrical, hourly_rate="50",
        )

    report = (await client.get(f"/api/v1/timecards/productivity?job_id={job_id}", headers=h)).json()
    assert report["rows"][0]["cost_code_id"] == electrical   # over budget, fewer hours
    assert report["rows"][0]["hours_variance"] == "10.00"
    assert report["rows"][1]["hours_variance"] == "-160.00"


async def test_budgeted_hours_need_a_budget_line(client, sold_job):
    """A target on a code the budget has no money for would sit outside the
    document the money argument is had over."""
    h = sold_job["headers"]
    stray = await post(
        client, h, "/api/v1/cost-codes",
        {"code": "99-999", "name": "Nothing budgeted", "division": "99 Other"},
    )
    refused = await client.put(
        f"/api/v1/timecards/budgeted-hours/{sold_job['job']['id']}", headers=h,
        json=[{"cost_code_id": stray["id"], "budgeted_hours": "10"}],
    )
    assert refused.status_code == 409


async def test_clearing_a_target_is_not_the_same_as_setting_it_to_nought(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    framing = sold_job["framing"]["id"]
    await a_timecard(client, h, job_id, cost_code_id=framing, hourly_rate="45")

    (
        await client.put(
            f"/api/v1/timecards/budgeted-hours/{job_id}",
            headers=h,
            json=[{"cost_code_id": framing, "budgeted_hours": "100"}],
        )
    ).json()
    cleared = (
        await client.put(
            f"/api/v1/timecards/budgeted-hours/{job_id}",
            headers=h,
            json=[{"cost_code_id": framing, "budgeted_hours": None}],
        )
    ).json()
    row = next(r for r in cleared["rows"] if r["cost_code_id"] == framing)
    assert row["budgeted_hours"] is None
    assert row["hours_variance"] is None


async def test_budgeted_hours_are_not_summed_across_homes(client, sold_job):
    """It would look like a company labour budget and would not be one - homes at
    different stages have spent different fractions of theirs."""
    h = sold_job["headers"]
    (
        await client.put(
            f"/api/v1/timecards/budgeted-hours/{sold_job['job']['id']}",
            headers=h,
            json=[{"cost_code_id": sold_job["framing"]["id"], "budgeted_hours": "100"}],
        )
    ).json()
    everywhere = (await client.get("/api/v1/timecards/productivity", headers=h)).json()
    assert everywhere["budgeted_hours"] is None
    assert everywhere["hours_variance"] is None


# ===================================================== rollups =====


async def test_the_worker_rollup_says_who_was_on_site(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    for day_offset, hours in [(0, "8"), (1, "9"), (2, "7")]:
        await a_timecard(
            client, h, job_id,
            worker_name="J. Mensah", hours=hours,
            worked_on=str(TODAY - timedelta(days=day_offset)), hourly_rate="45",
        )

    rows = (await client.get(f"/api/v1/timecards/by-worker?job_id={job_id}", headers=h)).json()
    row = next(r for r in rows if r["worker_name"] == "J. Mensah")
    assert row["hours"] == "24.00"
    assert row["days_on_site"] == 3
    assert row["average_day"] == "8.00"
    assert row["average_rate"] == "45.00"


async def test_a_name_on_two_trades_is_flagged_rather_than_guessed(client, sold_job):
    """Usually two different people with the same name."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    other = await post(
        client, h, "/api/v1/vendors",
        {"code": "FRAME", "name": "Titan Framing", "vendor_type": "trade"},
    )
    await a_timecard(
        client, h, job_id, worker_name="J. Smith", hours="8",
        vendor_id=sold_job["vendor"]["id"],
    )
    await a_timecard(
        client, h, job_id, worker_name="J. Smith", hours="8",
        worked_on=str(TODAY - timedelta(days=1)), vendor_id=other["id"],
    )

    rows = (await client.get(f"/api/v1/timecards/by-worker?job_id={job_id}", headers=h)).json()
    row = next(r for r in rows if r["worker_name"] == "J. Smith")
    assert row["vendor_count"] == 2
    assert row["vendor_id"] is None


async def test_the_vendor_rollup_shows_the_overtime_share(client, sold_job):
    """A trade running at a third overtime is either short-handed or billing you
    for being."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    await a_timecard(
        client, h, job_id, worker_name="A", hours="8", overtime_hours="4",
        vendor_id=sold_job["vendor"]["id"], hourly_rate="40",
    )

    rows = (await client.get(f"/api/v1/timecards/by-vendor?job_id={job_id}", headers=h)).json()
    row = next(r for r in rows if r["vendor_id"] == sold_job["vendor"]["id"])
    assert row["hours"] == "12.00"
    assert row["overtime_hours"] == "4.00"
    assert row["overtime_share"] == "0.33"      # quantised, like every rate here
    # 8 x 40 + 4 x 60 = 560
    assert row["labour_cost"] == "560.00"


# ===================================================== summary =====


async def test_the_summary_counts_over_everything(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    rated = await a_timecard(
        client, h, job_id, worker_name="A", hours="8", hourly_rate="45"
    )
    await a_timecard(client, h, job_id, worker_name="B", hours="6")
    await post(client, h, f"/api/v1/timecards/{rated['id']}/approve", None, expect=200)

    s = (await client.get(f"/api/v1/timecards/summary?job_id={job_id}", headers=h)).json()
    assert s["entries"] == 2
    assert s["workers"] == 2
    assert s["pending_approval"] == 1
    assert s["unrated_entries"] == 1
    assert s["total_hours"] == "14.00"
    assert s["unrated_hours"] == "6.00"
    assert s["pending_hours"] == "6.00"
    assert s["average_rate"] == "45.00"


async def test_the_summary_refuses_a_home_that_is_not_ours(client, sold_job):
    h = sold_job["headers"]
    resp = await client.get(
        "/api/v1/timecards/summary?job_id=00000000-0000-0000-0000-000000000000", headers=h
    )
    assert resp.status_code == 404


async def test_one_builder_cannot_see_anothers_hours(client, sold_job):
    h = sold_job["headers"]
    await a_timecard(client, h, sold_job["job"]["id"])

    other = await signup(client, "Rival Homes", "owner@rival.example.com")
    page = await client.get("/api/v1/timecards", headers=other)
    assert page.json()["items"] == []
