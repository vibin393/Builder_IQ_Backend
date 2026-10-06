"""Scoping the dashboard to one community.

A builder running four subdivisions does not want one blended number: Maple
Ridge being 12% over does not show up in an average that Oakfield is carrying.

The property worth pinning down is that scoping actually narrows *every*
figure, not just the obvious ones - a half-scoped dashboard is worse than an
unscoped one, because it looks authoritative and is wrong.
"""

import uuid
from decimal import Decimal

from tests.conftest import post


async def _second_community(client, h):
    """A community with its own lot, so a job can be built somewhere else.

    The code is unique per call: the test database is shared across tests in a
    run, so a fixed code collides on the second test that needs one.
    """
    code = f"OAK{uuid.uuid4().hex[:6].upper()}"
    community = await post(
        client, h, "/api/v1/communities",
        {"code": code, "name": "Oakfield", "city": "Dorset"},
    )
    lot = await post(
        client, h, f"/api/v1/communities/{community['id']}/lots",
        {"lot_number": "OAK-1", "premium": "5000.00"},
    )
    return community, lot


async def test_unscoped_dashboard_covers_every_community(client, sold_job):
    h = sold_job["headers"]
    d = (await client.get("/api/v1/reports/dashboard", headers=h)).json()

    assert d["scope"]["community_id"] is None
    assert d["scope"]["unscoped"] == []
    assert d["construction"]["jobs_total"] >= 1


async def test_scoping_to_a_community_narrows_the_homes(client, sold_job):
    h = sold_job["headers"]
    home_community = sold_job["community"]["id"]

    other, lot = await _second_community(client, h)
    await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": other["id"],
            "lot_id": lot["id"],
            "plan_id": sold_job["plan"]["id"],
        },
    )

    whole = (await client.get("/api/v1/reports/dashboard", headers=h)).json()
    assert whole["construction"]["jobs_total"] == 2

    scoped = (
        await client.get(
            f"/api/v1/reports/dashboard?community_id={home_community}", headers=h
        )
    ).json()
    assert scoped["construction"]["jobs_total"] == 1
    assert scoped["scope"]["community_id"] == home_community
    assert scoped["scope"]["jobs_in_scope"] == 1


async def test_money_is_scoped_too(client, sold_job):
    """The figure a builder actually looks at. Half-scoping this is the bug."""
    h = sold_job["headers"]
    other_community = (await _second_community(client, h))[0]["id"]

    whole = (await client.get("/api/v1/reports/dashboard", headers=h)).json()
    # The fixture budgets a home but leaves its contract unsigned, so budget is
    # the figure with something in it to prove scoping against.
    assert Decimal(whole["money"]["budget_revised"]) > 0

    # The empty community has no jobs, so every money figure must be zero -
    # not the portfolio total leaking through an unfiltered aggregate.
    empty = (
        await client.get(
            f"/api/v1/reports/dashboard?community_id={other_community}", headers=h
        )
    ).json()
    assert Decimal(empty["money"]["budget_revised"]) == 0
    assert Decimal(empty["money"]["committed"]) == 0
    assert Decimal(empty["money"]["actual"]) == 0
    assert Decimal(empty["money"]["retainage_held"]) == 0
    assert empty["construction"]["jobs_total"] == 0


async def test_project_controls_are_scoped_too(client, sold_job):
    h = sold_job["headers"]
    other_community = (await _second_community(client, h))[0]["id"]

    await post(
        client, h, "/api/v1/rfis",
        {
            "job_id": sold_job["job"]["id"],
            "subject": "Beam bearing",
            "question": "Which governs?",
            "is_blocking": True,
        },
    )

    whole = (await client.get("/api/v1/reports/dashboard", headers=h)).json()
    assert whole["controls"]["rfis_open"] == 1

    empty = (
        await client.get(
            f"/api/v1/reports/dashboard?community_id={other_community}", headers=h
        )
    ).json()
    assert empty["controls"]["rfis_open"] == 0
    assert empty["controls"]["rfis_blocking"] == 0


async def test_leads_stay_portfolio_wide_and_say_so(client, sold_job):
    """A lead arrives before anyone has picked a lot, so it belongs to no
    community. Pretending otherwise would silently drop them from every view."""
    h = sold_job["headers"]
    community = sold_job["community"]["id"]

    await post(
        client, h, "/api/v1/leads",
        {"first_name": "Sam", "last_name": "Hale", "source": "walk_in"},
    )

    scoped = (
        await client.get(f"/api/v1/reports/dashboard?community_id={community}", headers=h)
    ).json()
    assert scoped["sales"]["open_leads"] >= 1
    assert "open_leads" in scoped["scope"]["unscoped"]


async def test_another_tenants_community_is_not_found(client, sold_job):
    signup = await client.post(
        "/api/v1/auth/register",
        json={
            "company_name": "Rival Homes",
            "full_name": "Owner",
            "email": "rival2@rival.example.com",
            "password": "supersecret123",
        },
    )
    other = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    resp = await client.get(
        f"/api/v1/reports/dashboard?community_id={sold_job['community']['id']}",
        headers=other,
    )
    # 404 rather than 403: a 403 would confirm the community exists.
    assert resp.status_code == 404
