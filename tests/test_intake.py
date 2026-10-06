"""Leads that arrive by themselves.

The hard part of automated capture is not parsing - it is what happens when
parsing goes wrong, because there is nobody at the keyboard to notice. So most of
what is asserted here is about failure:

  * a payload we cannot read is **stored and flagged**, never dropped. That single
    property is the difference between a delay and a lost customer.
  * the caller is still told it arrived, because a portal that gets a 500 either
    gives up or retries forever.
  * a resend produces no second lead, because portals retry.
  * a repeat enquiry from somebody we already know is linked to them, not
    duplicated - a webhook has no one to ask.
  * a broken mapping can be fixed and the stored payload replayed.
  * one channel's leaked key cannot be used against another, and cannot reach
    another builder at all.
"""

import uuid

import pytest

from app.services.intake import reset_rate_limits
from tests.conftest import post, signup


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    """The limiter keeps its windows in process memory, so tests would otherwise
    inherit each other's traffic."""
    reset_rate_limits()
    yield
    reset_rate_limits()


async def make_source(client, headers, **overrides):
    payload = {"code": "website", "name": "Website enquiry form", "kind": "web_form"}
    payload.update(overrides)
    return await post(client, headers, "/api/v1/intake/sources", payload)


async def send(client, code, key, body, expect=200):
    resp = await client.post(
        f"/api/v1/intake/hooks/{code}", headers={"X-Intake-Key": key}, json=body
    )
    assert resp.status_code == expect, f"{resp.status_code}: {resp.text}"
    return resp.json()


# ========================================================= configuration =====


async def test_a_channel_hands_back_its_key_once(client, builder):
    """The key is stored hashed, so this response is the only chance to record it."""
    created = await make_source(client, builder["headers"])
    assert created["key"].startswith("cpmt_ik_")
    assert created["post_url"].endswith("/api/v1/intake/hooks/website")
    assert "cannot be shown again" in created["warning"]
    # And the hint is all that is readable afterwards.
    assert created["source"]["key_hint"] == created["key"][-4:]


async def test_the_key_never_appears_again(client, builder):
    """A table of usable credentials readable over the API is a table worth
    stealing."""
    created = await make_source(client, builder["headers"])
    fetched = (
        await client.get(
            f"/api/v1/intake/sources/{created['source']['id']}", headers=builder["headers"]
        )
    ).json()
    assert "key" not in fetched
    assert "key_hash" not in fetched


async def test_only_an_admin_can_issue_a_channel_key(client, builder):
    """A channel key creates records in this builder's account with no login.
    Handing those out is not a sales task."""
    h = builder["headers"]
    await post(
        client, h, "/api/v1/users",
        {"email": "seller@cascade.example.com", "full_name": "Seller",
         "password": "supersecret123", "role": "sales"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "seller@cascade.example.com", "password": "supersecret123"},
    )
    sales = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.post(
        "/api/v1/intake/sources", headers=sales,
        json={"code": "sneaky", "name": "Mine", "kind": "web_form"},
    )
    assert resp.status_code == 403


async def test_two_channels_cannot_share_a_code(client, builder):
    h = builder["headers"]
    await make_source(client, h)
    resp = await client.post(
        "/api/v1/intake/sources", headers=h,
        json={"code": "WEBSITE", "name": "Duplicate", "kind": "web_form"},
    )
    assert resp.status_code == 409


async def test_creating_a_source_does_not_shadow_the_sources_route(client, builder):
    """`/intake/sources` and `/intake/{code}` had the same shape. The public route
    is `/intake/hooks/{code}` so a channel called "sources" cannot swallow the
    configuration endpoint."""
    h = builder["headers"]
    await make_source(client, h, code="sources", name="Awkwardly named")
    listed = (await client.get("/api/v1/intake/sources", headers=h)).json()
    assert [s["code"] for s in listed] == ["sources"]


# ============================================================ the happy path =====


async def test_a_website_form_becomes_a_lead(client, builder):
    h = builder["headers"]
    created = await make_source(client, h, default_campaign="spring-parade")

    ack = await send(
        client, "website", created["key"],
        {
            "first_name": "Ama",
            "last_name": "Boateng",
            "email": "AMA.Boateng@Example.com",
            "telephone": "+44 7700 900123",
            "postcode": "bh151aa",
            "message": "Interested in the Maple, four bedrooms.",
            "bedrooms": "4",
        },
    )
    assert ack == {"received": True, "status": "accepted", "detail": None}

    leads = (await client.get("/api/v1/leads", headers=h)).json()
    assert leads["total"] == 1
    lead = leads["items"][0]
    assert lead["full_name"] == "Ama Boateng"
    assert lead["email"] == "ama.boateng@example.com"      # lower-cased
    assert lead["mobile"] == "07700 900123"                # +44 stripped, spaced
    assert lead["bedrooms_needed"] == 4
    assert lead["campaign"] == "spring-parade"             # from the channel
    assert lead["community_id"] is None


async def test_the_channel_fills_in_what_the_form_cannot_know(client, builder):
    """A website form knows nothing about communities or campaigns. Without these
    defaults every automated lead arrives unattributed."""
    h = builder["headers"]
    created = await make_source(
        client, h,
        default_community_id=builder["community"]["id"],
        default_campaign="autumn-open-day",
        lead_source="portal",
    )
    await send(client, "website", created["key"], {"name": "Jo Nkemdirim"})

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["community_id"] == builder["community"]["id"]
    assert lead["campaign"] == "autumn-open-day"
    assert lead["source"] == "portal"


async def test_one_name_field_is_split_rather_than_refused(client, builder):
    """Plenty of forms collect a single name box. Refusing them loses the lead."""
    h = builder["headers"]
    created = await make_source(client, h)
    await send(client, "website", created["key"], {"name": "Mary Anne Okonkwo"})

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["first_name"] == "Mary Anne"
    assert lead["last_name"] == "Okonkwo"


async def test_a_single_word_name_is_still_captured(client, builder):
    """Better a lead called "Dana" with a placeholder surname than no lead."""
    h = builder["headers"]
    created = await make_source(client, h)
    await send(client, "website", created["key"], {"name": "Dana", "email": "d@example.com"})

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["first_name"] == "Dana"
    assert "no surname" in lead["last_name"]


async def test_the_trail_records_that_nobody_typed_this(client, builder):
    h = builder["headers"]
    created = await make_source(client, h)
    await send(client, "website", created["key"], {"name": "Tom Bergeron"})

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    trail = (await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=h)).json()
    assert any("Captured automatically" in a["summary"] for a in trail)
    assert all(a["is_system"] for a in trail)
    assert all(a["user_id"] is None for a in trail)   # no actor: nobody did it


async def test_a_portal_reference_is_kept_where_there_is_no_column_for_it(client, builder):
    """Losing the property reference means nobody can tie the enquiry back to the
    listing it came from."""
    h = builder["headers"]
    created = await make_source(client, h, code="rightmove", name="Rightmove", kind="rightmove")
    await send(
        client, "rightmove", created["key"],
        {"name": "Lin Zhao", "email": "lin@example.com", "property_reference": "RM-99812"},
    )

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert "RM-99812" in lead["notes"]


# ============================================================== the shapes =====


async def test_meta_sends_questions_and_answers_not_fields(client, builder):
    """Meta's payload is a list of whatever the person who built the ad typed, so
    it is flattened before it is read."""
    h = builder["headers"]
    created = await make_source(client, h, code="fb", name="Facebook", kind="meta_lead_ads")
    await send(
        client, "fb", created["key"],
        {
            "leadgen_id": "fb-4471",
            "field_data": [
                {"name": "full_name", "values": ["Nadia Haddad"]},
                {"name": "email", "values": ["nadia@example.com"]},
                {"name": "phone_number", "values": ["07700900222"]},
            ],
            "campaign_name": "Poole launch",
        },
    )

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["full_name"] == "Nadia Haddad"
    assert lead["mobile"] == "07700 900222"
    assert "Poole launch" in lead["notes"]


async def test_google_sends_its_own_third_shape(client, builder):
    h = builder["headers"]
    created = await make_source(client, h, code="gads", name="Google", kind="google_lead_form")
    await send(
        client, "gads", created["key"],
        {
            "lead_id": "g-8812",
            "user_column_data": [
                {"column_id": "FIRST_NAME", "string_value": "Owen"},
                {"column_id": "LAST_NAME", "string_value": "Fitzgerald"},
                {"column_id": "EMAIL", "string_value": "owen@example.com"},
            ],
        },
    )
    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["full_name"] == "Owen Fitzgerald"


async def test_an_email_enquiry_is_read_off_its_body(client, builder):
    """The fallback for portals that only ever send an email."""
    h = builder["headers"]
    created = await make_source(client, h, code="inbox", name="Enquiries inbox", kind="email")
    await send(
        client, "inbox", created["key"],
        {
            "subject": "New enquiry from Zoopla",
            "from": "noreply@zoopla.example.com",
            "body": (
                "You have a new enquiry.\n\n"
                "Name: Grace Adeyemi\n"
                "Email: grace@example.com\n"
                "Telephone: 01202 555 190\n"
                "Message: Please send the brochure for the Maple.\n"
            ),
        },
    )
    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["full_name"] == "Grace Adeyemi"
    assert lead["email"] == "grace@example.com"
    assert lead["phone"] == "01202 555190"


async def test_an_unknown_channel_is_mapped_by_configuration(client, builder):
    """A new integration should need configuration, not a release."""
    h = builder["headers"]
    created = await make_source(
        client, h, code="partner", name="Partner feed", kind="generic",
        field_map={"customer_forename": "first_name", "customer_surname": "last_name",
                   "customer_email": "email"},
    )
    await send(
        client, "partner", created["key"],
        {"customer_forename": "Sam", "customer_surname": "Okafor",
         "customer_email": "sam@example.com"},
    )
    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["full_name"] == "Sam Okafor"


# ======================================================== when it goes wrong =====


async def test_an_unreadable_payload_is_kept_and_flagged(client, builder):
    """The property the whole design turns on. A parser that breaks must cost a
    delay, not a customer."""
    h = builder["headers"]
    created = await make_source(client, h)

    ack = await send(client, "website", created["key"], {"enquirer": "no recognisable name"})
    assert ack["received"] is False
    assert ack["status"] == "failed"

    # No lead - but the payload is on the queue, intact, with the reason.
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 0
    queue = (await client.get("/api/v1/intake/events/failed", headers=h)).json()
    assert queue["total"] == 1
    event = queue["items"][0]
    assert event["payload"] == {"enquirer": "no recognisable name"}
    assert "No name in the payload" in event["error"]
    assert event["needs_attention"] is True


async def test_a_failed_payload_can_be_replayed_after_a_fix(client, builder):
    """Correct the mapping, replay, and the enquiry becomes the lead it should
    have been."""
    h = builder["headers"]
    created = await make_source(
        client, h, code="partner", name="Partner", kind="generic",
        field_map={"wrong_field": "first_name"},
    )
    await send(
        client, "partner", created["key"],
        {"enquirer_name": "Priya Mistry", "enquirer_email": "priya@example.com"},
    )
    event = (await client.get("/api/v1/intake/events/failed", headers=h)).json()["items"][0]

    # Fix the map, then replay the payload we kept.
    await client.patch(
        f"/api/v1/intake/sources/{created['source']['id']}", headers=h,
        json={"field_map": {"enquirer_name": "name", "enquirer_email": "email"}},
    )
    result = await post(
        client, h, f"/api/v1/intake/events/{event['id']}/replay", None, expect=200
    )
    assert result["status"] == "replayed"
    assert result["lead_id"] is not None

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["full_name"] == "Priya Mistry"


async def test_a_replay_that_still_fails_says_so_and_keeps_the_payload(client, builder):
    h = builder["headers"]
    created = await make_source(client, h)
    await send(client, "website", created["key"], {"nothing": "useful"})
    event = (await client.get("/api/v1/intake/events/failed", headers=h)).json()["items"][0]

    resp = await client.post(f"/api/v1/intake/events/{event['id']}/replay", headers=h)
    assert resp.status_code == 409
    assert "Still cannot read" in resp.json()["detail"]
    # Still on the queue, not silently consumed by the attempt.
    assert (await client.get("/api/v1/intake/events/failed", headers=h)).json()["total"] == 1


async def test_a_bot_is_rejected_rather_than_captured(client, builder):
    """A field a human never sees. Anything that fills it in is a script."""
    h = builder["headers"]
    created = await make_source(client, h, honeypot_field="website_url")

    ack = await send(
        client, "website", created["key"],
        {"name": "Cheap Pills", "email": "spam@example.com", "website_url": "http://spam"},
    )
    assert ack["status"] == "rejected"
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 0
    # Rejected, not failed: we looked and it is rubbish. Conflating the two is how
    # a broken mapping gets mistaken for bot traffic and ignored.
    assert (await client.get("/api/v1/intake/events/failed", headers=h)).json()["total"] == 0


async def test_a_flood_from_one_address_is_throttled(client, builder):
    h = builder["headers"]
    created = await make_source(client, h, rate_limit_per_minute=3)

    outcomes = [
        (await send(client, "website", created["key"], {"name": f"Bot {i}"}))["status"]
        for i in range(5)
    ]
    assert outcomes[:3] == ["accepted"] * 3
    assert outcomes[3:] == ["rejected"] * 2
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 3


async def test_a_paused_channel_accepts_nothing(client, builder):
    """What you reach for when a form starts taking bot traffic."""
    h = builder["headers"]
    created = await make_source(client, h)
    await post(
        client, h, f"/api/v1/intake/sources/{created['source']['id']}/pause", None, expect=200
    )

    ack = await send(client, "website", created["key"], {"name": "Jo Nkemdirim"})
    assert ack["received"] is False
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 0

    await post(
        client, h, f"/api/v1/intake/sources/{created['source']['id']}/resume", None, expect=200
    )
    assert (await send(client, "website", created["key"], {"name": "Jo Nkemdirim"}))[
        "status"
    ] == "accepted"


# ============================================================== duplicates =====


async def test_a_resend_does_not_produce_a_second_lead(client, builder):
    """Portals retry. The endpoint has to be idempotent because the network is
    not."""
    h = builder["headers"]
    created = await make_source(client, h, code="rightmove", name="Rightmove", kind="rightmove")
    body = {"enquiryId": "RM-1234", "name": "Lin Zhao", "email": "lin@example.com"}

    first = await send(client, "rightmove", created["key"], body)
    second = await send(client, "rightmove", created["key"], body)

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 1


async def test_a_repeat_enquiry_is_linked_to_the_person_we_already_have(client, builder):
    """On the capture form a human is warned and decides. A webhook has nobody to
    ask, so it attaches rather than duplicating."""
    h = builder["headers"]
    existing = await post(
        client, h, "/api/v1/leads",
        {"first_name": "Dana", "last_name": "Reyes", "email": "dana@example.com",
         "phone": "01202 555 101"},
    )
    created = await make_source(client, h)

    ack = await send(
        client, "website", created["key"],
        {"name": "Dana Reyes", "telephone": "01202555101",
         "message": "Still interested - any news on plot 12?"},
    )
    assert ack["status"] == "duplicate"
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 1

    # The repeat is the strongest buying signal a lead gives, so it goes on the
    # trail rather than being discarded as noise.
    trail = (await client.get(f"/api/v1/leads/{existing['id']}/activities", headers=h)).json()
    assert any("Enquired again" in a["summary"] for a in trail)
    assert any("plot 12" in a["summary"] for a in trail)


async def test_a_repeat_enquiry_does_not_pretend_we_made_contact(client, builder):
    """They contacted us. The chasing queue measures whether *we* called *them*,
    and advancing it here would hide a lead nobody has rung."""
    h = builder["headers"]
    existing = await post(
        client, h, "/api/v1/leads",
        {"first_name": "Dana", "last_name": "Reyes", "email": "dana@example.com"},
    )
    created = await make_source(client, h)
    await send(
        client, "website", created["key"],
        {"name": "Dana Reyes", "email": "dana@example.com"},
    )

    after = (await client.get(f"/api/v1/leads/{existing['id']}", headers=h)).json()
    assert after["last_contacted_on"] is None


# ============================================================= round-robin =====


async def test_leads_are_shared_out_in_turn(client, builder):
    """Nothing should pile up unowned, and no single salesperson should take the
    lot."""
    h = builder["headers"]
    people = []
    for i in range(3):
        await post(
            client, h, "/api/v1/users",
            {"email": f"sales{i}@cascade.example.com", "full_name": f"Seller {i}",
             "password": "supersecret123", "role": "sales"},
        )
    listed = (await client.get("/api/v1/users", headers=h)).json()["items"]
    people = sorted(u["id"] for u in listed if u["role"] == "sales")
    assert len(people) == 3

    created = await make_source(client, h)
    for i in range(6):
        await send(
            client, "website", created["key"],
            {"name": f"Buyer {i}", "email": f"b{i}@e.com"},
        )

    leads = (await client.get("/api/v1/leads", headers=h)).json()["items"]
    owners = [row["assigned_to_id"] for row in leads]
    assert all(o is not None for o in owners)
    # Six leads, three salespeople, two each. Even distribution is the point.
    assert sorted({o: owners.count(o) for o in owners}.values()) == [2, 2, 2]


async def test_a_builder_with_no_sales_staff_still_gets_the_lead(client, builder):
    """A one-person builder should not lose enquiries because they never made a
    separate sales account."""
    h = builder["headers"]
    me = (await client.get("/api/v1/users/me", headers=h)).json()
    created = await make_source(client, h)
    await send(client, "website", created["key"], {"name": "Jo Nkemdirim"})

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert lead["assigned_to_id"] == me["id"]     # fell back to the owner


async def test_a_channel_can_be_told_not_to_assign(client, builder):
    """Some builders want a manager to distribute the morning's enquiries."""
    h = builder["headers"]
    created = await make_source(client, h, auto_assign=False)
    await send(client, "website", created["key"], {"name": "Jo Nkemdirim"})

    queue = (await client.get("/api/v1/leads/unassigned", headers=h)).json()
    assert queue["total"] == 1


# ================================================================ security =====


async def test_a_wrong_key_is_refused(client, builder):
    await make_source(client, builder["headers"])
    resp = await client.post(
        "/api/v1/intake/hooks/website",
        headers={"X-Intake-Key": "cpmt_ik_not-the-right-key"},
        json={"name": "Jo Nkemdirim"},
    )
    assert resp.status_code == 404


async def test_a_missing_key_is_refused(client, builder):
    await make_source(client, builder["headers"])
    resp = await client.post("/api/v1/intake/hooks/website", json={"name": "Jo Nkemdirim"})
    assert resp.status_code == 404


async def test_one_channels_key_does_not_work_on_another(client, builder):
    """Per-channel keys exist so a leak is contained to one channel."""
    h = builder["headers"]
    web = await make_source(client, h)
    await make_source(client, h, code="rightmove", name="Rightmove", kind="rightmove")

    resp = await client.post(
        "/api/v1/intake/hooks/rightmove",
        headers={"X-Intake-Key": web["key"]},
        json={"name": "Lin Zhao"},
    )
    assert resp.status_code == 404


async def test_a_key_cannot_reach_another_builder(client, builder):
    """The public endpoint resolves the tenant from the key. That is the one
    unscoped query in the application, so it is worth asserting directly."""
    h = builder["headers"]
    ours = await make_source(client, h)
    other = await signup(client, "Rival Homes", "rival-intake@rival.example.com")
    await make_source(client, other, code="website", name="Their form")

    # Our key on our own code creates in our tenant, and theirs stays empty.
    await send(client, "website", ours["key"], {"name": "Ama Boateng", "email": "a@e.com"})
    assert (await client.get("/api/v1/leads", headers=h)).json()["total"] == 1
    assert (await client.get("/api/v1/leads", headers=other)).json()["total"] == 0


async def test_rotating_a_key_stops_the_old_one_immediately(client, builder):
    """No grace period. A key is rotated because it leaked, and a window in which
    the leaked key still works is the thing being closed."""
    h = builder["headers"]
    created = await make_source(client, h)
    rotated = await post(
        client, h, f"/api/v1/intake/sources/{created['source']['id']}/rotate-key",
        None, expect=200,
    )
    assert rotated["key"] != created["key"]

    dead = await client.post(
        "/api/v1/intake/hooks/website",
        headers={"X-Intake-Key": created["key"]},
        json={"name": "Jo Nkemdirim"},
    )
    assert dead.status_code == 404
    assert (await send(client, "website", rotated["key"], {"name": "Jo Nkemdirim"}))[
        "status"
    ] == "accepted"


async def test_the_key_is_never_stored_in_the_lead(client, builder):
    """Platforms that cannot set headers put the key in the body. It must not end
    up in the notes."""
    h = builder["headers"]
    created = await make_source(client, h)
    resp = await client.post(
        "/api/v1/intake/hooks/website",
        json={"key": created["key"], "name": "Jo Nkemdirim", "message": "Hello"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    lead = (await client.get("/api/v1/leads", headers=h)).json()["items"][0]
    assert created["key"] not in (lead["notes"] or "")
    event = (await client.get("/api/v1/intake/events", headers=h)).json()["items"][0]
    assert "key" not in event["payload"]


async def test_events_do_not_leak_between_builders(client, builder):
    h = builder["headers"]
    created = await make_source(client, h)
    await send(client, "website", created["key"], {"name": "Ama Boateng"})
    other = await signup(client, "Rival Two", "rival-events@rival.example.com")

    assert (await client.get("/api/v1/intake/events", headers=other)).json()["total"] == 0
    assert (await client.get("/api/v1/intake/sources", headers=other)).json() == []


async def test_an_event_cannot_be_replayed_across_tenants(client, builder):
    h = builder["headers"]
    created = await make_source(client, h)
    await send(client, "website", created["key"], {"nothing": "readable"})
    event = (await client.get("/api/v1/intake/events/failed", headers=h)).json()["items"][0]
    other = await signup(client, "Rival Three", "rival-replay@rival.example.com")

    resp = await client.post(f"/api/v1/intake/events/{event['id']}/replay", headers=other)
    assert resp.status_code == 404


# ================================================================== health =====


async def test_health_shows_whether_each_channel_is_still_delivering(client, builder):
    """A channel that silently stops is the failure nobody notices: no error, just
    fewer leads than last month."""
    h = builder["headers"]
    created = await make_source(client, h)
    quiet = await make_source(client, h, code="rightmove", name="Rightmove", kind="rightmove")

    await send(client, "website", created["key"], {"name": "Ama Boateng"})
    await send(client, "website", created["key"], {"unreadable": True})

    health = (await client.get("/api/v1/intake/health", headers=h)).json()
    by_code = {s["code"]: s for s in health["sources"]}

    assert by_code["website"]["received_count"] == 2
    assert by_code["website"]["accepted_count"] == 1
    assert by_code["website"]["failed_count"] == 1
    assert by_code["website"]["last_received_at"] is not None

    assert by_code["rightmove"]["received_count"] == 0
    assert by_code["rightmove"]["last_received_at"] is None   # never heard from
    assert quiet["source"]["code"] == "rightmove"

    assert health["needs_attention"] == 1
    assert health["events_by_status"]["accepted"] == 1
    assert health["events_by_status"]["failed"] == 1


async def test_every_arrival_is_recorded_whatever_became_of_it(client, builder):
    """The log is the evidence that nothing was dropped, so it has to hold the
    rejected and the unreadable too."""
    h = builder["headers"]
    created = await make_source(client, h, honeypot_field="trap")

    await send(client, "website", created["key"], {"name": "Ama Boateng"})
    await send(client, "website", created["key"], {"unreadable": True})
    await send(client, "website", created["key"], {"name": "Bot", "trap": "x"})

    events = (await client.get("/api/v1/intake/events", headers=h)).json()
    assert events["total"] == 3
    assert {e["status"] for e in events["items"]} == {"accepted", "failed", "rejected"}


async def test_the_events_list_can_be_narrowed_to_one_channel(client, builder):
    h = builder["headers"]
    web = await make_source(client, h)
    rm = await make_source(client, h, code="rightmove", name="Rightmove", kind="rightmove")
    await send(client, "website", web["key"], {"name": "Ama Boateng", "email": "a@e.com"})
    await send(client, "rightmove", rm["key"], {"name": "Lin Zhao", "email": "l@e.com"})

    only = (
        await client.get(
            f"/api/v1/intake/events?source_id={rm['source']['id']}", headers=h
        )
    ).json()
    assert only["total"] == 1
    assert only["items"][0]["source_id"] == rm["source"]["id"]


async def test_archiving_a_channel_keeps_its_history(client, builder):
    """The events are the record of every lead it ever produced."""
    h = builder["headers"]
    created = await make_source(client, h)
    await send(client, "website", created["key"], {"name": "Ama Boateng"})

    await post(
        client, h, f"/api/v1/intake/sources/{created['source']['id']}/archive",
        {"reason": "Website rebuilt"}, expect=200,
    )
    assert (await client.get("/api/v1/intake/sources", headers=h)).json() == []
    assert (await client.get("/api/v1/intake/events", headers=h)).json()["total"] == 1
    assert uuid.UUID(created["source"]["id"])
