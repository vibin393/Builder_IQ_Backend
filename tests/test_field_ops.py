"""Field operations: the punch list, inspections, and the end of a build.

What is asserted here is mostly about the last fortnight of a house, because that
is where a home stops being finishable:

  * **somebody other than the trade signs the fix off.** A defect used to go from
    "the trade says it is done" straight to complete, so a home could be handed
    over on a snag list the builder had only their word for.
  * **open defects stop the handover.** Not cosmetic ones - a buyer will take the
    house and a decorator comes back - but anything above that, because handing
    over with a major defect on the list turns a warranty claim into a dispute.
  * **a failed inspection becomes a punch list in one step.** Retyping thirty
    failed checks is how three get missed, and the three that get missed are the
    ones the inspector looks at first on the re-inspection.
"""

from datetime import date, timedelta

from tests.conftest import post, signup

TODAY = date.today()
LAST_WEEK = TODAY - timedelta(days=7)


# --- helpers ------------------------------------------------------------------


async def a_home(client, builder, lot_number="110"):
    h = builder["headers"]
    lot = await post(
        client, h, f"/api/v1/communities/{builder['community']['id']}/lots",
        {"lot_number": lot_number},
    )
    return await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": builder["community"]["id"],
            "lot_id": lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )


async def a_defect(client, headers, job_id, **overrides):
    payload = {
        "job_id": job_id,
        "description": "Scuff on the hallway wall",
        "location": "Upstairs hallway",
        "severity": "minor",
    }
    payload.update(overrides)
    return await post(client, headers, "/api/v1/field/punch-items", payload)


async def an_inspection(client, headers, job_id, **overrides):
    payload = {"job_id": job_id, "name": "Framing", "inspection_type": "municipal"}
    payload.update(overrides)
    return await post(client, headers, "/api/v1/field/inspections", payload)


# ============================================== the punch lifecycle =====


async def test_a_defect_is_checked_before_it_is_closed(client, builder):
    """The step that was missing. Without it a home could be handed over on a snag
    list the builder had only the trade's word for."""
    h = builder["headers"]
    job = await a_home(client, builder, "110")
    item = await a_defect(client, h, job["id"])
    assert item["status"] == "open"

    assigned = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/assign",
        {"vendor_id": builder["vendor"]["id"], "due_date": str(TODAY + timedelta(days=3))},
        expect=200,
    )
    assert assigned["status"] == "assigned"
    assert assigned["ball_in_court"] == "trade"

    claimed = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/submit",
        {"note": "Filled and repainted"}, expect=200,
    )
    assert claimed["status"] == "ready_for_review"
    assert claimed["ball_in_court"] == "builder"

    signed = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/verify", None, expect=200
    )
    assert signed["status"] == "complete"
    assert signed["verified_by_id"] is not None
    assert signed["verified_date"] == str(TODAY)


async def test_a_defect_cannot_skip_the_check(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "111")
    item = await a_defect(client, h, job["id"])

    resp = await client.post(f"/api/v1/field/punch-items/{item['id']}/verify", headers=h)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "cannot become complete" in detail
    assert "ready for review" in detail


async def test_a_bad_fix_goes_back_with_a_reason(client, builder):
    """The loop that matters: a rejected fix used to be a phone call nobody could
    find later when the same defect was argued about at handover."""
    h = builder["headers"]
    job = await a_home(client, builder, "112")
    item = await a_defect(client, h, job["id"])
    await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/assign",
        {"vendor_id": builder["vendor"]["id"]}, expect=200,
    )
    await post(client, h, f"/api/v1/field/punch-items/{item['id']}/submit", {}, expect=200)

    sent_back = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/reject",
        {"reason": "Paint does not match the rest of the wall"}, expect=200,
    )
    assert sent_back["status"] == "assigned"
    assert sent_back["ball_in_court"] == "trade"
    assert "does not match" in sent_back["resolution_note"]


async def test_rejecting_needs_a_reason(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "113")
    item = await a_defect(client, h, job["id"])
    await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/assign",
        {"vendor_id": builder["vendor"]["id"]}, expect=200,
    )
    await post(client, h, f"/api/v1/field/punch-items/{item['id']}/submit", {}, expect=200)

    resp = await client.post(
        f"/api/v1/field/punch-items/{item['id']}/reject", headers=h, json={"reason": ""}
    )
    assert resp.status_code == 422


async def test_declining_to_fix_needs_a_reason(client, builder):
    """The status a buyer argues about at handover, so "we decided not to" is not
    an answer the system accepts."""
    h = builder["headers"]
    job = await a_home(client, builder, "114")
    item = await a_defect(client, h, job["id"], severity="cosmetic")

    resp = await client.post(
        f"/api/v1/field/punch-items/{item['id']}/wont-fix", headers=h, json={"reason": ""}
    )
    assert resp.status_code == 422

    declined = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/wont-fix",
        {"reason": "Within tolerance; agreed with the buyer on the walkthrough"},
        expect=200,
    )
    assert declined["status"] == "wont_fix"


async def test_a_blocking_defect_cannot_simply_be_declined(client, builder):
    """Blocking means life-safety or habitability. The honest move is to downgrade
    the severity deliberately if that is wrong."""
    h = builder["headers"]
    job = await a_home(client, builder, "115")
    item = await a_defect(
        client, h, job["id"], description="Soil pipe cracked", severity="blocking"
    )

    resp = await client.post(
        f"/api/v1/field/punch-items/{item['id']}/wont-fix",
        headers=h,
        json={"reason": "Buyer does not mind"},
    )
    assert resp.status_code == 409
    assert "marked as blocking" in resp.json()["detail"]


async def test_status_cannot_be_set_through_a_patch(client, builder):
    """A PATCH that set status would route round the verification step entirely."""
    h = builder["headers"]
    job = await a_home(client, builder, "116")
    item = await a_defect(client, h, job["id"])

    # Refused, not ignored. Dropping the field quietly answers 200 to a request
    # the route did not carry out, and the caller has no way to tell - which is
    # how a cleared punch list ended up scoring zero on the trade scorecard.
    refused = await client.patch(
        f"/api/v1/field/punch-items/{item['id']}",
        headers=h,
        json={"status": "complete", "location": "Landing"},
    )
    assert refused.status_code == 422

    # And nothing in the call was applied, including the part that was legitimate.
    page = await client.get(f"/api/v1/field/punch-items?job_id={job['id']}", headers=h)
    unchanged = next(i for i in page.json()["items"] if i["id"] == item["id"])
    assert unchanged["status"] == "open"
    assert unchanged["location"] != "Landing"

    # The same PATCH without the status is accepted.
    ok = await client.patch(
        f"/api/v1/field/punch-items/{item['id']}", headers=h, json={"location": "Landing"}
    )
    assert ok.status_code == 200
    assert ok.json()["location"] == "Landing"


async def test_an_overdue_defect_says_so(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "117")
    item = await a_defect(client, h, job["id"])
    await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/assign",
        {"vendor_id": builder["vendor"]["id"], "due_date": str(LAST_WEEK)}, expect=200,
    )

    after = (
        await client.get(f"/api/v1/field/punch-items?job_id={job['id']}", headers=h)
    ).json()["items"][0]
    assert after["is_overdue"] is True
    assert after["days_overdue"] == 7


# ============================================== blocking the handover =====


async def test_an_open_defect_stops_the_keys_changing_hands(client, builder):
    """Handing over with a major defect still on the list turns a warranty claim
    into a dispute."""
    h = builder["headers"]
    job = await a_home(client, builder, "118")
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/contract",
        {
            "buyer": {"first_name": "Ama", "last_name": "Boateng", "email": "ama@example.com"},
            "base_price": "420000.00",
        },
    )
    await post(client, h, f"/api/v1/jobs/{job['id']}/contract/sign", None, expect=200)
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/start",
        {"start_date": str(TODAY - timedelta(days=60))}, expect=200,
    )
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)

    await a_defect(client, h, job["id"], description="Boiler flue not sealed", severity="major")

    resp = await client.post(f"/api/v1/jobs/{job['id']}/close", headers=h, json={})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "unresolved defect" in detail
    assert "Boiler flue" in detail


async def test_a_cosmetic_defect_does_not_stop_a_handover(client, builder):
    """A buyer will take the house and a decorator comes back."""
    h = builder["headers"]
    job = await a_home(client, builder, "119")
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/contract",
        {
            "buyer": {"first_name": "Ama", "last_name": "Boateng", "email": "ama2@example.com"},
            "base_price": "420000.00",
        },
    )
    await post(client, h, f"/api/v1/jobs/{job['id']}/contract/sign", None, expect=200)
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/start",
        {"start_date": str(TODAY - timedelta(days=60))}, expect=200,
    )
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)

    await a_defect(client, h, job["id"], severity="cosmetic")

    closed = await post(client, h, f"/api/v1/jobs/{job['id']}/close", {}, expect=200)
    assert closed["status"] == "closed"


async def test_the_blocking_list_names_what_is_in_the_way(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "120")
    await a_defect(client, h, job["id"], description="Scuff", severity="cosmetic")
    major = await a_defect(client, h, job["id"], description="Cracked tile", severity="major")

    blocking = (
        await client.get(f"/api/v1/field/punch-items/blocking?job_id={job['id']}", headers=h)
    ).json()
    assert [b["id"] for b in blocking] == [major["id"]]


async def test_a_signed_off_defect_no_longer_blocks(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "121")
    item = await a_defect(client, h, job["id"], severity="major")
    await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/assign",
        {"vendor_id": builder["vendor"]["id"]}, expect=200,
    )
    await post(client, h, f"/api/v1/field/punch-items/{item['id']}/submit", {}, expect=200)
    await post(client, h, f"/api/v1/field/punch-items/{item['id']}/verify", None, expect=200)

    blocking = (
        await client.get(f"/api/v1/field/punch-items/blocking?job_id={job['id']}", headers=h)
    ).json()
    assert blocking == []


# ==================================================== inspections =====


async def test_a_failed_check_needs_a_note(client, builder):
    """Somebody has to send a trade back for it, and "fail" on its own does not say
    what to fix."""
    h = builder["headers"]
    job = await a_home(client, builder, "122")
    insp = await an_inspection(client, h, job["id"])
    check = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/items",
        {"description": "Hurricane ties at truss connections"},
    )

    resp = await client.post(
        f"/api/v1/field/inspections/items/{check['id']}/result",
        headers=h,
        json={"result": "fail"},
    )
    assert resp.status_code == 409
    assert "needs a note" in resp.json()["detail"]


async def test_a_failed_inspection_becomes_a_punch_list(client, builder):
    """Retyping thirty failed checks is how three of them get missed."""
    h = builder["headers"]
    job = await a_home(client, builder, "123")
    insp = await an_inspection(client, h, job["id"])

    failures = ["Hurricane ties missing", "Blocking absent at the stair opening"]
    for description in failures:
        check = await post(
            client, h, f"/api/v1/field/inspections/{insp['id']}/items",
            {"description": description, "vendor_id": builder["vendor"]["id"]},
        )
        await post(
            client, h, f"/api/v1/field/inspections/items/{check['id']}/result",
            {"result": "fail", "notes": "Seen on site"}, expect=200,
        )
    passing = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/items", {"description": "Straps"}
    )
    await post(
        client, h, f"/api/v1/field/inspections/items/{passing['id']}/result",
        {"result": "pass"}, expect=200,
    )
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed", "failure_reason": "Framing not to drawing"}, expect=200,
    )

    created = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/punch-from-failures", None
    )
    assert len(created) == 2
    assert {c["description"] for c in created} == set(failures)
    # A municipal failure is a legal stop, so anything from one blocks by
    # definition rather than by somebody's judgement.
    assert all(c["severity"] == "blocking" for c in created)
    # The trade was on the check, so the defect is already theirs.
    assert all(c["status"] == "assigned" for c in created)


async def test_generating_the_punch_list_twice_does_not_duplicate_it(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "124")
    insp = await an_inspection(client, h, job["id"])
    check = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/items", {"description": "Ties"}
    )
    await post(
        client, h, f"/api/v1/field/inspections/items/{check['id']}/result",
        {"result": "fail", "notes": "Missing"}, expect=200,
    )
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed", "failure_reason": "See checklist"}, expect=200,
    )

    first = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/punch-from-failures", None
    )
    second = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/punch-from-failures", None
    )
    assert len(first) == 1
    assert second == []


async def test_nothing_failed_means_nothing_to_generate(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "125")
    insp = await an_inspection(client, h, job["id"])

    resp = await client.post(
        f"/api/v1/field/inspections/{insp['id']}/punch-from-failures", headers=h
    )
    assert resp.status_code == 409
    assert "Nothing failed" in resp.json()["detail"]


async def test_a_re_inspection_points_at_what_it_is_retrying(client, builder):
    """The chain answers "how many times did this fail before it passed", which is
    what tells a builder whose work is costing them re-inspection fees."""
    h = builder["headers"]
    job = await a_home(client, builder, "126")
    insp = await an_inspection(client, h, job["id"])
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed", "failure_reason": "Framing not to drawing"}, expect=200,
    )

    retry = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/reinspect",
        {"scheduled_date": str(TODAY + timedelta(days=3)), "fee": "150.00"},
    )
    assert retry["reinspects_id"] == insp["id"]
    assert retry["result"] == "scheduled"
    assert retry["fee"] == "150.00"
    assert "re-inspection" in retry["name"]


async def test_a_passed_inspection_has_nothing_to_retry(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "127")
    insp = await an_inspection(client, h, job["id"])
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "passed"}, expect=200,
    )

    resp = await client.post(
        f"/api/v1/field/inspections/{insp['id']}/reinspect",
        headers=h,
        json={"scheduled_date": str(TODAY + timedelta(days=3))},
    )
    assert resp.status_code == 409
    assert "did not fail" in resp.json()["detail"]


async def test_a_result_cannot_be_recorded_twice(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "128")
    insp = await an_inspection(client, h, job["id"])
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "passed"}, expect=200,
    )

    resp = await client.post(
        f"/api/v1/field/inspections/{insp['id']}/result", headers=h, json={"result": "failed"}
    )
    assert resp.status_code == 409


async def test_a_failure_with_no_explanation_anywhere_is_refused(client, builder):
    """Either fail the individual checks or say what went wrong - otherwise nobody
    knows what to send anyone back for."""
    h = builder["headers"]
    job = await a_home(client, builder, "129")
    insp = await an_inspection(client, h, job["id"])

    resp = await client.post(
        f"/api/v1/field/inspections/{insp['id']}/result", headers=h, json={"result": "failed"}
    )
    assert resp.status_code == 409
    assert "needs a reason" in resp.json()["detail"]


async def test_the_checklist_is_fixed_once_the_inspection_happened(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "130")
    insp = await an_inspection(client, h, job["id"])
    await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "passed"}, expect=200,
    )

    resp = await client.post(
        f"/api/v1/field/inspections/{insp['id']}/items",
        headers=h,
        json={"description": "Added afterwards"},
    )
    assert resp.status_code == 409
    assert "checklist is fixed" in resp.json()["detail"]


# ===================================================== daily logs =====


async def test_filing_twice_in_a_day_updates_rather_than_duplicates(client, builder):
    """Two logs for one day makes the record that is supposed to substantiate a
    delay claim contradict itself."""
    h = builder["headers"]
    job = await a_home(client, builder, "131")

    await post(
        client, h, "/api/v1/field/daily-logs",
        {"job_id": job["id"], "weather": "Rain, 14C", "work_performed": "Interior only"},
    )
    await post(
        client, h, "/api/v1/field/daily-logs",
        {"job_id": job["id"], "crew_count": 6, "delays": "Half day lost to rain"},
    )

    logs = (
        await client.get(f"/api/v1/field/daily-logs?job_id={job['id']}", headers=h)
    ).json()
    assert logs["total"] == 1
    entry = logs["items"][0]
    assert entry["weather"] == "Rain, 14C"          # kept from the first filing
    assert entry["crew_count"] == 6                 # added by the second
    assert entry["delays"] == "Half day lost to rain"


async def test_a_log_cannot_be_filed_for_a_future_day(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "132")

    resp = await client.post(
        "/api/v1/field/daily-logs",
        headers=h,
        json={"job_id": job["id"], "log_date": str(TODAY + timedelta(days=1))},
    )
    assert resp.status_code == 409
    assert "has not happened yet" in resp.json()["detail"]


async def test_separate_days_are_separate_logs(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "133")
    for days_ago in range(3):
        await post(
            client, h, "/api/v1/field/daily-logs",
            {
                "job_id": job["id"],
                "log_date": str(TODAY - timedelta(days=days_ago)),
                "work_performed": f"Day {days_ago}",
            },
        )

    logs = (await client.get(f"/api/v1/field/daily-logs?job_id={job['id']}", headers=h)).json()
    assert logs["total"] == 3


# ======================================================== summary =====


async def test_the_summary_counts_what_the_superintendent_scans_for(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "134")

    await a_defect(client, h, job["id"], description="Scuff", severity="cosmetic")
    await a_defect(client, h, job["id"], description="Cracked tile", severity="major")
    walkthrough = await a_defect(
        client, h, job["id"], description="Door sticks", from_walkthrough=True
    )
    await post(
        client, h, f"/api/v1/field/punch-items/{walkthrough['id']}/assign",
        {"vendor_id": builder["vendor"]["id"], "due_date": str(LAST_WEEK)}, expect=200,
    )
    await post(client, h, f"/api/v1/field/punch-items/{walkthrough['id']}/submit", {}, expect=200)

    summary = (await client.get(f"/api/v1/field/summary?job_id={job['id']}", headers=h)).json()
    assert summary["open_punch"] == 3
    assert summary["awaiting_review"] == 1
    assert summary["overdue_punch"] == 1
    assert summary["blocking_handover"] == 2      # the cosmetic one does not
    assert summary["from_walkthrough"] == 1


async def test_field_work_does_not_leak_between_builders(client, builder):
    h = builder["headers"]
    job = await a_home(client, builder, "135")
    await a_defect(client, h, job["id"])
    other = await signup(client, "Rival Site", "rival-field@rival.example.com")

    assert (await client.get("/api/v1/field/punch-items", headers=other)).json()["total"] == 0
    assert (await client.get("/api/v1/field/inspections", headers=other)).json()["total"] == 0
    assert (
        await client.get(f"/api/v1/field/punch-items/blocking?job_id={job['id']}", headers=other)
    ).status_code == 404
