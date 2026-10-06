"""The design studio: an allowance, a cutoff, and a document that comes back.

Before this the studio could list appointments and mark a document signed. It
could not say what a buyer had left to spend, could not stop a structural choice
made after the frame was committed, and had nowhere to record why an addendum was
refused.

The three rules worth pinning down:

  * **the allowance counts pending choices.** A buyer who picked something in the
    room believes they have committed to it. A budget that ignores it produces a
    bill that does not.
  * **the structural cutoff is enforced, not displayed.** Otherwise a buyer is
    told yes in the studio and a superintendent finds out on site.
  * **a declined document needs a reason.** It means the selections behind it are
    wrong, and the next version will be wrong the same way if nobody says how.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tests.conftest import post, signup

TODAY = date.today()
NEXT_WEEK = datetime.now(UTC) + timedelta(days=7)
LAST_WEEK = datetime.now(UTC) - timedelta(days=7)


# --- helpers ------------------------------------------------------------------


async def sold_home(client, builder, lot_number="70", allowance="15000.00"):
    """A home with a buyer and an agreed options allowance."""
    h = builder["headers"]
    lot = await post(
        client, h, f"/api/v1/communities/{builder['community']['id']}/lots",
        {"lot_number": lot_number, "address": f"{lot_number} Studio Way"},
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
        client, h, f"/api/v1/jobs/{job['id']}/contract",
        {
            "buyer": {"first_name": "Ama", "last_name": "Boateng", "email": "ama@example.com"},
            "base_price": "420000.00",
        },
    )
    if allowance is not None:
        await post(
            client, h, f"/api/v1/jobs/{job['id']}/allowance", {"amount": allowance}, expect=200
        )
    return job


async def book(client, headers, job_id, **overrides):
    payload = {"job_id": job_id, "scheduled_for": NEXT_WEEK.isoformat()}
    payload.update(overrides)
    return await post(client, headers, "/api/v1/appointments", payload)


async def structural_option(client, headers, code="STR-1"):
    return await post(
        client, headers, "/api/v1/options",
        {
            "code": code,
            "name": "Moved staircase",
            "category": "structural",
            "price": "6500.00",
            "is_structural": True,
        },
    )


# ============================================================== allowance =====


async def test_the_allowance_is_agreed_and_read_back(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "70", allowance="15000.00")

    summary = (await client.get(f"/api/v1/jobs/{job['id']}/allowance", headers=h)).json()
    assert Decimal(summary["allowance"]) == Decimal("15000.00")
    assert Decimal(summary["spent"]) == Decimal("0")
    assert Decimal(summary["remaining"]) == Decimal("15000.00")
    assert summary["is_over"] is False


async def test_an_allowance_needs_a_contract_to_live_in(client, builder):
    """It is money inside the base price, not a preference on the home."""
    h = builder["headers"]
    lot = await post(
        client, h, f"/api/v1/communities/{builder['community']['id']}/lots",
        {"lot_number": "71"},
    )
    job = await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": builder["community"]["id"],
            "lot_id": lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/allowance", headers=h, json={"amount": "10000.00"}
    )
    assert resp.status_code == 409
    assert "no contract" in resp.json()["detail"]


async def test_a_pending_choice_already_counts_against_the_budget(client, builder):
    """A buyer who picked something in the room believes they have committed. A
    budget that ignores it produces a bill that does not."""
    h = builder["headers"]
    job = await sold_home(client, builder, "72", allowance="15000.00")

    await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections",
        {"option_id": builder["option"]["id"]},
    )
    summary = (await client.get(f"/api/v1/jobs/{job['id']}/allowance", headers=h)).json()
    assert Decimal(summary["pending"]) == Decimal("8500.00")
    assert Decimal(summary["confirmed"]) == Decimal("0")
    assert Decimal(summary["remaining"]) == Decimal("6500.00")


async def test_confirming_moves_the_money_without_changing_the_total(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "73", allowance="15000.00")
    await post(client, h, f"/api/v1/jobs/{job['id']}/budget", None)
    sel = await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections",
        {"option_id": builder["option"]["id"]},
    )
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections/{sel['id']}/confirm", None, expect=200
    )

    summary = (await client.get(f"/api/v1/jobs/{job['id']}/allowance", headers=h)).json()
    assert Decimal(summary["confirmed"]) == Decimal("8500.00")
    assert Decimal(summary["pending"]) == Decimal("0")
    assert Decimal(summary["spent"]) == Decimal("8500.00")


async def test_going_over_the_allowance_is_shown_not_blocked(client, builder):
    """Overspending is normal and is how a builder makes margin on options. The
    system says by how much; it does not refuse the sale."""
    h = builder["headers"]
    job = await sold_home(client, builder, "74", allowance="5000.00")

    await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections",
        {"option_id": builder["option"]["id"]},
    )
    summary = (await client.get(f"/api/v1/jobs/{job['id']}/allowance", headers=h)).json()
    assert summary["is_over"] is True
    assert Decimal(summary["over_by"]) == Decimal("3500.00")
    assert Decimal(summary["remaining"]) == Decimal("-3500.00")


async def test_a_cancelled_selection_gives_the_money_back(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "75", allowance="15000.00")
    sel = await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections",
        {"option_id": builder["option"]["id"]},
    )
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections/{sel['id']}/cancel", None, expect=200
    )

    summary = (await client.get(f"/api/v1/jobs/{job['id']}/allowance", headers=h)).json()
    assert Decimal(summary["spent"]) == Decimal("0")


async def test_a_negative_allowance_is_refused(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "76")
    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/allowance", headers=h, json={"amount": "-100.00"}
    )
    assert resp.status_code == 422


# =========================================================== appointments =====


async def test_a_session_goes_in_the_diary_with_a_date(client, builder):
    """`scheduled_for` used to be optional, and the list showed a row reading
    "Not set" - an appointment with no date is a note."""
    h = builder["headers"]
    job = await sold_home(client, builder, "77")

    appt = await book(client, h, job["id"], location="Design studio, Poole")
    assert appt["status"] == "scheduled"
    assert appt["scheduled_for"] is not None
    assert appt["is_open"] is True


async def test_an_appointment_in_the_past_is_refused(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "78")

    resp = await client.post(
        "/api/v1/appointments",
        headers=h,
        json={"job_id": job["id"], "scheduled_for": LAST_WEEK.isoformat()},
    )
    assert resp.status_code == 409
    assert "in the past" in resp.json()["detail"]


async def test_a_home_cannot_hold_two_live_bookings(client, builder):
    """Somebody turns up on the wrong day and nobody knows which record is real."""
    h = builder["headers"]
    job = await sold_home(client, builder, "79")
    await book(client, h, job["id"])

    resp = await client.post(
        "/api/v1/appointments",
        headers=h,
        json={"job_id": job["id"], "scheduled_for": NEXT_WEEK.isoformat()},
    )
    assert resp.status_code == 409
    assert "already has an appointment" in resp.json()["detail"]


async def test_rescheduling_keeps_the_trail_rather_than_editing_the_date(client, builder):
    """A buyer who has moved three times is a fact worth seeing. Editing the date
    in place erases it."""
    h = builder["headers"]
    job = await sold_home(client, builder, "80")
    first = await book(client, h, job["id"])

    later = (datetime.now(UTC) + timedelta(days=21)).isoformat()
    second = await post(
        client, h, f"/api/v1/appointments/{first['id']}/reschedule",
        {"scheduled_for": later, "note": "Buyer away"}, expect=200,
    )
    assert second["id"] != first["id"]
    assert second["rescheduled_from_id"] == first["id"]
    assert second["status"] == "scheduled"

    old = (await client.get(f"/api/v1/appointments/{first['id']}", headers=h)).json()
    assert old["status"] == "cancelled"
    assert old["outcome_note"] == "Buyer away"


async def test_a_no_show_is_its_own_ending(client, builder):
    """A cancellation is somebody telling you. A no-show is a wasted slot and a
    consultant sitting alone for three hours."""
    h = builder["headers"]
    job = await sold_home(client, builder, "81")
    appt = await book(client, h, job["id"])

    missed = await post(
        client, h, f"/api/v1/appointments/{appt['id']}/no-show",
        {"note": "No answer on the day"}, expect=200,
    )
    assert missed["status"] == "no_show"
    assert missed["is_open"] is False


async def test_a_finished_appointment_cannot_be_finished_again(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "82")
    appt = await book(client, h, job["id"])
    await post(client, h, f"/api/v1/appointments/{appt['id']}/complete", {}, expect=200)

    resp = await client.post(f"/api/v1/appointments/{appt['id']}/no-show", headers=h, json={})
    assert resp.status_code == 409
    assert "already complete" in resp.json()["detail"]


async def test_a_handed_over_home_gets_no_studio_session(client, builder):
    """Selections are agreed before the build, not after the buyer has the keys."""
    h = builder["headers"]
    job = await sold_home(client, builder, "83")
    await post(client, h, f"/api/v1/jobs/{job['id']}/contract/sign", None, expect=200)
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/start",
        {"start_date": str(TODAY - timedelta(days=60))}, expect=200,
    )
    await post(client, h, f"/api/v1/jobs/{job['id']}/complete", {}, expect=200)
    await post(client, h, f"/api/v1/jobs/{job['id']}/close", {}, expect=200)

    resp = await client.post(
        "/api/v1/appointments",
        headers=h,
        json={"job_id": job["id"], "scheduled_for": NEXT_WEEK.isoformat()},
    )
    assert resp.status_code == 409
    assert "handed over" in resp.json()["detail"]


# ======================================================= the structural gate =====


async def test_a_structural_option_after_the_cutoff_is_refused(client, builder):
    """The most valuable rule on this screen.

    Without it a buyer is told yes in the studio and a superintendent discovers on
    site that the staircase has moved.
    """
    h = builder["headers"]
    job = await sold_home(client, builder, "84")
    appt = await book(client, h, job["id"])
    await post(
        client, h, f"/api/v1/appointments/{appt['id']}/cutoff",
        {"structural_cutoff": str(TODAY - timedelta(days=1))}, expect=200,
    )
    option = await structural_option(client, h, "STR-A")

    resp = await client.post(
        f"/api/v1/jobs/{job['id']}/selections", headers=h, json={"option_id": option["id"]}
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "structural" in detail
    assert "change order" in detail


async def test_a_non_structural_option_is_unaffected_by_the_cutoff(client, builder):
    """Carpet and worktops can be chosen until far later. Blocking everything at
    the structural date would be simpler and wrong."""
    h = builder["headers"]
    job = await sold_home(client, builder, "85")
    appt = await book(client, h, job["id"])
    await post(
        client, h, f"/api/v1/appointments/{appt['id']}/cutoff",
        {"structural_cutoff": str(TODAY - timedelta(days=1))}, expect=200,
    )

    chosen = await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections",
        {"option_id": builder["option"]["id"]},
    )
    assert chosen["status"] == "pending"


async def test_a_structural_option_before_the_cutoff_is_fine(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "86")
    appt = await book(client, h, job["id"])
    await post(
        client, h, f"/api/v1/appointments/{appt['id']}/cutoff",
        {"structural_cutoff": str(TODAY + timedelta(days=30))}, expect=200,
    )
    option = await structural_option(client, h, "STR-B")

    chosen = await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections", {"option_id": option["id"]}
    )
    assert chosen["status"] == "pending"


async def test_a_cancelled_appointments_cutoff_does_not_govern(client, builder):
    """A stale date from a booking nobody kept should not be deciding what the
    buyer may choose."""
    h = builder["headers"]
    job = await sold_home(client, builder, "87")
    appt = await book(client, h, job["id"])
    await post(
        client, h, f"/api/v1/appointments/{appt['id']}/cutoff",
        {"structural_cutoff": str(TODAY - timedelta(days=5))}, expect=200,
    )
    await post(client, h, f"/api/v1/appointments/{appt['id']}/cancel", {}, expect=200)
    option = await structural_option(client, h, "STR-C")

    chosen = await post(
        client, h, f"/api/v1/jobs/{job['id']}/selections", {"option_id": option["id"]}
    )
    assert chosen["status"] == "pending"


async def test_days_to_cutoff_is_what_the_consultant_leads_with(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "88")
    appt = await book(client, h, job["id"])
    updated = await post(
        client, h, f"/api/v1/appointments/{appt['id']}/cutoff",
        {"structural_cutoff": str(TODAY + timedelta(days=10))}, expect=200,
    )
    assert updated["days_to_cutoff"] == 10
    assert updated["structural_cutoff_passed"] is False


# ============================================================= signatures =====


async def test_a_document_goes_out_and_comes_back_signed(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "89")
    req = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": job["id"],
            "document_name": "Design selections addendum #1",
            "signer_name": "Ama Boateng",
            "signer_email": "ama@example.com",
        },
    )
    assert req["status"] == "draft"

    sent = await post(client, h, f"/api/v1/signatures/{req['id']}/send", None, expect=200)
    assert sent["status"] == "sent"
    assert sent["is_outstanding"] is True

    viewed = await post(client, h, f"/api/v1/signatures/{req['id']}/viewed", None, expect=200)
    assert viewed["status"] == "viewed"

    signed = await post(
        client, h, f"/api/v1/signatures/{req['id']}/mark-signed", None, expect=200
    )
    assert signed["status"] == "signed"
    assert signed["is_outstanding"] is False


async def test_a_draft_cannot_be_signed_without_being_sent(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "90")
    req = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": job["id"],
            "document_name": "Addendum",
            "signer_name": "Ama",
            "signer_email": "ama@example.com",
        },
    )

    resp = await client.post(f"/api/v1/signatures/{req['id']}/mark-signed", headers=h)
    assert resp.status_code == 409
    assert "Next steps from here: sent" in resp.json()["detail"]


async def test_a_declined_document_records_why(client, builder):
    """The most valuable field on the record: the selections behind it are wrong,
    and the next version will be wrong the same way if nobody says how."""
    h = builder["headers"]
    job = await sold_home(client, builder, "91")
    req = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": job["id"],
            "document_name": "Addendum",
            "signer_name": "Ama",
            "signer_email": "ama@example.com",
        },
    )
    await post(client, h, f"/api/v1/signatures/{req['id']}/send", None, expect=200)

    declined = await post(
        client, h, f"/api/v1/signatures/{req['id']}/decline",
        {"reason": "Worktop is the wrong colour on page 2"}, expect=200,
    )
    assert declined["status"] == "declined"
    assert "wrong colour" in declined["decline_reason"]


async def test_declining_with_no_reason_is_refused(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "92")
    req = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": job["id"],
            "document_name": "Addendum",
            "signer_name": "Ama",
            "signer_email": "ama@example.com",
        },
    )
    await post(client, h, f"/api/v1/signatures/{req['id']}/send", None, expect=200)

    resp = await client.post(
        f"/api/v1/signatures/{req['id']}/decline", headers=h, json={"reason": ""}
    )
    assert resp.status_code == 422


async def test_chasing_is_counted(client, builder):
    """So "we only sent it once" cannot be claimed when the record says otherwise."""
    h = builder["headers"]
    job = await sold_home(client, builder, "93")
    req = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": job["id"],
            "document_name": "Addendum",
            "signer_name": "Ama",
            "signer_email": "ama@example.com",
        },
    )
    await post(client, h, f"/api/v1/signatures/{req['id']}/send", None, expect=200)

    for expected in (1, 2):
        chased = await post(
            client, h, f"/api/v1/signatures/{req['id']}/remind", None, expect=200
        )
        assert chased["reminders_sent"] == expected
    assert chased["last_reminder_at"] is not None


async def test_a_signed_document_cannot_be_chased_or_voided(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "94")
    req = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": job["id"],
            "document_name": "Addendum",
            "signer_name": "Ama",
            "signer_email": "ama@example.com",
        },
    )
    await post(client, h, f"/api/v1/signatures/{req['id']}/send", None, expect=200)
    await post(client, h, f"/api/v1/signatures/{req['id']}/mark-signed", None, expect=200)

    assert (
        await client.post(f"/api/v1/signatures/{req['id']}/remind", headers=h)
    ).status_code == 409

    voided = await client.post(
        f"/api/v1/signatures/{req['id']}/void", headers=h, json={"reason": "Wrong version"}
    )
    assert voided.status_code == 409
    assert "superseding" in voided.json()["detail"]


async def test_a_document_sent_in_error_can_be_withdrawn(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "95")
    req = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": job["id"],
            "document_name": "Addendum",
            "signer_name": "Ama",
            "signer_email": "ama@example.com",
        },
    )
    await post(client, h, f"/api/v1/signatures/{req['id']}/send", None, expect=200)

    voided = await post(
        client, h, f"/api/v1/signatures/{req['id']}/void",
        {"reason": "Superseded by addendum #2"}, expect=200,
    )
    assert voided["status"] == "expired"
    assert "Superseded" in voided["void_reason"]


# ================================================= summary and isolation =====


async def test_the_summary_counts_what_the_studio_scans_for(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "96")
    appt = await book(client, h, job["id"])
    await post(
        client, h, f"/api/v1/appointments/{appt['id']}/cutoff",
        {"structural_cutoff": str(TODAY + timedelta(days=10))}, expect=200,
    )
    req = await post(
        client, h, "/api/v1/signatures",
        {
            "job_id": job["id"],
            "document_name": "Addendum",
            "signer_name": "Ama",
            "signer_email": "ama@example.com",
        },
    )
    await post(client, h, f"/api/v1/signatures/{req['id']}/send", None, expect=200)

    summary = (await client.get("/api/v1/studio/summary", headers=h)).json()
    assert summary["scheduled"] == 1
    assert summary["awaiting_signature"] == 1
    assert summary["signed"] == 0
    assert summary["cutoff_soon"] == 1


async def test_a_cutoff_far_away_is_not_urgent(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "97")
    appt = await book(client, h, job["id"])
    await post(
        client, h, f"/api/v1/appointments/{appt['id']}/cutoff",
        {"structural_cutoff": str(TODAY + timedelta(days=120))}, expect=200,
    )

    summary = (await client.get("/api/v1/studio/summary", headers=h)).json()
    assert summary["cutoff_soon"] == 0


async def test_appointments_can_be_narrowed_to_one_home(client, builder):
    h = builder["headers"]
    one = await sold_home(client, builder, "98")
    two = await sold_home(client, builder, "99")
    await book(client, h, one["id"])
    await book(client, h, two["id"])

    only = (await client.get(f"/api/v1/appointments?job_id={one['id']}", headers=h)).json()
    assert only["total"] == 1
    assert only["items"][0]["job_id"] == one["id"]


async def test_the_studio_does_not_leak_between_builders(client, builder):
    h = builder["headers"]
    job = await sold_home(client, builder, "100")
    await book(client, h, job["id"])
    other = await signup(client, "Rival Studio", "rival-studio@rival.example.com")

    assert (await client.get("/api/v1/appointments", headers=other)).json()["total"] == 0
    assert (await client.get("/api/v1/signatures", headers=other)).json()["total"] == 0
    assert (
        await client.get(f"/api/v1/jobs/{job['id']}/allowance", headers=other)
    ).status_code == 404
