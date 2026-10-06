"""Inspection checklists and punch ball-in-court.

An inspection that records only a verdict tells you the house failed. One that
records its items tells you what to send the framer back for, which is the
difference between a record and a work list.

The lifecycle itself lives in test_field_ops.py. What is here is the checklist and
the one question that feature raised: who does an item say it is with, given the
column is now on screen and people read it.
"""

from tests.conftest import post


async def _inspection(client, h, job_id, name="Framing"):
    return await post(
        client, h, "/api/v1/field/inspections",
        {"job_id": job_id, "name": name, "inspection_type": "municipal"},
    )


async def _get(client, h, inspection_id, job_id):
    """Re-read the inspection.

    Adding a check returns the check, not its inspection - the caller asked for one
    thing and gets that thing back. So the assertions about the checklist as a whole
    fetch it.
    """
    page = await client.get(f"/api/v1/field/inspections?job_id={job_id}", headers=h)
    return next(i for i in page.json()["items"] if i["id"] == inspection_id)


# ============================================== inspection checklists =====

async def test_an_inspection_carries_a_checklist(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    insp = await _inspection(client, h, job_id)

    for description in ("Fire blocking at penetrations", "Joist hangers fully nailed"):
        await post(
            client, h, f"/api/v1/field/inspections/{insp['id']}/items",
            {"description": description},
        )

    insp = await _get(client, h, insp["id"], job_id)
    assert [i["description"] for i in insp["items"]] == [
        "Fire blocking at penetrations",
        "Joist hangers fully nailed",
    ]
    assert [i["sequence"] for i in insp["items"]] == [1, 2]
    assert all(i["result"] == "pending" for i in insp["items"])
    assert insp["checked_items"] == 0


async def test_a_failed_check_must_say_what_is_wrong(client, sold_job):
    h = sold_job["headers"]
    insp = await _inspection(client, h, sold_job["job"]["id"])
    check = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/items",
        {"description": "Fire blocking at penetrations"},
    )

    refused = await client.post(
        f"/api/v1/field/inspections/items/{check['id']}/result",
        headers=h,
        json={"result": "fail"},
    )
    assert refused.status_code == 409


async def test_recording_item_results_counts_them(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    insp = await _inspection(client, h, job_id)
    ids = [
        (
            await post(
                client, h, f"/api/v1/field/inspections/{insp['id']}/items",
                {"description": d},
            )
        )["id"]
        for d in ("Check A", "Check B", "Check C")
    ]

    await post(
        client, h, f"/api/v1/field/inspections/items/{ids[0]}/result",
        {"result": "pass"}, expect=200,
    )
    await post(
        client, h, f"/api/v1/field/inspections/items/{ids[1]}/result",
        {"result": "not_applicable"}, expect=200,
    )

    insp = await _get(client, h, insp["id"], job_id)
    assert insp["checked_items"] == 2
    results = {i["id"]: i["result"] for i in insp["items"]}
    assert results[ids[0]] == "pass"
    assert results[ids[1]] == "not_applicable"
    assert results[ids[2]] == "pending"


async def test_failed_items_become_the_failure_reason(client, sold_job):
    """The checklist already says what went wrong, so it should not be retyped."""
    h = sold_job["headers"]
    insp = await _inspection(client, h, sold_job["job"]["id"])
    for d in ("Fire blocking at penetrations", "Joist hangers fully nailed"):
        check = await post(
            client, h, f"/api/v1/field/inspections/{insp['id']}/items", {"description": d}
        )
        await post(
            client, h, f"/api/v1/field/inspections/items/{check['id']}/result",
            {"result": "fail", "notes": "not done"}, expect=200,
        )

    # No failure_reason supplied: the items supply it.
    done = await post(
        client, h, f"/api/v1/field/inspections/{insp['id']}/result",
        {"result": "failed"}, expect=200,
    )
    assert done["failure_reason"] == (
        "Fire blocking at penetrations; Joist hangers fully nailed"
    )


async def test_a_failure_with_no_checklist_still_needs_a_reason(client, sold_job):
    h = sold_job["headers"]
    insp = await _inspection(client, h, sold_job["job"]["id"])

    refused = await client.post(
        f"/api/v1/field/inspections/{insp['id']}/result", headers=h, json={"result": "failed"}
    )
    assert refused.status_code == 409


# ================================================ punch ball in court =====

async def test_an_unassigned_defect_sits_with_the_builder(client, sold_job):
    """It used to say it sat with the trade. Which trade?

    The outstanding action on a defect nobody has been given is choosing who fixes
    it, and that is the builder's. The old default read as though somebody was
    already on it.
    """
    h = sold_job["headers"]
    item = await post(
        client, h, "/api/v1/field/punch-items",
        {"job_id": sold_job["job"]["id"], "description": "Scuffed skirting, hallway"},
    )
    assert item["ball_in_court"] == "builder"
    assert item["status"] == "open"


async def test_raising_it_against_a_trade_puts_it_in_their_court(client, sold_job):
    h = sold_job["headers"]
    item = await post(
        client, h, "/api/v1/field/punch-items",
        {
            "job_id": sold_job["job"]["id"],
            "description": "Scuffed skirting, hallway",
            "vendor_id": sold_job["vendor"]["id"],
        },
    )
    assert item["ball_in_court"] == "trade"
    assert item["status"] == "assigned"


async def test_the_court_cannot_be_set_by_hand(client, sold_job):
    """It follows the action now.

    Letting anybody set it made it a label rather than a fact - and the PATCH used
    to drop the field silently, so the caller got a 200 for a change that never
    happened.
    """
    h = sold_job["headers"]
    item = await post(
        client, h, "/api/v1/field/punch-items",
        {"job_id": sold_job["job"]["id"], "description": "Paint touch-up disputed"},
    )
    refused = await client.patch(
        f"/api/v1/field/punch-items/{item['id']}", headers=h, json={"ball_in_court": "builder"}
    )
    assert refused.status_code == 422


async def test_status_cannot_be_set_by_hand_either(client, sold_job):
    """A PATCH that set it would route round the sign-off entirely."""
    h = sold_job["headers"]
    item = await post(
        client, h, "/api/v1/field/punch-items",
        {"job_id": sold_job["job"]["id"], "description": "Cracked tile, ensuite"},
    )
    refused = await client.patch(
        f"/api/v1/field/punch-items/{item['id']}", headers=h, json={"status": "complete"}
    )
    assert refused.status_code == 422

    still = await client.get(
        f"/api/v1/field/punch-items?job_id={sold_job['job']['id']}", headers=h
    )
    assert next(i for i in still.json()["items"] if i["id"] == item["id"])["status"] == "open"


async def test_signing_a_defect_off_closes_its_court(client, sold_job):
    h = sold_job["headers"]
    item = await post(
        client, h, "/api/v1/field/punch-items",
        {
            "job_id": sold_job["job"]["id"],
            "description": "Cracked tile, ensuite",
            "vendor_id": sold_job["vendor"]["id"],
        },
    )
    await post(client, h, f"/api/v1/field/punch-items/{item['id']}/submit", None, expect=200)
    done = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/verify", None, expect=200
    )

    assert done["status"] == "complete"
    assert done["ball_in_court"] == "closed"
    assert done["completed_date"] is not None


async def test_declining_a_defect_closes_its_court_too(client, sold_job):
    """Nobody owes anything once the decision has been taken."""
    h = sold_job["headers"]
    item = await post(
        client, h, "/api/v1/field/punch-items",
        {
            "job_id": sold_job["job"]["id"],
            "description": "Grain mismatch on the newel post",
            "vendor_id": sold_job["vendor"]["id"],
            "severity": "cosmetic",
        },
    )
    done = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/wont-fix",
        {"reason": "Within tolerance; agreed with the buyer"}, expect=200,
    )
    assert done["ball_in_court"] == "closed"


async def test_reopening_it_takes_it_out_of_the_closed_court(client, sold_job):
    """The worst of the three to leave wrong: the defect is open again and anybody
    scanning for who owes the next move would not see it."""
    h = sold_job["headers"]
    item = await post(
        client, h, "/api/v1/field/punch-items",
        {
            "job_id": sold_job["job"]["id"],
            "description": "Grain mismatch on the newel post",
            "vendor_id": sold_job["vendor"]["id"],
            "severity": "cosmetic",
        },
    )
    await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/wont-fix",
        {"reason": "Within tolerance"}, expect=200,
    )
    back = await post(
        client, h, f"/api/v1/field/punch-items/{item['id']}/reopen",
        {"reason": "Buyer raised it again at the second walkthrough"}, expect=200,
    )
    assert back["status"] == "open"
    assert back["ball_in_court"] == "trade"
