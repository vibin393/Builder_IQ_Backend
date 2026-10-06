"""The sales pipeline: capturing leads, working them, and how they end.

Before this, a lead had five fields and could be created, listed and converted.
The rules asserted here are the ones that make the difference between a list of
names and a pipeline somebody can run a business from:

  * a lead cannot skip qualification on its way to being a buyer - that step is
    where the builder finds out whether the buyer can actually pay
  * a lost lead needs a reason, because a pipeline of unexplained losses teaches
    the business nothing
  * duplicates are flagged and not blocked, because the same family enquiring
    about two plans is legitimate and a walk-in at the desk must not be refused
  * every status change and assignment is logged without anybody typing it
  * the queues - overdue, unassigned - mean what a sales manager means by them
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.contract import Buyer
from app.models.crm import Lead
from tests.conftest import post, signup

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
LAST_MONTH = TODAY - timedelta(days=30)
NEXT_WEEK = TODAY + timedelta(days=7)


# --- helpers ------------------------------------------------------------------


async def make_lead(client, headers, **overrides):
    payload = {
        "first_name": "Ama",
        "last_name": "Boateng",
        "email": "ama.boateng@example.com",
        "phone": "01202 555111",
        "source": "website",
    }
    payload.update(overrides)
    return await post(client, headers, "/api/v1/leads", payload)


async def qualify(client, headers, lead_id):
    """Walk a lead to qualified, the state most actions require."""
    await post(client, headers, f"/api/v1/leads/{lead_id}/status", {"status": "contacted"}, 200)
    return await post(
        client, headers, f"/api/v1/leads/{lead_id}/status", {"status": "qualified"}, 200
    )


# ============================================================= capture =====


async def test_a_walk_in_with_only_a_name_can_be_captured(client, builder):
    """The most common intake there is. A form that refuses this loses the lead
    entirely, which is worse than an incomplete record."""
    lead = await post(
        client, builder["headers"], "/api/v1/leads",
        {"first_name": "Jo", "last_name": "Nkemdirim", "source": "walk_in"},
    )
    assert lead["status"] == "new"
    assert lead["full_name"] == "Jo Nkemdirim"
    assert lead["email"] is None


async def test_capture_records_everything_the_form_collects(client, builder):
    """The fields exist to be used. If intake collects a budget and the record
    drops it, the plan-matching query is built on nothing."""
    lead = await make_lead(
        client, builder["headers"],
        mobile="07700 900111",
        preferred_contact="text",
        best_time_to_call="Evenings",
        city="Poole",
        postcode="BH15 1AA",
        community_id=builder["community"]["id"],
        plan_id=builder["plan"]["id"],
        price_min="380000.00",
        price_max="450000.00",
        bedrooms_needed=4,
        desired_move_in=str(NEXT_WEEK),
        is_prequalified=True,
        lender_name="Halifax",
        financing_type="conventional",
        has_home_to_sell=True,
        rating="hot",
    )
    assert lead["mobile"] == "07700 900111"
    assert lead["preferred_contact"] == "text"
    assert lead["bedrooms_needed"] == 4
    assert lead["is_prequalified"] is True
    assert lead["financing_type"] == "conventional"
    assert lead["is_contingent"] is True    # they have a house to sell
    assert lead["rating"] == "hot"


async def test_a_budget_maximum_below_the_minimum_is_rejected(client, builder):
    """A typo that would silently exclude this lead from every plan match."""
    resp = await client.post(
        "/api/v1/leads",
        headers=builder["headers"],
        json={
            "first_name": "Wrong",
            "last_name": "Way",
            "price_min": "500000.00",
            "price_max": "400000.00",
        },
    )
    assert resp.status_code == 422


async def test_capture_writes_its_own_first_history_entry(client, builder):
    """The trail starts at capture, so "where did this come from" is answerable
    without anybody having typed it."""
    lead = await make_lead(client, builder["headers"], source="signage")
    trail = (
        await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=builder["headers"])
    ).json()
    assert len(trail) == 1
    assert trail[0]["is_system"] is True
    assert "signage" in trail[0]["summary"]


async def test_a_lead_cannot_point_at_another_builders_community(client, builder):
    """Checked before the write, so the caller gets "not found" rather than an
    integrity error from the database."""
    other = await signup(client, "Rival Homes", "rival-lead@rival.example.com")
    theirs = await post(
        client, other, "/api/v1/communities", {"code": "RIV", "name": "Rival Park"}
    )

    resp = await client.post(
        "/api/v1/leads",
        headers=builder["headers"],
        json={"first_name": "Cross", "last_name": "Tenant", "community_id": theirs["id"]},
    )
    assert resp.status_code == 404


# ========================================================== duplicates =====


async def test_a_repeat_enquiry_is_flagged_by_email(client, builder):
    h = builder["headers"]
    first = await make_lead(client, h)

    matches = await post(
        client, h, "/api/v1/leads/check-duplicates",
        {"email": "ama.boateng@example.com"}, expect=200,
    )
    assert [m["id"] for m in matches] == [first["id"]]


async def test_a_phone_number_matches_however_it_is_punctuated(client, builder):
    """"01202 555 111" and "01202555111" are the same number to everybody except
    a string comparison."""
    h = builder["headers"]
    first = await make_lead(client, h, email=None, phone="01202 555 111")

    matches = await post(
        client, h, "/api/v1/leads/check-duplicates", {"phone": "01202555111"}, expect=200
    )
    assert [m["id"] for m in matches] == [first["id"]]


async def test_a_mobile_matches_against_a_landline_field_and_back(client, builder):
    """Somebody who gave their mobile as a phone number last time is still the
    same person."""
    h = builder["headers"]
    first = await make_lead(client, h, email=None, phone=None, mobile="07700 900222")

    matches = await post(
        client, h, "/api/v1/leads/check-duplicates", {"phone": "07700900222"}, expect=200
    )
    assert [m["id"] for m in matches] == [first["id"]]


async def test_two_unrelated_people_with_the_same_name_are_not_duplicates(client, builder):
    """Matched on contact details, never on name."""
    h = builder["headers"]
    await make_lead(client, h, first_name="John", last_name="Smith", email="j1@example.com")

    matches = await post(
        client, h, "/api/v1/leads/check-duplicates", {"email": "j2@example.com"}, expect=200
    )
    assert matches == []


async def test_a_duplicate_is_a_warning_and_not_a_block(client, builder):
    """The same family enquiring about two plans is legitimate, and refusing a
    walk-in standing at the desk is worse than a duplicate row."""
    h = builder["headers"]
    await make_lead(client, h)
    second = await make_lead(client, h)      # identical details, still created
    assert second["id"]


async def test_a_short_number_identifies_nobody(client, builder):
    """Guarding against a three-digit extension matching half the database."""
    h = builder["headers"]
    await make_lead(client, h, email=None, phone="01202 555111")

    matches = await post(
        client, h, "/api/v1/leads/check-duplicates", {"phone": "111"}, expect=200
    )
    assert matches == []


async def test_duplicates_do_not_cross_tenants(client, builder):
    h = builder["headers"]
    await make_lead(client, h)
    other = await signup(client, "Rival Two", "rival-dup@rival.example.com")

    matches = await post(
        client, other, "/api/v1/leads/check-duplicates",
        {"email": "ama.boateng@example.com"}, expect=200,
    )
    assert matches == []


# ====================================================== the transitions =====


async def test_a_lead_walks_forward_through_the_funnel(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)

    for step in ("contacted", "qualified", "appointment"):
        moved = await post(
            client, h, f"/api/v1/leads/{lead['id']}/status", {"status": step}, expect=200
        )
        assert moved["status"] == step


async def test_qualification_cannot_be_skipped_on_the_way_to_a_sale(client, builder):
    """The single most expensive mistake this feature prevents: a job started for
    somebody who was never checked and never completes."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    resp = await client.post(
        f"/api/v1/leads/{lead['id']}/status", headers=h, json={"status": "appointment"}
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "cannot move from new to appointment" in detail
    # The refusal names the legal next steps rather than just saying no.
    assert "qualified" in detail


async def test_a_lead_can_cool_back_to_contacted_but_not_to_new(client, builder):
    """"New" means nobody has spoken to them. Once somebody has, that is a fact
    and the queues must not be able to un-know it."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    await qualify(client, h, lead["id"])

    cooled = await post(
        client, h, f"/api/v1/leads/{lead['id']}/status", {"status": "contacted"}, expect=200
    )
    assert cooled["status"] == "contacted"

    resp = await client.post(
        f"/api/v1/leads/{lead['id']}/status", headers=h, json={"status": "new"}
    )
    assert resp.status_code == 409


async def test_status_cannot_be_used_to_convert(client, builder):
    """Conversion creates a buyer record. Routing round it would leave a lead
    marked converted with nothing to point at."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    await qualify(client, h, lead["id"])

    resp = await client.post(
        f"/api/v1/leads/{lead['id']}/status", headers=h, json={"status": "converted"}
    )
    assert resp.status_code == 409
    assert "Convert to buyer" in resp.json()["detail"]


async def test_patching_a_lead_cannot_move_its_status(client, builder):
    """The transition check lives on one route. A PATCH that set status would
    route round it and write no history."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    patched = (
        await client.patch(
            f"/api/v1/leads/{lead['id']}", headers=h, json={"status": "converted", "city": "Poole"}
        )
    ).json()
    assert patched["status"] == "new"        # ignored
    assert patched["city"] == "Poole"        # applied


async def test_every_status_change_writes_itself_into_the_history(client, builder):
    """A history containing only what somebody remembered to type is not a
    history."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    await qualify(client, h, lead["id"])

    trail = (await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=h)).json()
    changes = [a for a in trail if a["activity_type"] == "status_change"]
    assert len(changes) == 2
    assert all(a["is_system"] for a in changes)
    assert any("new to contacted" in a["summary"] for a in changes)


async def test_setting_the_status_it_already_has_changes_nothing(client, builder):
    """Idempotent, and not worth a history entry - a UI that fires twice should
    not litter the trail."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    same = await post(
        client, h, f"/api/v1/leads/{lead['id']}/status", {"status": "new"}, expect=200
    )
    assert same["status"] == "new"
    trail = (await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=h)).json()
    assert len(trail) == 1      # just the capture entry


# ========================================================= assignment =====


async def test_a_lead_can_be_assigned_and_unassigned(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)
    me = (await client.get("/api/v1/users/me", headers=h)).json()

    assigned = await post(
        client, h, f"/api/v1/leads/{lead['id']}/assign",
        {"assigned_to_id": me["id"]}, expect=200,
    )
    assert assigned["assigned_to_id"] == me["id"]

    cleared = await post(
        client, h, f"/api/v1/leads/{lead['id']}/assign", {"assigned_to_id": None}, expect=200
    )
    assert cleared["assigned_to_id"] is None


async def test_claiming_a_lead_puts_it_in_your_own_pipeline(client, builder):
    """One click, because "I am picking this up" is the commonest action there is
    on an unassigned lead."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    me = (await client.get("/api/v1/users/me", headers=h)).json()

    claimed = await post(client, h, f"/api/v1/leads/{lead['id']}/claim", None, expect=200)
    assert claimed["assigned_to_id"] == me["id"]


async def test_a_lead_cannot_be_assigned_to_another_builders_staff(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)
    other = await signup(client, "Rival Three", "rival-assign@rival.example.com")
    them = (await client.get("/api/v1/users/me", headers=other)).json()

    resp = await client.post(
        f"/api/v1/leads/{lead['id']}/assign", headers=h, json={"assigned_to_id": them["id"]}
    )
    assert resp.status_code == 404


async def test_a_closed_lead_cannot_be_assigned(client, builder):
    """Assignment means "you are working this". A lost lead is not being worked."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    await post(client, h, f"/api/v1/leads/{lead['id']}/lost", {"reason": "Bought elsewhere"}, 200)
    me = (await client.get("/api/v1/users/me", headers=h)).json()

    resp = await client.post(
        f"/api/v1/leads/{lead['id']}/assign", headers=h, json={"assigned_to_id": me["id"]}
    )
    assert resp.status_code == 409


async def test_a_bulk_assign_reports_what_it_skipped(client, builder):
    """A manager handing out a batch should not lose the rest because one had
    already closed."""
    h = builder["headers"]
    me = (await client.get("/api/v1/users/me", headers=h)).json()

    ok_one = await make_lead(client, h, email="b1@example.com")
    ok_two = await make_lead(client, h, email="b2@example.com")
    closed = await make_lead(client, h, email="b3@example.com")
    await post(client, h, f"/api/v1/leads/{closed['id']}/lost", {"reason": "Timing"}, 200)

    result = await post(
        client, h, "/api/v1/leads/bulk-assign",
        {
            "lead_ids": [ok_one["id"], ok_two["id"], closed["id"]],
            "assigned_to_id": me["id"],
        },
        expect=200,
    )
    assert result["assigned"] == 2
    assert len(result["skipped"]) == 1
    assert "lost" in result["skipped"][0]


async def test_a_bulk_assign_ignores_ids_that_are_not_ours(client, builder):
    """Counted, not named: the caller learns how many were touched, not which
    strangers' records exist."""
    h = builder["headers"]
    me = (await client.get("/api/v1/users/me", headers=h)).json()
    mine = await make_lead(client, h)

    other = await signup(client, "Rival Four", "rival-bulk@rival.example.com")
    theirs = await make_lead(client, other, email="theirs@example.com")

    result = await post(
        client, h, "/api/v1/leads/bulk-assign",
        {"lead_ids": [mine["id"], theirs["id"]], "assigned_to_id": me["id"]},
        expect=200,
    )
    assert result["assigned"] == 1
    assert result["not_found"] == 1


async def test_assignment_is_logged_without_anybody_typing_it(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)
    await post(client, h, f"/api/v1/leads/{lead['id']}/claim", None, expect=200)

    trail = (await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=h)).json()
    assert any(a["activity_type"] == "assignment" for a in trail)


# ========================================================= activities =====


async def test_logging_a_call_moves_a_new_lead_to_contacted(client, builder):
    """So the "nobody has rung these" queue stays honest."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    await post(
        client, h, f"/api/v1/leads/{lead['id']}/activities",
        {"activity_type": "call", "summary": "Rang about the Maple.", "outcome": "reached"},
    )
    after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=h)).json()
    assert after["status"] == "contacted"
    assert after["last_contacted_on"] == str(TODAY)


async def test_a_note_to_oneself_is_not_contact(client, builder):
    """"Last contacted" must mean somebody actually reached out. A note that
    advanced it would make a neglected lead look freshly worked."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    await post(
        client, h, f"/api/v1/leads/{lead['id']}/activities",
        {"activity_type": "note", "summary": "Looks like a good fit for plot 12."},
    )
    after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=h)).json()
    assert after["status"] == "new"
    assert after["last_contacted_on"] is None


async def test_booking_an_appointment_moves_the_lead_to_appointment(client, builder):
    """Because that is what booking one means."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    await qualify(client, h, lead["id"])

    await post(
        client, h, f"/api/v1/leads/{lead['id']}/activities",
        {"activity_type": "call", "summary": "Booked a site visit.", "outcome": "booked"},
    )
    after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=h)).json()
    assert after["status"] == "appointment"


async def test_not_interested_does_not_silently_lose_the_lead(client, builder):
    """Marking it lost needs a reason, and that is the salesperson's to write
    rather than ours to invent."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    await post(
        client, h, f"/api/v1/leads/{lead['id']}/activities",
        {"activity_type": "call", "summary": "Not looking any more.", "outcome": "not_interested"},
    )
    after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=h)).json()
    assert after["status"] == "contacted"     # moved on, not closed


async def test_a_call_and_the_next_one_are_one_request(client, builder):
    """One action to the person doing it, so one request here."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    await post(
        client, h, f"/api/v1/leads/{lead['id']}/activities",
        {
            "activity_type": "call",
            "summary": "Left a message.",
            "outcome": "left_message",
            "next_follow_up": str(NEXT_WEEK),
        },
    )
    after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=h)).json()
    assert after["next_follow_up"] == str(NEXT_WEEK)


async def test_an_activity_cannot_be_logged_in_the_future(client, builder):
    """That is a follow-up, not a history entry, and conflating them corrupts
    every "days since contact" figure."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    resp = await client.post(
        f"/api/v1/leads/{lead['id']}/activities",
        headers=h,
        json={"activity_type": "call", "summary": "Will ring.", "occurred_on": str(NEXT_WEEK)},
    )
    assert resp.status_code == 409


async def test_a_backdated_call_does_not_undo_a_more_recent_one(client, builder):
    """Logging Monday's call on Friday must not make the lead look older."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    await post(
        client, h, f"/api/v1/leads/{lead['id']}/activities",
        {"activity_type": "call", "summary": "Today.", "occurred_on": str(TODAY)},
    )
    await post(
        client, h, f"/api/v1/leads/{lead['id']}/activities",
        {"activity_type": "call", "summary": "Catching up on last month.",
         "occurred_on": str(LAST_MONTH)},
    )
    after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=h)).json()
    assert after["last_contacted_on"] == str(TODAY)


async def test_the_history_cannot_be_read_across_tenants(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)
    other = await signup(client, "Rival Five", "rival-trail@rival.example.com")

    resp = await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=other)
    assert resp.status_code == 404


# ============================================================ the queues =====


async def test_the_overdue_queue_is_open_leads_whose_date_has_passed(client, builder):
    h = builder["headers"]
    overdue = await make_lead(client, h, email="od@example.com")
    await post(
        client, h, f"/api/v1/leads/{overdue['id']}/follow-up",
        {"next_follow_up": str(YESTERDAY)}, expect=200,
    )
    future = await make_lead(client, h, email="fut@example.com")
    await post(
        client, h, f"/api/v1/leads/{future['id']}/follow-up",
        {"next_follow_up": str(NEXT_WEEK)}, expect=200,
    )

    queue = (await client.get("/api/v1/leads/overdue", headers=h)).json()
    assert [i["id"] for i in queue["items"]] == [overdue["id"]]
    assert queue["items"][0]["follow_up_overdue"] is True


async def test_a_closed_lead_drops_out_of_the_overdue_queue(client, builder):
    """A converted lead with a stale follow-up date is not work."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    await post(
        client, h, f"/api/v1/leads/{lead['id']}/follow-up",
        {"next_follow_up": str(YESTERDAY)}, expect=200,
    )
    assert (await client.get("/api/v1/leads/overdue", headers=h)).json()["total"] == 1

    await post(client, h, f"/api/v1/leads/{lead['id']}/lost", {"reason": "Gone quiet"}, 200)
    assert (await client.get("/api/v1/leads/overdue", headers=h)).json()["total"] == 0


async def test_the_overdue_queue_puts_the_most_neglected_lead_first(client, builder):
    """The top of this list is what somebody should be ringing now."""
    h = builder["headers"]
    recent = await make_lead(client, h, email="r@example.com")
    ancient = await make_lead(client, h, email="a@example.com")
    await post(
        client, h, f"/api/v1/leads/{recent['id']}/follow-up",
        {"next_follow_up": str(YESTERDAY)}, expect=200,
    )
    await post(
        client, h, f"/api/v1/leads/{ancient['id']}/follow-up",
        {"next_follow_up": str(LAST_MONTH)}, expect=200,
    )

    queue = (await client.get("/api/v1/leads/overdue", headers=h)).json()
    assert [i["id"] for i in queue["items"]] == [ancient["id"], recent["id"]]


async def test_the_unassigned_queue_holds_only_open_unowned_leads(client, builder):
    h = builder["headers"]
    me = (await client.get("/api/v1/users/me", headers=h)).json()
    unowned = await make_lead(client, h, email="u1@example.com")
    owned = await make_lead(client, h, email="u2@example.com")
    await post(
        client, h, f"/api/v1/leads/{owned['id']}/assign",
        {"assigned_to_id": me["id"]}, expect=200,
    )

    queue = (await client.get("/api/v1/leads/unassigned", headers=h)).json()
    assert [i["id"] for i in queue["items"]] == [unowned["id"]]


async def test_a_follow_up_cannot_be_set_on_a_closed_lead(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)
    await post(client, h, f"/api/v1/leads/{lead['id']}/lost", {"reason": "No budget"}, 200)

    resp = await client.post(
        f"/api/v1/leads/{lead['id']}/follow-up", headers=h, json={"next_follow_up": str(NEXT_WEEK)}
    )
    assert resp.status_code == 409


# ============================================================== endings =====


async def test_a_lost_lead_needs_a_reason(client, builder):
    """A pipeline full of unexplained losses tells the business nothing."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    resp = await client.post(f"/api/v1/leads/{lead['id']}/lost", headers=h, json={"reason": ""})
    assert resp.status_code == 422      # the schema requires it


async def test_a_loss_records_who_won_it(client, builder):
    """"They went to a competitor on price" is what changes next quarter's plan."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    lost = await post(
        client, h, f"/api/v1/leads/{lead['id']}/lost",
        {"reason": "Beaten on price by 15k", "lost_to": "Barratt"}, expect=200,
    )
    assert lost["status"] == "lost"
    assert lost["lost_to"] == "Barratt"
    assert lost["lost_on"] == str(TODAY)
    # Nothing left to chase, so the follow-up is cleared rather than left to rot
    # in the overdue queue forever.
    assert lost["next_follow_up"] is None


async def test_a_lost_lead_can_come_back(client, builder):
    """They rang again; it happens. Returns to contacted rather than new, because
    somebody has already spoken to them."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    await post(client, h, f"/api/v1/leads/{lead['id']}/lost", {"reason": "Timing"}, 200)

    reopened = await post(
        client, h, f"/api/v1/leads/{lead['id']}/reopen",
        {"note": "Rang back, their sale completed."}, expect=200,
    )
    assert reopened["status"] == "contacted"
    assert reopened["lost_reason"] is None
    assert reopened["lost_on"] is None


async def test_reopening_an_open_lead_is_refused(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)

    resp = await client.post(f"/api/v1/leads/{lead['id']}/reopen", headers=h, json={})
    assert resp.status_code == 409


async def test_converting_creates_the_buyer_and_keeps_the_link(client, builder, db_session):
    """The link is the whole reason the lead survives conversion: without it the
    business can only answer which marketing produced enquiries."""
    h = builder["headers"]
    lead = await make_lead(client, h, source="realtor")
    await qualify(client, h, lead["id"])

    converted = await post(client, h, f"/api/v1/leads/{lead['id']}/convert", None, expect=200)
    lead_tenant = (await db_session.get(Lead, uuid.UUID(lead["id"]))).tenant_id
    assert converted["status"] == "converted"
    assert converted["converted_buyer_id"] is not None
    assert converted["converted_on"] == str(TODAY)

    # Asserted through the session rather than an endpoint: buyers are only
    # readable today as part of a contract, so conversion currently creates a
    # record with no route of its own. Worth fixing when Contracts is built - the
    # side effect is real either way, and this is what proves it.
    buyer = await db_session.get(Buyer, uuid.UUID(converted["converted_buyer_id"]))
    assert buyer is not None
    assert buyer.first_name == "Ama"
    assert buyer.tenant_id == lead_tenant


async def test_an_unqualified_lead_cannot_be_converted(client, builder):
    """Qualification is where the builder finds out whether they can pay."""
    h = builder["headers"]
    lead = await make_lead(client, h)

    resp = await client.post(f"/api/v1/leads/{lead['id']}/convert", headers=h)
    assert resp.status_code == 409
    assert "qualified" in resp.json()["detail"]


async def test_a_lead_with_no_contact_details_cannot_be_converted(client, builder):
    """A buyer will be sent selection deadlines and payment requests. One with no
    email or phone cannot be."""
    h = builder["headers"]
    lead = await post(
        client, h, "/api/v1/leads", {"first_name": "No", "last_name": "Contact"}
    )
    await qualify(client, h, lead["id"])

    resp = await client.post(f"/api/v1/leads/{lead['id']}/convert", headers=h)
    assert resp.status_code == 409
    assert "contact details" in resp.json()["detail"]


async def test_a_lost_lead_cannot_be_converted_without_reopening(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)
    await qualify(client, h, lead["id"])
    await post(client, h, f"/api/v1/leads/{lead['id']}/lost", {"reason": "Timing"}, 200)

    resp = await client.post(f"/api/v1/leads/{lead['id']}/convert", headers=h)
    assert resp.status_code == 409
    assert "Reopen" in resp.json()["detail"]


async def test_converting_twice_is_refused(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h)
    await qualify(client, h, lead["id"])
    await post(client, h, f"/api/v1/leads/{lead['id']}/convert", None, expect=200)

    resp = await client.post(f"/api/v1/leads/{lead['id']}/convert", headers=h)
    assert resp.status_code == 409


async def test_a_converted_lead_cannot_be_archived(client, builder):
    """That record is the only trace of where a real buyer came from."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    await qualify(client, h, lead["id"])
    await post(client, h, f"/api/v1/leads/{lead['id']}/convert", None, expect=200)

    resp = await client.post(f"/api/v1/leads/{lead['id']}/archive", headers=h, json={})
    assert resp.status_code == 409


async def test_a_junk_enquiry_can_be_archived_and_restored(client, builder):
    h = builder["headers"]
    lead = await make_lead(client, h, first_name="asdf", last_name="qwer")

    archived = await post(
        client, h, f"/api/v1/leads/{lead['id']}/archive",
        {"reason": "Bot submission"}, expect=200,
    )
    assert archived["is_archived"] is True
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 0

    restored = await post(client, h, f"/api/v1/leads/{lead['id']}/restore", None, expect=200)
    assert restored["is_archived"] is False
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 1


# ======================================================= list and search =====


async def test_paging_shows_every_lead_exactly_once(client, builder):
    """The property that matters, and the one that silently breaks.

    Ordering by a timestamp alone is not a total order - two enquiries captured in
    the same second tie, and the database may then break the tie differently for
    the page-1 query and the page-2 query, so a lead appears twice or vanishes.
    The repository appends the primary key as a final tiebreaker to make that
    impossible; these five leads are created in the same second, which is exactly
    the case that would expose it.
    """
    h = builder["headers"]
    for i in range(5):
        await make_lead(client, h, email=f"p{i}@example.com", last_name=f"Page{i}")

    seen: list[str] = []
    for page in (1, 2, 3):
        body = (await client.get(f"/api/v1/leads?page={page}&size=2", headers=h)).json()
        assert body["total"] == 5        # the match count, not the page size
        seen.extend(i["id"] for i in body["items"])

    assert len(seen) == 5
    assert len(set(seen)) == 5          # nothing repeated, nothing skipped


async def test_the_list_is_newest_first(client, builder, db_session):
    """A lead list is read from the top: the one that came in this morning is the
    one somebody needs to ring.

    The timestamps are pushed apart deliberately. Captured through the API they
    would all land in the same second, and no ordering rule can distinguish rows
    that genuinely tie.
    """
    h = builder["headers"]
    made = []
    for i in range(3):
        made.append(await make_lead(client, h, email=f"n{i}@example.com", last_name=f"Day{i}"))

    for age, lead in enumerate(made):
        row = await db_session.get(Lead, uuid.UUID(lead["id"]))
        row.created_at = datetime.now(UTC) - timedelta(days=age)
    await db_session.commit()

    body = (await client.get("/api/v1/leads", headers=h)).json()
    assert [i["last_name"] for i in body["items"]] == ["Day0", "Day1", "Day2"]


async def test_search_finds_a_lead_by_any_of_the_things_you_remember(client, builder):
    h = builder["headers"]
    await make_lead(client, h, last_name="Okonkwo", city="Bournemouth", campaign="Spring Open Day")

    for term in ("okonkwo", "bournemouth", "spring open", "555111"):
        hit = (await client.get(f"/api/v1/leads?search={term}", headers=h)).json()
        assert hit["total"] == 1, f"search for {term!r} found {hit['total']}"


async def test_the_list_can_be_filtered_down_to_one_salespersons_pipeline(client, builder):
    h = builder["headers"]
    me = (await client.get("/api/v1/users/me", headers=h)).json()
    mine = await make_lead(client, h, email="m1@example.com")
    await make_lead(client, h, email="m2@example.com")
    await post(client, h, f"/api/v1/leads/{mine['id']}/claim", None, expect=200)

    filtered = (
        await client.get(f"/api/v1/leads?assigned_to_id={me['id']}", headers=h)
    ).json()
    assert [i["id"] for i in filtered["items"]] == [mine["id"]]


async def test_filters_compose(client, builder):
    """Hot leads from a realtor, not hot leads or realtor leads."""
    h = builder["headers"]
    agent = await post(
        client, h, "/api/v1/realtors", {"code": "AG1", "name": "Dana Cole"}
    )
    wanted = await make_lead(
        client, h, email="w@example.com", source="realtor",
        realtor_id=agent["id"], rating="hot",
    )
    await make_lead(client, h, email="c@example.com", source="realtor",
                    realtor_id=agent["id"], rating="cold")
    await make_lead(client, h, email="h@example.com", source="website", rating="hot")

    hits = (
        await client.get("/api/v1/leads?source=realtor&rating=hot", headers=h)
    ).json()
    assert [i["id"] for i in hits["items"]] == [wanted["id"]]


async def test_a_lead_cannot_be_read_across_tenants(client, builder):
    """404 rather than 403 - a 403 would confirm the record exists."""
    h = builder["headers"]
    lead = await make_lead(client, h)
    other = await signup(client, "Rival Six", "rival-read@rival.example.com")

    assert (await client.get(f"/api/v1/leads/{lead['id']}", headers=other)).status_code == 404


# =============================================================== summary =====


async def test_the_summary_counts_what_a_sales_manager_scans_for(client, builder):
    """Open, unowned, overdue, due today - the two middle numbers are where money
    leaks."""
    h = builder["headers"]
    me = (await client.get("/api/v1/users/me", headers=h)).json()

    owned_overdue = await make_lead(client, h, email="s1@example.com")
    await post(client, h, f"/api/v1/leads/{owned_overdue['id']}/claim", None, expect=200)
    await post(
        client, h, f"/api/v1/leads/{owned_overdue['id']}/follow-up",
        {"next_follow_up": str(YESTERDAY)}, expect=200,
    )

    due = await make_lead(client, h, email="s2@example.com")
    await post(
        client, h, f"/api/v1/leads/{due['id']}/follow-up",
        {"next_follow_up": str(TODAY)}, expect=200,
    )

    closed = await make_lead(client, h, email="s3@example.com")
    await post(client, h, f"/api/v1/leads/{closed['id']}/lost", {"reason": "Timing"}, 200)

    summary = (await client.get("/api/v1/leads/summary", headers=h)).json()
    assert summary["open"] == 2
    assert summary["unassigned"] == 1
    assert summary["overdue"] == 1
    assert summary["due_today"] == 1
    assert summary["by_status"]["lost"] == 1
    assert summary["by_status"]["converted"] == 0   # reported, not omitted
    assert me["id"]


async def test_the_summary_breaks_enquiries_down_by_source(client, builder):
    """The report that decides next year's marketing spend, which is why
    conversions are counted alongside enquiries."""
    h = builder["headers"]
    converted = await make_lead(client, h, email="src1@example.com", source="signage")
    await qualify(client, h, converted["id"])
    await post(client, h, f"/api/v1/leads/{converted['id']}/convert", None, expect=200)
    await make_lead(client, h, email="src2@example.com", source="signage")
    await make_lead(client, h, email="src3@example.com", source="social")

    summary = (await client.get("/api/v1/leads/summary", headers=h)).json()
    by_source = {r["source"]: r for r in summary["by_source"]}
    assert by_source["signage"] == {"source": "signage", "leads": 2, "converted": 1}
    assert by_source["social"]["converted"] == 0


# ============================================================== realtors =====


async def test_an_agent_is_a_record_with_a_commission(client, builder):
    """Not three text fields on a lead: the question asked is "what do we owe
    them", and that needs a number on a record."""
    agent = await post(
        client, builder["headers"], "/api/v1/realtors",
        {
            "code": "COLE",
            "name": "Dana Cole",
            "brokerage": "Cole & Co",
            "email": "dana@coleandco.example.com",
            "commission_percent": "2.50",
        },
    )
    assert agent["commission_percent"] == "2.50"
    assert agent["brokerage"] == "Cole & Co"


async def test_two_agents_cannot_share_a_code(client, builder):
    h = builder["headers"]
    await post(client, h, "/api/v1/realtors", {"code": "DUP", "name": "First"})

    resp = await client.post("/api/v1/realtors", headers=h, json={"code": "dup", "name": "Second"})
    assert resp.status_code == 409


async def test_agent_performance_ranks_by_buyers_not_browsers(client, builder):
    """The agent who sends three buyers outranks the one who sends thirty
    browsers, and a raw enquiry count would say the opposite."""
    h = builder["headers"]
    closer = await post(client, h, "/api/v1/realtors", {"code": "CLOSE", "name": "Ada Closer"})
    browser = await post(client, h, "/api/v1/realtors", {"code": "BROWSE", "name": "Bob Browser"})

    won = await make_lead(
        client, h, email="won@example.com", source="realtor", realtor_id=closer["id"]
    )
    await qualify(client, h, won["id"])
    await post(client, h, f"/api/v1/leads/{won['id']}/convert", None, expect=200)

    for i in range(3):
        await make_lead(
            client, h, email=f"br{i}@example.com", source="realtor", realtor_id=browser["id"]
        )

    table = (await client.get("/api/v1/realtors/performance", headers=h)).json()
    rows = {r["code"]: r for r in table}
    assert [r["code"] for r in table][0] == "CLOSE"
    assert rows["CLOSE"]["leads_converted"] == 1
    assert rows["CLOSE"]["conversion_rate"] == 100.0
    assert rows["BROWSE"]["leads_sent"] == 3
    assert rows["BROWSE"]["conversion_rate"] == 0.0


async def test_an_agent_who_has_sent_nothing_has_no_rate_rather_than_zero(client, builder):
    """A new agent has no conversion rate yet, which is a different fact from a
    rate of zero."""
    h = builder["headers"]
    await post(client, h, "/api/v1/realtors", {"code": "NEW", "name": "Fresh Agent"})

    table = (await client.get("/api/v1/realtors/performance", headers=h)).json()
    fresh = next(r for r in table if r["code"] == "NEW")
    assert fresh["leads_sent"] == 0
    assert fresh["conversion_rate"] is None


async def test_an_agent_with_open_leads_cannot_be_archived(client, builder):
    """There is a commission riding on each of them."""
    h = builder["headers"]
    agent = await post(client, h, "/api/v1/realtors", {"code": "BUSY", "name": "Busy Agent"})
    await make_lead(client, h, source="realtor", realtor_id=agent["id"])

    resp = await client.post(f"/api/v1/realtors/{agent['id']}/archive", headers=h, json={})
    assert resp.status_code == 409
    assert "open lead" in resp.json()["detail"]


async def test_an_agent_can_be_archived_once_their_leads_have_closed(client, builder):
    """History survives: every lead they sent keeps pointing at them. That is the
    difference from deleting."""
    h = builder["headers"]
    agent = await post(client, h, "/api/v1/realtors", {"code": "RETIRE", "name": "Retiring"})
    lead = await make_lead(client, h, source="realtor", realtor_id=agent["id"])
    await post(client, h, f"/api/v1/leads/{lead['id']}/lost", {"reason": "Went quiet"}, 200)

    archived = await post(
        client, h, f"/api/v1/realtors/{agent['id']}/archive",
        {"reason": "Left the trade"}, expect=200,
    )
    assert archived["is_archived"] is True

    still = (await client.get(f"/api/v1/leads/{lead['id']}", headers=h)).json()
    assert still["realtor_id"] == agent["id"]

    # And they are out of the picker.
    picker = (await client.get("/api/v1/realtors", headers=h)).json()
    assert agent["id"] not in [a["id"] for a in picker]


@pytest.mark.parametrize(
    "slug,route",
    [("list", "/api/v1/realtors"), ("perf", "/api/v1/realtors/performance")],
)
async def test_agents_do_not_leak_between_builders(client, builder, slug, route):
    await post(client, builder["headers"], "/api/v1/realtors", {"code": "OURS", "name": "Ours"})
    other = await signup(client, f"Rival {slug}", f"rival-{slug}@r.example.com")

    theirs = (await client.get(route, headers=other)).json()
    assert theirs == []
