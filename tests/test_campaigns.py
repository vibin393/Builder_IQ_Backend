"""Campaigns: what marketing spent, and whether it came back.

The old screen reported opens and clicks, which is not the question a homebuilder
asks about marketing. Three campaigns with identical open rates can differ by an
order of magnitude on cost per sale, and only one of them is worth running again.

So what is asserted here is mostly about money and attribution:

  * cost per lead and cost per sale, and that they are **null rather than zero**
    when nothing has been produced yet - zero is a lie a report would repeat
  * revenue traced lead → buyer → contract, so a campaign is judged against what
    it earned rather than only against what it cost
  * attribution by foreign key, because the old string match meant renaming a
    campaign code silently detached every lead that came from it
  * no open rate on a road sign, because 0% reads as failure rather than as a
    question that does not apply
"""

from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import post, signup

TODAY = date.today()
NEXT_WEEK = TODAY + timedelta(days=7)
LAST_WEEK = TODAY - timedelta(days=7)


# --- helpers ------------------------------------------------------------------


async def make_campaign(client, headers, code="spring-parade", **overrides):
    payload = {
        "code": code,
        "name": "Spring Parade of Homes",
        "channel": "email",
        "subject": "Tour our new models this weekend",
        "budget": "5000.00",
    }
    payload.update(overrides)
    return await post(client, headers, "/api/v1/campaigns", payload)


async def lead_from(client, headers, campaign_code, email, convert=False):
    """A lead tagged with a campaign, optionally taken all the way to a buyer."""
    lead = await post(
        client, headers, "/api/v1/leads",
        {
            "first_name": "Ama",
            "last_name": email.split("@")[0].title(),
            "email": email,
            "source": "campaign",
            "campaign": campaign_code,
        },
    )
    if convert:
        for step in ("contacted", "qualified"):
            await post(
                client, headers, f"/api/v1/leads/{lead['id']}/status", {"status": step}, expect=200
            )
        lead = await post(
            client, headers, f"/api/v1/leads/{lead['id']}/convert", None, expect=200
        )
    return lead


async def launch(client, headers, campaign_id):
    return await post(client, headers, f"/api/v1/campaigns/{campaign_id}/launch", {}, expect=200)


# ============================================================ the lifecycle =====


async def test_a_campaign_runs_from_draft_to_finished(client, builder):
    h = builder["headers"]
    c = await make_campaign(client, h)
    assert c["status"] == "draft"

    scheduled = await post(
        client, h, f"/api/v1/campaigns/{c['id']}/schedule",
        {"scheduled_for": str(NEXT_WEEK)}, expect=200,
    )
    assert scheduled["status"] == "scheduled"

    running = await launch(client, h, c["id"])
    assert running["status"] == "running"
    assert running["starts_on"] == str(TODAY)
    assert running["is_active"] is True

    done = await post(client, h, f"/api/v1/campaigns/{c['id']}/complete", {}, expect=200)
    assert done["status"] == "sent"


async def test_an_email_with_no_subject_is_not_sent(client, builder):
    """One arriving with an empty subject line is worse than one that does not
    arrive."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="no-subject", subject=None)

    resp = await client.post(f"/api/v1/campaigns/{c['id']}/launch", headers=h, json={})
    assert resp.status_code == 409
    assert "no subject line" in resp.json()["detail"]


async def test_a_road_sign_needs_no_subject(client, builder):
    """The subject rule belongs to email, not to campaigns."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="a31-boards", channel="signage", subject=None)
    running = await launch(client, h, c["id"])
    assert running["status"] == "running"


async def test_scheduling_into_the_past_is_refused(client, builder):
    h = builder["headers"]
    c = await make_campaign(client, h, code="backwards")

    resp = await client.post(
        f"/api/v1/campaigns/{c['id']}/schedule", headers=h, json={"scheduled_for": str(LAST_WEEK)}
    )
    assert resp.status_code == 409
    assert "has passed" in resp.json()["detail"]


async def test_a_campaign_that_ends_before_it_starts_is_refused(client, builder):
    h = builder["headers"]
    resp = await client.post(
        "/api/v1/campaigns",
        headers=h,
        json={
            "code": "impossible",
            "name": "Backwards",
            "starts_on": str(NEXT_WEEK),
            "ends_on": str(TODAY),
        },
    )
    assert resp.status_code == 409
    assert "ends before it starts" in resp.json()["detail"]


async def test_a_finished_campaign_cannot_be_edited(client, builder):
    """Its results were produced by what it actually said and cost. Editing that
    afterwards turns the report into fiction."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="finished")
    await launch(client, h, c["id"])
    await post(client, h, f"/api/v1/campaigns/{c['id']}/complete", {}, expect=200)

    resp = await client.patch(
        f"/api/v1/campaigns/{c['id']}", headers=h, json={"name": "Rewritten"}
    )
    assert resp.status_code == 409
    assert "Duplicate it instead" in resp.json()["detail"]


async def test_cancelling_needs_a_reason(client, builder):
    """Money was committed against it and somebody will ask what happened."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="doomed")

    resp = await client.post(
        f"/api/v1/campaigns/{c['id']}/cancel", headers=h, json={"reason": ""}
    )
    assert resp.status_code == 422

    cancelled = await post(
        client, h, f"/api/v1/campaigns/{c['id']}/cancel",
        {"reason": "Budget pulled"}, expect=200,
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancellation_reason"] == "Budget pulled"


async def test_a_copy_keeps_the_content_and_drops_the_results(client, builder):
    """Most campaigns are last year's with the dates changed. Carrying the old
    numbers across would be the most misleading possible default."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="parade-2025")
    await launch(client, h, c["id"])
    await post(
        client, h, f"/api/v1/campaigns/{c['id']}/delivery",
        {"sent_count": 4000, "opened_count": 1200}, expect=200,
    )
    await post(client, h, f"/api/v1/campaigns/{c['id']}/cost", {"cost": "4200.00"}, expect=200)

    copy = await post(
        client, h, f"/api/v1/campaigns/{c['id']}/duplicate", {"code": "parade-2026"}
    )
    assert copy["status"] == "draft"
    assert copy["subject"] == "Tour our new models this weekend"
    assert Decimal(copy["budget"]) == Decimal("5000.00")
    # The results belong to the campaign that produced them.
    assert copy["sent_count"] == 0
    assert Decimal(copy["cost"]) == Decimal("0")


# ============================================================= delivery =====


async def test_delivery_numbers_are_checked_against_each_other(client, builder):
    """A wrong open rate on a report nobody questions is worse than an error now."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="checked")
    await launch(client, h, c["id"])

    too_many_opens = await client.post(
        f"/api/v1/campaigns/{c['id']}/delivery",
        headers=h,
        json={"sent_count": 100, "opened_count": 150},
    )
    assert too_many_opens.status_code == 409
    assert "More opens" in too_many_opens.json()["detail"]

    too_many_clicks = await client.post(
        f"/api/v1/campaigns/{c['id']}/delivery",
        headers=h,
        json={"sent_count": 100, "opened_count": 40, "clicked_count": 60},
    )
    assert too_many_clicks.status_code == 409
    assert "before they can click" in too_many_clicks.json()["detail"]


async def test_a_road_sign_has_no_delivery_numbers(client, builder):
    """Showing 0% against a hoarding reads as failure rather than as a question
    that does not apply."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="hoardings", channel="signage", subject=None)

    resp = await client.post(
        f"/api/v1/campaigns/{c['id']}/delivery", headers=h, json={"sent_count": 100}
    )
    assert resp.status_code == 409
    assert "belong to email" in resp.json()["detail"]
    assert c["has_delivery_numbers"] is False


async def test_delivery_produces_the_rates(client, builder):
    h = builder["headers"]
    c = await make_campaign(client, h, code="rates")
    await launch(client, h, c["id"])

    updated = await post(
        client, h, f"/api/v1/campaigns/{c['id']}/delivery",
        {"sent_count": 1000, "opened_count": 250, "clicked_count": 50}, expect=200,
    )
    assert updated["open_rate"] == 0.25
    assert updated["click_rate"] == 0.05


# =========================================================== attribution =====


async def test_a_lead_links_to_its_campaign_the_moment_it_is_captured(client, builder):
    """Attribution that needs somebody to press a button is only as good as who
    remembers, which is how a marketing report quietly understates everything."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="at-capture")
    await lead_from(client, h, "at-capture", "now@example.com")

    report = (await client.get("/api/v1/campaigns/performance", headers=h)).json()
    row = next(r for r in report["campaigns"] if r["campaign_id"] == c["id"])
    assert row["leads"] == 1        # nothing was pressed


async def test_attaching_catches_leads_that_predate_the_campaign(client, builder):
    """What the button is actually for: tags that arrived before anybody created
    the campaign they name."""
    h = builder["headers"]
    # Tagged with a campaign that does not exist yet, so nothing to link to.
    await lead_from(client, h, "late-created", "one@example.com")
    await lead_from(client, h, "LATE-CREATED", "two@example.com")   # case is ignored
    await lead_from(client, h, "something-else", "three@example.com")

    c = await make_campaign(client, h, code="late-created")
    result = await post(
        client, h, f"/api/v1/campaigns/{c['id']}/attach-leads", None, expect=200
    )
    assert result["attached"] == 2

    # And running it again finds nothing, because they are already linked.
    again = await post(
        client, h, f"/api/v1/campaigns/{c['id']}/attach-leads", None, expect=200
    )
    assert again["attached"] == 0


async def test_renaming_a_campaign_no_longer_detaches_its_leads(client, builder):
    """The whole reason attribution moved to a foreign key."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="old-code")
    await lead_from(client, h, "old-code", "keeper@example.com")
    await post(client, h, f"/api/v1/campaigns/{c['id']}/attach-leads", None, expect=200)

    await client.patch(f"/api/v1/campaigns/{c['id']}", headers=h, json={"code": "new-code"})

    report = (await client.get("/api/v1/campaigns/performance", headers=h)).json()
    row = next(r for r in report["campaigns"] if r["campaign_id"] == c["id"])
    assert row["code"] == "new-code"
    assert row["leads"] == 1        # still attributed


async def test_a_tag_matching_nothing_is_counted_rather_than_ignored(client, builder):
    """Money spent somewhere nobody can name is worth surfacing."""
    h = builder["headers"]
    await lead_from(client, h, "a-campaign-that-does-not-exist", "orphan@example.com")

    summary = (await client.get("/api/v1/campaigns/summary", headers=h)).json()
    assert summary["unmatched_tags"] == 1


# ================================================================= money =====


async def test_cost_per_lead_and_per_sale(client, builder):
    """The two numbers that decide whether to run it again."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="money")
    await launch(client, h, c["id"])
    await post(client, h, f"/api/v1/campaigns/{c['id']}/cost", {"cost": "4000.00"}, expect=200)

    for i in range(4):
        await lead_from(client, h, "money", f"m{i}@example.com", convert=(i == 0))
    await post(client, h, f"/api/v1/campaigns/{c['id']}/attach-leads", None, expect=200)

    report = (await client.get("/api/v1/campaigns/performance", headers=h)).json()
    row = next(r for r in report["campaigns"] if r["campaign_id"] == c["id"])
    assert row["leads"] == 4
    assert row["converted"] == 1
    assert Decimal(row["cost_per_lead"]) == Decimal("1000.00")
    assert Decimal(row["cost_per_sale"]) == Decimal("4000.00")
    assert row["conversion_rate"] == 25.0


async def test_cost_per_lead_says_nothing_rather_than_zero(client, builder):
    """Zero would be a lie a report repeats, and infinity is not actionable."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="quiet")
    await post(client, h, f"/api/v1/campaigns/{c['id']}/cost", {"cost": "2000.00"}, expect=200)

    report = (await client.get("/api/v1/campaigns/performance", headers=h)).json()
    row = next(r for r in report["campaigns"] if r["campaign_id"] == c["id"])
    assert row["leads"] == 0
    assert row["cost_per_lead"] is None
    assert row["cost_per_sale"] is None
    assert row["conversion_rate"] is None


async def test_revenue_is_traced_from_the_lead_to_the_contract(client, builder):
    """A campaign is judged against what it earned, not only what it cost. That
    chain is the whole reason a lead record survives conversion."""
    h = builder["headers"]
    c = await make_campaign(client, h, code="earner")
    await post(client, h, f"/api/v1/campaigns/{c['id']}/cost", {"cost": "4000.00"}, expect=200)

    lead = await lead_from(client, h, "earner", "buyer@example.com", convert=True)
    await post(client, h, f"/api/v1/campaigns/{c['id']}/attach-leads", None, expect=200)

    # The buyer that lead became signs for a house.
    lot = await post(
        client, h, f"/api/v1/communities/{builder['community']['id']}/lots",
        {"lot_number": "61"},
    )
    job = await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": builder["community"]["id"],
            "lot_id": lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )
    # Naming the buyer the lead became, rather than creating a second record for
    # the same person. This is what keeps the chain joinable.
    contract = await post(
        client, h, f"/api/v1/jobs/{job['id']}/contract",
        {
            "buyer_id": lead["converted_buyer_id"],
            "base_price": "420000.00",
            "lot_premium": "15000.00",
        },
    )
    assert contract["buyer_id"] == lead["converted_buyer_id"]

    report = (await client.get("/api/v1/campaigns/performance", headers=h)).json()
    row = next(r for r in report["campaigns"] if r["campaign_id"] == c["id"])
    assert row["converted"] == 1
    assert Decimal(row["revenue"]) == Decimal("435000.00")     # base + lot premium
    # 435,000 of contracts from 4,000 of spend.
    assert row["return_on_spend"] == 108.75


async def test_selling_to_a_lead_does_not_create_a_second_buyer(client, builder):
    """Converting a lead creates a Buyer. Selling the home used to create another
    for the same person, which left orphan buyers behind and broke the chain
    campaign revenue is traced along."""
    h = builder["headers"]
    lead = await lead_from(client, h, "reuse", "reuse@example.com", convert=True)
    lot = await post(
        client, h, f"/api/v1/communities/{builder['community']['id']}/lots",
        {"lot_number": "62"},
    )
    job = await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": builder["community"]["id"],
            "lot_id": lot["id"],
            "plan_id": builder["plan"]["id"],
        },
    )

    contract = await post(
        client, h, f"/api/v1/jobs/{job['id']}/contract",
        {"buyer_id": lead["converted_buyer_id"], "base_price": "420000.00"},
    )
    assert contract["buyer_id"] == lead["converted_buyer_id"]


async def test_a_contract_needs_a_buyer_one_way_or_the_other(client, builder):
    h = builder["headers"]
    lot = await post(
        client, h, f"/api/v1/communities/{builder['community']['id']}/lots",
        {"lot_number": "63"},
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
        f"/api/v1/jobs/{job['id']}/contract", headers=h, json={"base_price": "420000.00"}
    )
    assert resp.status_code == 422


async def test_over_budget_is_flagged(client, builder):
    h = builder["headers"]
    c = await make_campaign(client, h, code="overspent", budget="1000.00")
    over = await post(
        client, h, f"/api/v1/campaigns/{c['id']}/cost", {"cost": "1500.00"}, expect=200
    )
    assert over["is_over_budget"] is True


async def test_a_negative_cost_is_refused(client, builder):
    h = builder["headers"]
    c = await make_campaign(client, h, code="negative")
    resp = await client.post(
        f"/api/v1/campaigns/{c['id']}/cost", headers=h, json={"cost": "-50.00"}
    )
    assert resp.status_code == 422


# =============================================== ordering and isolation =====


async def test_the_report_leads_with_the_cheapest_sale_not_the_most_leads(client, builder):
    """The campaign that produced the most enquiries is very often not the one
    worth repeating, and a list sorted by volume quietly argues that it is."""
    h = builder["headers"]

    cheap = await make_campaign(client, h, code="cheap")
    await post(client, h, f"/api/v1/campaigns/{cheap['id']}/cost", {"cost": "1000.00"}, 200)
    await lead_from(client, h, "cheap", "cheap1@example.com", convert=True)
    await post(client, h, f"/api/v1/campaigns/{cheap['id']}/attach-leads", None, expect=200)

    noisy = await make_campaign(client, h, code="noisy")
    await post(client, h, f"/api/v1/campaigns/{noisy['id']}/cost", {"cost": "9000.00"}, 200)
    for i in range(5):
        await lead_from(client, h, "noisy", f"n{i}@example.com", convert=(i == 0))
    await post(client, h, f"/api/v1/campaigns/{noisy['id']}/attach-leads", None, expect=200)

    report = (await client.get("/api/v1/campaigns/performance", headers=h)).json()
    codes = [r["code"] for r in report["campaigns"] if r["code"] in ("cheap", "noisy")]
    assert codes == ["cheap", "noisy"]     # cheapest per sale first, despite fewer leads

    assert report["total_leads"] == 6
    assert Decimal(report["total_cost"]) == Decimal("10000.00")


async def test_the_summary_totals_the_spend(client, builder):
    h = builder["headers"]
    one = await make_campaign(client, h, code="s1")
    two = await make_campaign(client, h, code="s2", channel="print", subject=None)
    await post(client, h, f"/api/v1/campaigns/{one['id']}/cost", {"cost": "2000.00"}, 200)
    await post(client, h, f"/api/v1/campaigns/{two['id']}/cost", {"cost": "500.00"}, 200)
    await launch(client, h, one["id"])

    summary = (await client.get("/api/v1/campaigns/summary", headers=h)).json()
    assert Decimal(summary["total_spend"]) == Decimal("2500.00")
    assert summary["active"] == 1
    assert summary["by_status"]["draft"] == 1


async def test_campaigns_can_be_filtered_by_channel(client, builder):
    h = builder["headers"]
    await make_campaign(client, h, code="e1")
    sign = await make_campaign(client, h, code="sg1", channel="signage", subject=None)

    only = (await client.get("/api/v1/campaigns?channel=signage", headers=h)).json()
    assert [c["id"] for c in only["items"]] == [sign["id"]]


async def test_two_campaigns_cannot_share_a_code(client, builder):
    h = builder["headers"]
    await make_campaign(client, h, code="unique")
    resp = await client.post(
        "/api/v1/campaigns", headers=h, json={"code": "UNIQUE", "name": "Clash"}
    )
    assert resp.status_code == 409


async def test_campaigns_do_not_leak_between_builders(client, builder):
    await make_campaign(client, builder["headers"], code="ours")
    other = await signup(client, "Rival Marketing", "rival-mkt@rival.example.com")

    assert (await client.get("/api/v1/campaigns", headers=other)).json()["total"] == 0
    report = (await client.get("/api/v1/campaigns/performance", headers=other)).json()
    assert report["campaigns"] == []
    assert Decimal(report["total_cost"]) == Decimal("0")
