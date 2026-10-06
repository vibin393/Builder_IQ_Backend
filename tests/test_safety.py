"""Safety and quality.

Three claims this feature makes, and what would be wrong if it did not.

  * **An observation is a fix somebody owes, not a note.** So it closes only when
    somebody other than whoever did the work has looked at the fix and accepted
    it. Without that, closed means "the trade said so".
  * **A failed inspection raises one automatically.** That sentence was on the
    screen before anything did it, which made the inspection a record that a
    problem existed rather than a record of what was done about it.
  * **Whether an incident is reportable is a legal question with a deadline.** In
    the UK that is RIDDOR. A tick box cannot be late; a deadline can, and the
    failure mode of a tick box is silently not reporting something.
"""

from datetime import date, timedelta

from tests.conftest import post, signup

TODAY = date.today()


# --- helpers ------------------------------------------------------------------


async def an_observation(client, headers, job_id, **overrides):
    payload = {
        "job_id": job_id,
        "title": "Unguarded stair opening, first floor",
        "location": "First floor landing",
        "observation_type": "safety",
        "severity": "high",
    }
    payload.update(overrides)
    return await post(client, headers, "/api/v1/observations", payload)


async def an_incident(client, headers, job_id, **overrides):
    payload = {
        "job_id": job_id,
        "incident_type": "injury",
        "title": "Laceration to forearm from utility knife",
        "occurred_on": str(TODAY - timedelta(days=3)),
        "severity": "medium",
    }
    payload.update(overrides)
    return await post(client, headers, "/api/v1/incidents", payload)


async def a_template(client, headers, name="Pre-pour checklist", steps=None):
    plan = await post(
        client, headers, "/api/v1/action-plans", {"name": name, "is_template": True}
    )
    for description, signoff in steps or [
        ("Rebar to drawing and tied", True),
        ("Formwork square and braced", False),
        ("Damp proof membrane lapped", True),
    ]:
        plan = await post(
            client, headers, f"/api/v1/action-plans/{plan['id']}/items",
            {"description": description, "requires_signoff": signoff},
            expect=200,          # returns the plan, not the step
        )
    return plan


# ============================================ the observation lifecycle =====


async def test_an_observation_starts_owed_and_dated(client, sold_job):
    """No date means a note. These are not notes, so one is derived from severity."""
    h = sold_job["headers"]
    obs = await an_observation(client, h, sold_job["job"]["id"], severity="critical")

    assert obs["status"] == "initiated"
    assert obs["is_open"] is True
    # Critical gets a day, not a fortnight.
    assert obs["due_on"] == str(TODAY + timedelta(days=1))


async def test_a_low_severity_observation_gets_longer(client, sold_job):
    h = sold_job["headers"]
    obs = await an_observation(client, h, sold_job["job"]["id"], severity="low")
    assert obs["due_on"] == str(TODAY + timedelta(days=14))


async def test_a_stated_due_date_is_kept(client, sold_job):
    h = sold_job["headers"]
    obs = await an_observation(
        client, h, sold_job["job"]["id"], severity="critical", due_on=str(TODAY + timedelta(days=5))
    )
    assert obs["due_on"] == str(TODAY + timedelta(days=5))


async def test_an_observation_cannot_jump_to_closed(client, sold_job):
    """The whole point. Closing on the word of whoever owed the work records an
    intention, not an outcome."""
    h = sold_job["headers"]
    obs = await an_observation(client, h, sold_job["job"]["id"])

    refused = await client.post(f"/api/v1/observations/{obs['id']}/close", headers=h)
    assert refused.status_code == 409
    assert "ready for review" in refused.json()["detail"]


async def test_the_fix_is_claimed_then_accepted(client, sold_job):
    h = sold_job["headers"]
    obs = await an_observation(client, h, sold_job["job"]["id"])

    claimed = await post(
        client, h, f"/api/v1/observations/{obs['id']}/submit",
        {"resolution": "Guardrail refitted and fixings checked."}, expect=200,
    )
    assert claimed["status"] == "ready_for_review"
    assert claimed["resolution"] == "Guardrail refitted and fixings checked."
    assert claimed["is_open"] is True        # claimed is not closed

    closed = await post(
        client, h, f"/api/v1/observations/{obs['id']}/close", None, expect=200
    )
    assert closed["status"] == "closed"
    assert closed["closed_on"] is not None
    assert closed["closed_by_id"] is not None
    assert closed["is_open"] is False


async def test_claiming_a_fix_needs_to_say_what_was_done(client, sold_job):
    """Asked of the person who did the work, because the reviewer writes 'fixed'."""
    h = sold_job["headers"]
    obs = await an_observation(client, h, sold_job["job"]["id"])

    refused = await client.post(
        f"/api/v1/observations/{obs['id']}/submit", headers=h, json={"resolution": ""}
    )
    assert refused.status_code == 422


async def test_a_bad_fix_goes_back_to_the_trade(client, sold_job):
    """The state the enum always had and nothing could reach."""
    h = sold_job["headers"]
    obs = await an_observation(client, h, sold_job["job"]["id"])
    await post(
        client, h, f"/api/v1/observations/{obs['id']}/submit",
        {"resolution": "Rail refitted"}, expect=200,
    )

    sent_back = await post(
        client, h, f"/api/v1/observations/{obs['id']}/reject",
        {"reason": "Rail is fixed to the plasterboard, not the stud."}, expect=200,
    )
    assert sent_back["status"] == "not_accepted"
    assert "stud" in sent_back["resolution"]
    assert sent_back["is_open"] is True

    # And it can be resubmitted, so the loop closes rather than dead-ending.
    again = await post(
        client, h, f"/api/v1/observations/{obs['id']}/submit",
        {"resolution": "Refixed into the stud, checked by the site manager."}, expect=200,
    )
    assert again["status"] == "ready_for_review"


async def test_reopening_goes_back_to_the_start(client, sold_job):
    """A second round is a full round, not a shortcut to closed."""
    h = sold_job["headers"]
    obs = await an_observation(client, h, sold_job["job"]["id"])
    await post(
        client, h, f"/api/v1/observations/{obs['id']}/submit", {"resolution": "Done"}, expect=200
    )
    await post(client, h, f"/api/v1/observations/{obs['id']}/close", None, expect=200)

    back = await post(
        client, h, f"/api/v1/observations/{obs['id']}/reopen",
        {"reason": "Failed again at the next walk."}, expect=200,
    )
    assert back["status"] == "initiated"
    assert back["closed_on"] is None
    assert back["closed_by_id"] is None

    # Straight to closed is refused all over again.
    refused = await client.post(f"/api/v1/observations/{obs['id']}/close", headers=h)
    assert refused.status_code == 409


async def test_status_cannot_be_set_through_the_patch(client, sold_job):
    """A PATCH that set it would route round the review entirely - and dropping the
    field silently would answer 200 to a request it did not carry out."""
    h = sold_job["headers"]
    obs = await an_observation(client, h, sold_job["job"]["id"])

    refused = await client.patch(
        f"/api/v1/observations/{obs['id']}", headers=h,
        json={"status": "closed", "location": "Landing"},
    )
    assert refused.status_code == 422

    page = await client.get(f"/api/v1/observations?job_id={sold_job['job']['id']}", headers=h)
    unchanged = next(o for o in page.json()["items"] if o["id"] == obs["id"])
    assert unchanged["status"] == "initiated"
    assert unchanged["location"] != "Landing"


async def test_an_overdue_observation_says_by_how_much(client, sold_job):
    h = sold_job["headers"]
    obs = await an_observation(
        client, h, sold_job["job"]["id"], due_on=str(TODAY - timedelta(days=4))
    )
    assert obs["is_overdue"] is True
    assert obs["days_overdue"] == 4


async def test_a_closed_observation_is_not_overdue(client, sold_job):
    """It was late; it is not still late. Otherwise the chase list never shrinks."""
    h = sold_job["headers"]
    obs = await an_observation(
        client, h, sold_job["job"]["id"], due_on=str(TODAY - timedelta(days=4))
    )
    await post(
        client, h, f"/api/v1/observations/{obs['id']}/submit", {"resolution": "Fixed"}, expect=200
    )
    closed = await post(
        client, h, f"/api/v1/observations/{obs['id']}/close", None, expect=200
    )
    assert closed["is_overdue"] is False
    assert closed["days_overdue"] is None


# ============================================== the inspection chain =====


async def test_a_failed_inspection_raises_an_observation(client, sold_job):
    """The sentence on the screen, made true.

    Without this an inspection records that a problem existed. With it, the
    inspection record shows what was done about the problem.
    """
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    insp = await post(
        client, h, "/api/v1/field/inspections",
        {"job_id": job_id, "name": "Framing", "inspection_type": "municipal"},
    )
    check = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/items",
        {"description": "Truss straps fitted", "vendor_id": sold_job["vendor"]["id"]},
    )
    await post(
        client, h, f"/api/v1/field/inspections/items/{check['id']}/result",
        {"result": "fail", "notes": "Missing at four connections"}, expect=200,
    )
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed"}, expect=200,
    )

    page = await client.get(f"/api/v1/observations?job_id={job_id}", headers=h)
    raised = [o for o in page.json()["items"] if o["inspection_id"] == insp["id"]]
    assert len(raised) == 1
    obs = raised[0]
    assert obs["title"] == "Framing failed"
    # A municipal failure is a legal stop, so it gets the shortest clock.
    assert obs["severity"] == "critical"
    assert obs["due_on"] == str(TODAY + timedelta(days=1))
    # Owed by the trade whose check failed.
    assert obs["assigned_vendor_id"] == sold_job["vendor"]["id"]
    assert obs["description"] == "Truss straps fitted"


async def test_a_passed_inspection_raises_nothing(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    insp = await post(
        client, h, "/api/v1/field/inspections",
        {"job_id": job_id, "name": "Foundation", "inspection_type": "municipal"},
    )
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "passed"}, expect=200,
    )

    page = await client.get(f"/api/v1/observations?job_id={job_id}", headers=h)
    assert not [o for o in page.json()["items"] if o["inspection_id"] == insp["id"]]


async def test_an_internal_failure_is_quality_not_safety(client, sold_job):
    """Ours to manage, so it is not dressed up as a statutory problem."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    insp = await post(
        client, h, "/api/v1/field/inspections",
        {"job_id": job_id, "name": "Pre-plaster QA", "inspection_type": "internal_qa"},
    )
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed", "failure_reason": "Service penetrations not fire stopped"},
        expect=200,
    )

    page = await client.get(f"/api/v1/observations?job_id={job_id}", headers=h)
    obs = next(o for o in page.json()["items"] if o["inspection_id"] == insp["id"])
    assert obs["observation_type"] == "quality"
    assert obs["severity"] == "high"


async def test_failed_checks_from_two_trades_stay_with_the_builder(client, sold_job):
    """Pinning it on whichever check came first would be arbitrary, and the trade
    it landed on would be the one arguing about it."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    other = await post(
        client, h, "/api/v1/vendors",
        {"code": "FRAME", "name": "Titan Framing", "vendor_type": "trade"},
    )
    insp = await post(
        client, h, "/api/v1/field/inspections",
        {"job_id": job_id, "name": "Framing", "inspection_type": "municipal"},
    )
    for description, vendor in [
        ("Truss straps fitted", sold_job["vendor"]["id"]),
        ("Notching within limits", other["id"]),
    ]:
        check = await post(
            client, h, f"/api/v1/field/inspections/{insp['id']}/items",
            {"description": description, "vendor_id": vendor},
        )
        await post(
            client, h, f"/api/v1/field/inspections/items/{check['id']}/result",
            {"result": "fail", "notes": "not right"}, expect=200,
        )
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed"}, expect=200,
    )

    page = await client.get(f"/api/v1/observations?job_id={job_id}", headers=h)
    obs = next(o for o in page.json()["items"] if o["inspection_id"] == insp["id"])
    assert obs["assigned_vendor_id"] is None


# ================================================= incidents: RIDDOR =====


async def test_a_treated_injury_is_categorised_without_being_asked(client, sold_job):
    """The alternative was a tick box, and a tick box defaults to unticked."""
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"], medical_treatment=True
    )
    assert incident["riddor_category"] == "specified_injury"
    assert incident["reportable"] is True
    # Ten days from the day it happened, not from today.
    assert incident["riddor_deadline"] == str(TODAY + timedelta(days=7))
    assert incident["report_without_delay"] is True


async def test_a_lost_time_injury_gets_the_longer_deadline(client, sold_job):
    """Fifteen days, because the seven have to elapse before anybody knows it
    qualifies."""
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"], lost_time=True,
        occurred_on=str(TODAY),
    )
    assert incident["riddor_category"] == "over_7_day"
    assert incident["riddor_deadline"] == str(TODAY + timedelta(days=15))
    assert incident["report_without_delay"] is False


async def test_an_ordinary_near_miss_is_not_reportable(client, sold_job):
    """Categorising every near miss would train people to ignore the flag."""
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"],
        incident_type="near_miss", severity="high", title="Dropped hammer",
    )
    assert incident["riddor_category"] == "not_reportable"
    assert incident["reportable"] is False
    assert incident["riddor_deadline"] is None
    assert incident["days_to_report"] is None


async def test_a_critical_near_miss_is_a_dangerous_occurrence(client, sold_job):
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"],
        incident_type="near_miss", severity="critical", title="Scaffold collapse",
    )
    assert incident["riddor_category"] == "dangerous_occurrence"
    assert incident["report_without_delay"] is True


async def test_a_stated_category_overrides_the_suggestion(client, sold_job):
    """The regulations turn on facts this system does not hold, so the operator
    has the last word."""
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"],
        incident_type="near_miss", severity="low", riddor_category="dangerous_occurrence",
    )
    assert incident["riddor_category"] == "dangerous_occurrence"
    assert incident["reportable"] is True


async def test_an_overdue_hse_report_says_so(client, sold_job):
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"],
        medical_treatment=True, occurred_on=str(TODAY - timedelta(days=14)),
    )
    assert incident["report_overdue"] is True
    assert incident["days_to_report"] == -4

    page = await client.get("/api/v1/incidents/awaiting-report", headers=h)
    assert incident["id"] in [i["id"] for i in page.json()]


async def test_recording_the_report_clears_the_deadline(client, sold_job):
    h = sold_job["headers"]
    incident = await an_incident(client, h, sold_job["job"]["id"], medical_treatment=True)

    done = await post(
        client, h, f"/api/v1/incidents/{incident['id']}/hse-report",
        {"reference": "HSE-2026-00814"}, expect=200,
    )
    assert done["reported_to_hse_on"] == str(TODAY)
    assert done["hse_reference"] == "HSE-2026-00814"
    assert done["report_overdue"] is False
    assert done["days_to_report"] is None

    page = await client.get("/api/v1/incidents/awaiting-report", headers=h)
    assert incident["id"] not in [i["id"] for i in page.json()]


async def test_a_report_cannot_predate_the_incident(client, sold_job):
    h = sold_job["headers"]
    incident = await an_incident(client, h, sold_job["job"]["id"], medical_treatment=True)
    refused = await client.post(
        f"/api/v1/incidents/{incident['id']}/hse-report", headers=h,
        json={"reported_on": str(TODAY - timedelta(days=30))},
    )
    assert refused.status_code == 409


async def test_a_report_cannot_be_recorded_on_a_non_reportable_incident(client, sold_job):
    """It would make the reportable count wrong in the direction nobody checks."""
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"], incident_type="near_miss", severity="low"
    )
    refused = await client.post(
        f"/api/v1/incidents/{incident['id']}/hse-report", headers=h, json={}
    )
    assert refused.status_code == 409


async def test_an_incident_cannot_have_happened_tomorrow(client, sold_job):
    h = sold_job["headers"]
    refused = await client.post(
        "/api/v1/incidents", headers=h,
        json={
            "job_id": sold_job["job"]["id"],
            "incident_type": "injury",
            "title": "Future injury",
            "occurred_on": str(TODAY + timedelta(days=1)),
        },
    )
    assert refused.status_code == 409


# ============================================= incidents: closing =====


async def test_a_reportable_incident_cannot_close_unreported(client, sold_job):
    """The one rule here with a legal consequence. Closing it would file the
    obligation as dealt with."""
    h = sold_job["headers"]
    incident = await an_incident(client, h, sold_job["job"]["id"], medical_treatment=True)
    await post(
        client, h, f"/api/v1/incidents/{incident['id']}/root-cause",
        {"root_cause": "Blade retracted too slowly; knives replaced with safety knives."},
        expect=200,
    )

    refused = await client.post(f"/api/v1/incidents/{incident['id']}/close", headers=h)
    assert refused.status_code == 409
    assert "HSE" in refused.json()["detail"]


async def test_an_incident_cannot_close_with_no_cause(client, sold_job):
    """An investigation that concluded nothing leaves nothing to act on."""
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"], incident_type="near_miss", severity="low"
    )
    refused = await client.post(f"/api/v1/incidents/{incident['id']}/close", headers=h)
    assert refused.status_code == 409
    assert "root cause" in refused.json()["detail"].lower()


async def test_a_fully_handled_incident_closes(client, sold_job):
    h = sold_job["headers"]
    incident = await an_incident(client, h, sold_job["job"]["id"], medical_treatment=True)
    await post(
        client, h, f"/api/v1/incidents/{incident['id']}/root-cause",
        {"root_cause": "Blade retracted too slowly."}, expect=200,
    )
    await post(
        client, h, f"/api/v1/incidents/{incident['id']}/hse-report",
        {"reference": "HSE-2026-00814"}, expect=200,
    )
    closed = await post(
        client, h, f"/api/v1/incidents/{incident['id']}/close", None, expect=200
    )
    assert closed["closed_on"] == str(TODAY)
    assert closed["is_open"] is False


async def test_reclassifying_to_not_reportable_after_reporting_is_refused(client, sold_job):
    """It would leave a report on file against an incident the system says never
    needed one."""
    h = sold_job["headers"]
    incident = await an_incident(client, h, sold_job["job"]["id"], medical_treatment=True)
    await post(
        client, h, f"/api/v1/incidents/{incident['id']}/hse-report", {}, expect=200
    )
    refused = await client.post(
        f"/api/v1/incidents/{incident['id']}/classify", headers=h,
        json={"riddor_category": "not_reportable"},
    )
    assert refused.status_code == 409


async def test_reclassifying_moves_the_deadline(client, sold_job):
    h = sold_job["headers"]
    incident = await an_incident(
        client, h, sold_job["job"]["id"], medical_treatment=True, occurred_on=str(TODAY)
    )
    assert incident["riddor_deadline"] == str(TODAY + timedelta(days=10))

    moved = await post(
        client, h, f"/api/v1/incidents/{incident['id']}/classify",
        {"riddor_category": "over_7_day"}, expect=200,
    )
    assert moved["riddor_deadline"] == str(TODAY + timedelta(days=15))


# ================================================== action plans =====


async def test_a_template_cannot_belong_to_a_home(client, sold_job):
    h = sold_job["headers"]
    refused = await client.post(
        "/api/v1/action-plans", headers=h,
        json={"name": "Pre-pour", "is_template": True, "job_id": sold_job["job"]["id"]},
    )
    assert refused.status_code == 409


async def test_starting_a_template_copies_its_steps(client, sold_job):
    """Copied, not referenced. A template edited next month must not rewrite the
    checklist somebody signed last month."""
    h = sold_job["headers"]
    template = await a_template(client, h)

    plan = await post(
        client, h, f"/api/v1/action-plans/{template['id']}/start",
        {"job_id": sold_job["job"]["id"]},
    )
    assert plan["job_id"] == sold_job["job"]["id"]
    assert plan["is_template"] is False
    assert plan["template_id"] == template["id"]
    assert plan["started_on"] == str(TODAY)
    assert plan["status"] == "active"
    assert [i["description"] for i in plan["items"]] == [
        i["description"] for i in template["items"]
    ]

    # Editing the template afterwards leaves the started plan alone.
    await post(
        client, h, f"/api/v1/action-plans/{template['id']}/items",
        {"description": "Added later"}, expect=200,
    )
    page = await client.get(
        f"/api/v1/action-plans?job_id={sold_job['job']['id']}", headers=h
    )
    live = next(p for p in page.json()["items"] if p["id"] == plan["id"])
    assert len(live["items"]) == 3


async def test_an_empty_template_cannot_be_started(client, sold_job):
    """An empty checklist on a home reads as complete."""
    h = sold_job["headers"]
    empty = await post(
        client, h, "/api/v1/action-plans", {"name": "Nothing yet", "is_template": True}
    )
    refused = await client.post(
        f"/api/v1/action-plans/{empty['id']}/start", headers=h,
        json={"job_id": sold_job["job"]["id"]},
    )
    assert refused.status_code == 409


async def test_steps_run_in_order(client, sold_job):
    """A pre-pour checklist that lets the pour be signed off before the rebar
    check is a form, not a checklist."""
    h = sold_job["headers"]
    template = await a_template(client, h)
    plan = await post(
        client, h, f"/api/v1/action-plans/{template['id']}/start",
        {"job_id": sold_job["job"]["id"]},
    )
    second = plan["items"][1]

    refused = await client.post(
        f"/api/v1/action-plans/{plan['id']}/items/{second['id']}/complete", headers=h, json={}
    )
    assert refused.status_code == 409
    assert "Rebar" in refused.json()["detail"]


async def test_a_signoff_step_needs_a_second_pair_of_eyes(client, sold_job):
    """The model has always said so. With one column the code set both to the same
    user, which made the rule a comment."""
    h = sold_job["headers"]
    template = await a_template(client, h)
    plan = await post(
        client, h, f"/api/v1/action-plans/{template['id']}/start",
        {"job_id": sold_job["job"]["id"]},
    )
    first = plan["items"][0]

    plan = await post(
        client, h, f"/api/v1/action-plans/{plan['id']}/items/{first['id']}/complete",
        {}, expect=200,
    )
    step = plan["items"][0]
    assert step["completed_on"] == str(TODAY)
    assert step["completed_by_id"] is not None
    assert step["awaiting_signoff"] is True
    assert step["is_done"] is False          # done means witnessed

    refused = await client.post(
        f"/api/v1/action-plans/{plan['id']}/items/{first['id']}/sign-off", headers=h
    )
    assert refused.status_code == 409
    assert "cannot witness" in refused.json()["detail"]


async def test_somebody_else_can_witness_it(client, sold_job):
    h = sold_job["headers"]
    template = await a_template(client, h)
    plan = await post(
        client, h, f"/api/v1/action-plans/{template['id']}/start",
        {"job_id": sold_job["job"]["id"]},
    )
    first = plan["items"][0]
    await post(
        client, h, f"/api/v1/action-plans/{plan['id']}/items/{first['id']}/complete",
        {}, expect=200,
    )

    # A second user in the same tenant.
    mate = await post(
        client, h, "/api/v1/users",
        {
            "email": "super@cascade.example.com",
            "full_name": "Site Manager",
            "password": "Sup3rStrong!",
            "role": "superintendent",
        },
    )
    assert mate["id"]
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "super@cascade.example.com", "password": "Sup3rStrong!"},
    )
    h2 = {"Authorization": f"Bearer {login.json()['access_token']}"}

    plan = await post(
        client, h2, f"/api/v1/action-plans/{plan['id']}/items/{first['id']}/sign-off",
        None, expect=200,
    )
    step = plan["items"][0]
    assert step["signed_off_on"] == str(TODAY)
    assert step["signed_off_by_id"] != step["completed_by_id"]
    assert step["is_done"] is True
    assert step["awaiting_signoff"] is False


async def test_a_plan_is_not_complete_while_a_signoff_is_outstanding(client, sold_job):
    """Counting a step waiting on its witness as finished would report the plan
    complete while the thing it exists to guarantee had not happened."""
    h = sold_job["headers"]
    template = await a_template(
        client, h, name="Two steps", steps=[("Do the thing", True), ("Do the other", False)]
    )
    plan = await post(
        client, h, f"/api/v1/action-plans/{template['id']}/start",
        {"job_id": sold_job["job"]["id"]},
    )
    for item in plan["items"]:
        plan = await post(
            client, h, f"/api/v1/action-plans/{plan['id']}/items/{item['id']}/complete",
            {}, expect=200,
        )

    assert plan["completed_items"] == 2
    assert plan["status"] != "complete"
    assert plan["next_item"] is None          # nothing left to do
    assert plan["items"][0]["awaiting_signoff"] is True


async def test_the_next_step_is_the_one_the_plan_is_waiting_on(client, sold_job):
    h = sold_job["headers"]
    template = await a_template(client, h)
    plan = await post(
        client, h, f"/api/v1/action-plans/{template['id']}/start",
        {"job_id": sold_job["job"]["id"]},
    )
    assert plan["next_item"]["sequence"] == 1
    assert plan["percent_complete"] == 0


# ======================================================= summary =====


async def test_the_summary_counts_over_everything(client, sold_job):
    """Not over the page on screen. 'Two open observations' meaning 'two on this
    page' is the kind of number people act on."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    await an_observation(client, h, job_id, severity="critical")
    await an_observation(client, h, job_id, severity="low")
    late = await an_observation(
        client, h, job_id, severity="medium", due_on=str(TODAY - timedelta(days=2))
    )
    reviewing = await an_observation(client, h, job_id, severity="high")
    await post(
        client, h, f"/api/v1/observations/{reviewing['id']}/submit",
        {"resolution": "Done"}, expect=200,
    )
    await an_incident(client, h, job_id, medical_treatment=True)
    await an_incident(client, h, job_id, incident_type="near_miss", severity="low")

    s = (await client.get(f"/api/v1/safety/summary?job_id={job_id}", headers=h)).json()
    assert s["open_observations"] == 4          # including the one in review
    assert s["awaiting_review"] == 1
    assert s["overdue"] == 1
    assert s["critical"] == 1
    assert s["incidents"] == 2
    assert s["near_misses"] == 1
    assert s["reportable"] == 1
    assert s["awaiting_hse_report"] == 1
    assert late["id"]


async def test_the_dashboard_and_this_screen_agree_on_open(client, sold_job):
    """Two screens showing different numbers for the same word.

    The dashboard counted only initiated and not-accepted, leaving out the ones
    sent for review - so a builder reading "3 open" on one page and "4 open" on
    the next has no way to know which is wrong, and stops trusting both.
    """
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    await an_observation(client, h, job_id)
    reviewing = await an_observation(client, h, job_id)
    await post(
        client, h, f"/api/v1/observations/{reviewing['id']}/submit",
        {"resolution": "Done"}, expect=200,
    )

    screen = (await client.get(f"/api/v1/safety/summary?job_id={job_id}", headers=h)).json()
    dashboard = (await client.get("/api/v1/reports/dashboard", headers=h)).json()
    assert screen["open_observations"] == 2
    assert dashboard["controls"]["observations_open"] == screen["open_observations"]


async def test_the_dashboard_counts_reports_still_owed_not_ever_owed(client, sold_job):
    """A count of every reportable incident ever never goes down, so nobody looks
    at it twice. What belongs on a dashboard is the list somebody has to act on."""
    h = sold_job["headers"]
    incident = await an_incident(client, h, sold_job["job"]["id"], medical_treatment=True)

    before = (await client.get("/api/v1/reports/dashboard", headers=h)).json()
    assert before["controls"]["incidents_reportable"] == 1

    await post(
        client, h, f"/api/v1/incidents/{incident['id']}/hse-report",
        {"reference": "HSE-2026-1"}, expect=200,
    )
    after = (await client.get("/api/v1/reports/dashboard", headers=h)).json()
    assert after["controls"]["incidents_reportable"] == 0


async def test_the_summary_refuses_a_home_that_is_not_ours(client, sold_job):
    """An empty summary reads as 'nothing is wrong', not 'no such house'."""
    h = sold_job["headers"]
    resp = await client.get(
        "/api/v1/safety/summary?job_id=00000000-0000-0000-0000-000000000000", headers=h
    )
    assert resp.status_code == 404


async def test_one_builder_cannot_see_anothers_observations(client, sold_job):
    h = sold_job["headers"]
    await an_observation(client, h, sold_job["job"]["id"])

    other = await signup(client, "Rival Homes", "owner@rival.example.com")
    page = await client.get("/api/v1/observations", headers=other)
    assert page.json()["items"] == []
