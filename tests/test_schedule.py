"""The build programme, and keeping it honest as it slips.

Two things here were wrong in ways that made every date downstream fiction, and
most of what is asserted is about those:

  * **durations were calendar days.** A ten-day task starting Thursday finished
    Sunday. Nobody frames on a Sunday, and over a 115-day build the error
    compounds to roughly a third of the programme.
  * **a slip rewrote the promise.** Completing a task late moved the job's
    `projected_completion` - the date the Job model calls the frozen baseline
    lateness is measured against. The target retreated ahead of the slip, so the
    job was never late.

The rest is the lifecycle a superintendent needs and did not have: starting a
task, blocking one with a reason, skipping one, and moving a date without every
trade behind it silently staying put.
"""

from datetime import date, timedelta

from tests.conftest import post, signup

TODAY = date.today()


def next_monday(after: date | None = None) -> date:
    """A fixed weekday to reason from, so the assertions do not depend on when the
    suite happens to run."""
    day = after or TODAY
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


MONDAY = next_monday()


# --- helpers ------------------------------------------------------------------


async def make_template(client, headers, code="STD", tasks=None):
    template = await post(
        client, headers, "/api/v1/schedule-templates", {"code": code, "name": "Standard build"}
    )
    for spec in tasks or [
        {"name": "Site prep", "offset_days": 0, "duration_days": 5, "is_milestone": True},
        {"name": "Foundation", "offset_days": 5, "duration_days": 10},
        {"name": "Framing", "offset_days": 15, "duration_days": 10, "is_milestone": True},
    ]:
        await post(client, headers, f"/api/v1/schedule-templates/{template['id']}/tasks", spec)
    return (
        await client.get(f"/api/v1/schedule-templates/{template['id']}", headers=headers)
    ).json()


async def started_home(client, builder, lot_number="80"):
    """A home on site, ready for a schedule."""
    h = builder["headers"]
    lot = await post(
        client, h, f"/api/v1/communities/{builder['community']['id']}/lots",
        {"lot_number": lot_number},
    )
    job = await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": builder["community"]["id"],
            "lot_id": lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/start",
        {
            "allow_unsold": True,
            "start_date": str(TODAY),
            "projected_completion": str(TODAY + timedelta(days=120)),
        },
        expect=200,
    )
    return job


async def scheduled_home(client, builder, lot_number="80", code="STD", start=None):
    h = builder["headers"]
    template = await make_template(client, h, code=code)
    job = await started_home(client, builder, lot_number)
    tasks = await post(
        client, h, f"/api/v1/jobs/{job['id']}/schedule",
        {"template_id": template["id"], "start_date": str(start or MONDAY)},
    )
    return job, template, tasks


# ======================================================= working days =====


async def test_a_duration_never_lands_on_a_weekend(client, builder):
    """The correction this whole module turns on.

    A five-day task starting Monday finishes Friday. As calendar days it finished
    on the Friday too - but the ten-day task behind it ran into a Sunday, and
    everything after that was wrong.
    """
    _, _, tasks = await scheduled_home(client, builder, "80", start=MONDAY)

    prep = next(t for t in tasks if t["name"] == "Site prep")
    assert prep["scheduled_start"] == str(MONDAY)
    # Five working days, inclusive: Monday to Friday.
    assert prep["scheduled_finish"] == str(MONDAY + timedelta(days=4))

    for t in tasks:
        for key in ("scheduled_start", "scheduled_finish"):
            day = date.fromisoformat(t[key])
            assert day.weekday() < 5, f"{t['name']} {key} lands on a weekend"


async def test_a_ten_day_task_spans_two_calendar_weeks(client, builder):
    """As calendar days it would have finished on the Wednesday, four days before
    the trade had actually had ten days on site."""
    _, _, tasks = await scheduled_home(client, builder, "81", start=MONDAY)

    foundation = next(t for t in tasks if t["name"] == "Foundation")
    start = date.fromisoformat(foundation["scheduled_start"])
    finish = date.fromisoformat(foundation["scheduled_finish"])
    # Ten working days from a Monday is the Friday of the following week: eleven
    # calendar days, because one weekend falls inside. As calendar days it would
    # have finished on the Wednesday, before the trade had had ten days on site.
    assert (finish - start).days == 11
    assert start.weekday() == 0 and finish.weekday() == 4


async def test_a_schedule_starting_on_a_saturday_begins_on_the_monday(client, builder):
    """Work does not start on the Saturday and go unattended."""
    saturday = MONDAY + timedelta(days=5)
    _, _, tasks = await scheduled_home(client, builder, "82", start=saturday)

    first = min(tasks, key=lambda t: t["sequence"])
    assert date.fromisoformat(first["scheduled_start"]).weekday() == 0


async def test_a_holiday_pushes_the_programme(client, builder):
    """Which days are not worked is a property of the builder, not the software."""
    h = builder["headers"]
    template = await make_template(client, h, code="HOL")
    job = await started_home(client, builder, "83")

    # A Wednesday in the middle of the first task.
    holiday = MONDAY + timedelta(days=2)
    await post(
        client, h, "/api/v1/schedule/holidays",
        {"on_date": str(holiday), "name": "Founders day"},
    )

    tasks = await post(
        client, h, f"/api/v1/jobs/{job['id']}/schedule",
        {"template_id": template["id"], "start_date": str(MONDAY)},
    )
    prep = next(t for t in tasks if t["name"] == "Site prep")
    # Five working days that skip the Wednesday, so it lands on the Monday after.
    assert prep["scheduled_finish"] == str(MONDAY + timedelta(days=7))


async def test_a_holiday_cannot_be_added_twice(client, builder):
    h = builder["headers"]
    await post(
        client, h, "/api/v1/schedule/holidays", {"on_date": str(MONDAY), "name": "One"}
    )
    resp = await client.post(
        "/api/v1/schedule/holidays", headers=h, json={"on_date": str(MONDAY), "name": "Two"}
    )
    assert resp.status_code == 409


# ================================================ baseline vs forecast =====


async def test_a_slip_moves_the_forecast_and_leaves_the_promise_alone(client, builder):
    """The other correction. Moving the baseline meant the job was never late,
    because the target retreated ahead of the slip."""
    h = builder["headers"]
    job, _, tasks = await scheduled_home(client, builder, "84", start=MONDAY)

    before = (await client.get(f"/api/v1/jobs/{job['id']}", headers=h)).json()
    baseline = before["projected_completion"]
    assert baseline is not None

    prep = next(t for t in tasks if t["name"] == "Site prep")
    result = await post(
        client, h, f"/api/v1/tasks/{prep['id']}/complete",
        {"actual_finish": str(TODAY)}, expect=200,
    )

    after = (await client.get(f"/api/v1/jobs/{job['id']}", headers=h)).json()
    assert after["projected_completion"] == baseline     # untouched
    if result["days_late"] > 0:
        assert after["forecast_completion"] is not None
        assert after["forecast_slip"] is not None


async def test_finishing_late_pushes_everything_behind_it(client, builder):
    """The ripple is what makes the schedule worth keeping."""
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "85", start=MONDAY - timedelta(days=28))

    prep = next(t for t in tasks if t["name"] == "Site prep")
    foundation_before = next(t for t in tasks if t["name"] == "Foundation")

    result = await post(
        client, h, f"/api/v1/tasks/{prep['id']}/complete",
        {"actual_finish": str(TODAY)}, expect=200,
    )
    assert result["days_late"] > 0
    assert result["tasks_shifted"] == 2

    after = (
        await client.get(f"/api/v1/jobs/{prep['job_id']}/schedule", headers=h)
    ).json()
    foundation_after = next(t for t in after if t["name"] == "Foundation")
    assert foundation_after["scheduled_start"] > foundation_before["scheduled_start"]
    # And it still does not land on a weekend.
    assert date.fromisoformat(foundation_after["scheduled_start"]).weekday() < 5


async def test_finishing_on_time_shifts_nothing(client, builder):
    h = builder["headers"]
    # Dated into the past, because a task finished exactly on schedule can only be
    # recorded once that date has arrived - completing one in the future is
    # refused, and rightly.
    _, _, tasks = await scheduled_home(
        client, builder, "86", start=MONDAY - timedelta(days=28)
    )
    prep = next(t for t in tasks if t["name"] == "Site prep")

    result = await post(
        client, h, f"/api/v1/tasks/{prep['id']}/complete",
        {"actual_finish": prep["scheduled_finish"]}, expect=200,
    )
    assert result["days_late"] == 0
    assert result["tasks_shifted"] == 0


async def test_the_next_task_becomes_the_trades_to_start(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "87", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")

    result = await post(
        client, h, f"/api/v1/tasks/{prep['id']}/complete", {}, expect=200
    )
    after = (await client.get(f"/api/v1/jobs/{prep['job_id']}/schedule", headers=h)).json()
    nxt = next(t for t in after if t["id"] == result["next_task_id"])
    assert nxt["status"] == "ready"


# ================================================== the task lifecycle =====


async def test_a_task_can_be_started_before_it_finishes(client, builder):
    """The gap between the two is the only honest way to know a task is running
    long before it finishes late."""
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "88", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")

    started = await post(
        client, h, f"/api/v1/tasks/{prep['id']}/start", {}, expect=200
    )
    assert started["status"] == "in_progress"
    assert started["actual_start"] == str(TODAY)


async def test_a_blocked_task_records_why(client, builder):
    """`BLOCKED` existed as a status nothing could set, so a superintendent had no
    way to say the plot was waterlogged."""
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "89", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")

    blocked = await post(
        client, h, f"/api/v1/tasks/{prep['id']}/block",
        {"reason": "Plot waterlogged after the storm"}, expect=200,
    )
    assert blocked["status"] == "blocked"
    assert "waterlogged" in blocked["blocked_reason"]

    freed = await post(client, h, f"/api/v1/tasks/{prep['id']}/unblock", None, expect=200)
    assert freed["status"] == "ready"
    assert freed["blocked_reason"] is None


async def test_blocking_needs_a_reason(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "90", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")

    resp = await client.post(
        f"/api/v1/tasks/{prep['id']}/block", headers=h, json={"reason": ""}
    )
    assert resp.status_code == 422


async def test_a_skipped_task_is_not_a_completed_one(client, builder):
    """Counting it as done would report work nobody did."""
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "91", start=MONDAY)
    framing = next(t for t in tasks if t["name"] == "Framing")

    skipped = await post(
        client, h, f"/api/v1/tasks/{framing['id']}/skip",
        {"reason": "Timber frame delivered pre-assembled"}, expect=200,
    )
    assert skipped["status"] == "skipped"
    assert "pre-assembled" in skipped["notes"]

    resp = await client.post(f"/api/v1/tasks/{framing['id']}/complete", headers=h, json={})
    assert resp.status_code == 409
    assert "did not happen" in resp.json()["detail"]


async def test_a_task_cannot_be_completed_twice(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "92", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")
    await post(client, h, f"/api/v1/tasks/{prep['id']}/complete", {}, expect=200)

    resp = await client.post(f"/api/v1/tasks/{prep['id']}/complete", headers=h, json={})
    assert resp.status_code == 409


async def test_a_task_cannot_be_completed_in_the_future(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "93", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")

    resp = await client.post(
        f"/api/v1/tasks/{prep['id']}/complete",
        headers=h,
        json={"actual_finish": str(TODAY + timedelta(days=7))},
    )
    assert resp.status_code == 409
    assert "in the future" in resp.json()["detail"]


async def test_moving_a_task_moves_the_trades_behind_it(client, builder):
    """A task that moves on its own leaves the next trade booked for a day the
    site will not be ready."""
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "94", start=MONDAY)
    foundation = next(t for t in tasks if t["name"] == "Foundation")
    framing_before = next(t for t in tasks if t["name"] == "Framing")

    result = await post(
        client, h, f"/api/v1/tasks/{foundation['id']}/reschedule",
        {"scheduled_start": str(MONDAY + timedelta(days=21))}, expect=200,
    )
    assert result["tasks_shifted"] == 1

    after = (await client.get(f"/api/v1/jobs/{foundation['job_id']}/schedule", headers=h)).json()
    framing_after = next(t for t in after if t["name"] == "Framing")
    assert framing_after["scheduled_start"] > framing_before["scheduled_start"]


async def test_a_task_can_be_moved_without_cascading(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "95", start=MONDAY)
    foundation = next(t for t in tasks if t["name"] == "Foundation")
    framing_before = next(t for t in tasks if t["name"] == "Framing")

    await post(
        client, h, f"/api/v1/tasks/{foundation['id']}/reschedule",
        {"scheduled_start": str(MONDAY + timedelta(days=21)), "cascade": False}, expect=200,
    )
    after = (await client.get(f"/api/v1/jobs/{foundation['job_id']}/schedule", headers=h)).json()
    framing_after = next(t for t in after if t["name"] == "Framing")
    assert framing_after["scheduled_start"] == framing_before["scheduled_start"]


async def test_a_trade_can_be_put_on_a_task(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "96", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")

    assigned = await post(
        client, h, f"/api/v1/tasks/{prep['id']}/trade",
        {"vendor_id": builder["vendor"]["id"]}, expect=200,
    )
    assert assigned["vendor_id"] == builder["vendor"]["id"]


# ===================================================== generating it =====


async def test_a_home_cannot_have_two_schedules(client, builder):
    """The sort of thing nobody notices until two trades turn up on the same
    morning."""
    h = builder["headers"]
    job, template, _ = await scheduled_home(client, builder, "97", start=MONDAY)

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/schedule",
        headers=h,
        json={"template_id": template["id"]},
    )
    assert resp.status_code == 409
    assert "already has a schedule" in resp.json()["detail"]


async def test_an_empty_template_generates_nothing(client, builder):
    h = builder["headers"]
    empty = await post(
        client, h, "/api/v1/schedule-templates", {"code": "EMPTY", "name": "Nothing yet"}
    )
    job = await started_home(client, builder, "98")

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/schedule", headers=h, json={"template_id": empty["id"]}
    )
    assert resp.status_code == 409
    assert "has no tasks" in resp.json()["detail"]


# ========================================================== templates =====


async def test_a_task_is_appended_rather_than_colliding(client, builder):
    """Two tasks at the same sequence generate a schedule whose order depends on
    the database."""
    h = builder["headers"]
    template = await make_template(client, h, code="APP")
    added = await post(
        client, h, f"/api/v1/schedule-templates/{template['id']}/tasks",
        {"name": "Snagging", "duration_days": 3},
    )
    assert added["sequence"] == 4


async def test_a_template_can_be_copied_with_its_tasks(client, builder):
    """A builder's second house type is usually the first with three steps
    changed, and retyping forty tasks is how a template ends up wrong."""
    h = builder["headers"]
    template = await make_template(client, h, code="ORIG")

    copy = await post(
        client, h, f"/api/v1/schedule-templates/{template['id']}/duplicate",
        {"code": "COPY", "name": "Standard build, detached"},
    )
    assert len(copy["tasks"]) == len(template["tasks"])
    assert [t["name"] for t in sorted(copy["tasks"], key=lambda x: x["sequence"])] == [
        t["name"] for t in sorted(template["tasks"], key=lambda x: x["sequence"])
    ]


async def test_a_template_step_can_be_corrected_and_removed(client, builder):
    h = builder["headers"]
    template = await make_template(client, h, code="EDIT")
    task = sorted(template["tasks"], key=lambda t: t["sequence"])[0]

    fixed = (
        await client.patch(
            f"/api/v1/schedule-templates/tasks/{task['id']}",
            headers=h,
            json={"duration_days": 8},
        )
    ).json()
    assert fixed["duration_days"] == 8

    gone = await client.delete(
        f"/api/v1/schedule-templates/tasks/{task['id']}", headers=h
    )
    assert gone.status_code == 204

    after = (
        await client.get(f"/api/v1/schedule-templates/{template['id']}", headers=h)
    ).json()
    assert len(after["tasks"]) == 2


async def test_reordering_needs_every_task(client, builder):
    """A partial list would leave gaps or duplicates, and the sequence is what the
    whole ripple is built from."""
    h = builder["headers"]
    template = await make_template(client, h, code="ORDER")
    ids = [t["id"] for t in sorted(template["tasks"], key=lambda t: t["sequence"])]

    partial = await client.post(
        f"/api/v1/schedule-templates/{template['id']}/reorder",
        headers=h,
        json={"task_ids": ids[:2]},
    )
    assert partial.status_code == 409

    reversed_ = await post(
        client, h, f"/api/v1/schedule-templates/{template['id']}/reorder",
        {"task_ids": list(reversed(ids))}, expect=200,
    )
    order = [t["name"] for t in sorted(reversed_["tasks"], key=lambda t: t["sequence"])]
    assert order == ["Framing", "Foundation", "Site prep"]


# ============================================================ the queues =====


async def test_the_lookahead_answers_what_is_due_this_fortnight(client, builder):
    """The question a superintendent actually asks, and there was no way to ask
    it - the only view was one job's task list."""
    h = builder["headers"]
    await scheduled_home(client, builder, "99", code="LA1", start=MONDAY)

    soon = (await client.get("/api/v1/schedule/lookahead?days=14", headers=h)).json()
    assert len(soon) >= 1
    assert all(t["is_open"] for t in soon)


async def test_the_behind_list_holds_what_should_have_finished(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(
        client, builder, "100", code="LATE", start=MONDAY - timedelta(days=60)
    )

    late = (await client.get("/api/v1/schedule/behind", headers=h)).json()
    assert len(late) >= 1
    assert all(t["is_overdue"] for t in late)
    assert all(t["days_overdue"] > 0 for t in late)
    assert tasks


async def test_a_completed_task_drops_off_the_behind_list(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(
        client, builder, "101", code="DROP", start=MONDAY - timedelta(days=60)
    )
    prep = next(t for t in tasks if t["name"] == "Site prep")
    await post(client, h, f"/api/v1/tasks/{prep['id']}/complete", {}, expect=200)

    late = (await client.get("/api/v1/schedule/behind", headers=h)).json()
    assert prep["id"] not in [t["id"] for t in late]


async def test_the_summary_counts_what_needs_attention(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "102", code="SUM", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")
    await post(
        client, h, f"/api/v1/tasks/{prep['id']}/block", {"reason": "Waiting on the survey"}, 200
    )

    summary = (await client.get("/api/v1/schedule/summary", headers=h)).json()
    assert summary["blocked"] == 1
    assert summary["templates"] >= 1


async def test_the_lookahead_can_be_narrowed_to_one_trade(client, builder):
    h = builder["headers"]
    _, _, tasks = await scheduled_home(client, builder, "103", code="TRADE", start=MONDAY)
    prep = next(t for t in tasks if t["name"] == "Site prep")
    await post(
        client, h, f"/api/v1/tasks/{prep['id']}/trade",
        {"vendor_id": builder["vendor"]["id"]}, expect=200,
    )

    theirs = (
        await client.get(
            f"/api/v1/schedule/lookahead?vendor_id={builder['vendor']['id']}", headers=h
        )
    ).json()
    assert [t["id"] for t in theirs] == [prep["id"]]


async def test_the_schedule_does_not_leak_between_builders(client, builder):
    await scheduled_home(client, builder, "104", code="MINE", start=MONDAY)
    other = await signup(client, "Rival Build", "rival-sched@rival.example.com")

    assert (await client.get("/api/v1/schedule/lookahead", headers=other)).json() == []
    assert (await client.get("/api/v1/schedule-templates", headers=other)).json() == []
    assert (await client.get("/api/v1/schedule/holidays", headers=other)).json() == []
