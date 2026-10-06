"""Trade portal: a subcontractor sees their own work and nothing else.

The security property under test is narrow and important: a trade must never see the
contract price or the job budget, and must never see another vendor's purchase orders.
"""

from tests.conftest import PASSWORD, post

TRADE_EMAIL = "office@sparks.example.com"


async def _trade_login(client, builder) -> dict[str, str]:
    """Create a vendor with a contact email, a matching trade user, and log in as them."""
    h = builder["headers"]

    vendor = await post(
        client, h, "/api/v1/vendors",
        {"code": "SPARK2", "name": "Sparks Electrical Office", "email": TRADE_EMAIL},
    )
    await post(
        client, h, "/api/v1/users",
        {
            "email": TRADE_EMAIL,
            "full_name": "Sparks Office",
            "password": PASSWORD,
            "role": "trade",
        },
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": TRADE_EMAIL, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return vendor, {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_portal_identity_counts_outstanding_work(client, sold_job):
    vendor, th = await _trade_login(client, sold_job)
    h = sold_job["headers"]

    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": vendor["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "9000.00"}],
        },
    )
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)

    me = (await client.get("/api/v1/portal/me", headers=th)).json()
    assert me["vendor_code"] == "SPARK2"
    assert me["open_purchase_orders"] == 1


async def test_trade_sees_only_issued_pos_for_their_own_vendor(client, sold_job):
    vendor, th = await _trade_login(client, sold_job)
    h = sold_job["headers"]

    # One PO for this trade, issued.
    mine = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": vendor["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "9000.00"}],
        },
    )
    await post(client, h, f"/api/v1/purchase-orders/{mine['id']}/issue", None, expect=200)

    # One for a different vendor, and one left as a draft.
    await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": sold_job["vendor"]["id"],
            "lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "5000.00"}],
        },
    )
    await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": vendor["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "1000.00"}],
        },
    )

    visible = (await client.get("/api/v1/portal/purchase-orders", headers=th)).json()
    assert len(visible) == 1
    assert visible[0]["po_number"] == mine["po_number"]


async def test_trade_can_acknowledge_their_po(client, sold_job):
    vendor, th = await _trade_login(client, sold_job)
    h = sold_job["headers"]

    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": vendor["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "9000.00"}],
        },
    )
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)

    acked = await post(
        client, th, f"/api/v1/portal/purchase-orders/{po['id']}/acknowledge", None, expect=200
    )
    assert acked["status"] == "acknowledged"


async def test_trade_job_view_exposes_no_financials(client, sold_job):
    vendor, th = await _trade_login(client, sold_job)
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": job_id,
            "vendor_id": vendor["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "9000.00"}],
        },
    )
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)

    view = (await client.get(f"/api/v1/portal/jobs/{job_id}", headers=th)).json()
    assert set(view) == {
        "job_id",
        "job_number",
        "status",
        "start_date",
        "projected_completion",
    }

    # And the builder-side endpoints stay shut to a trade account. A subcontractor must
    # never see the job budget, the buyer's contract price, or the cost catalog.
    for path in (
        f"/api/v1/jobs/{job_id}/budget",
        f"/api/v1/jobs/{job_id}/contract",
        f"/api/v1/jobs/{job_id}/cost",
        "/api/v1/cost-codes",
        "/api/v1/leads",
        "/api/v1/reports/dashboard",
        "/api/v1/reports/job-margin",
    ):
        assert (await client.get(path, headers=th)).status_code == 403, path


async def test_trade_cannot_see_a_job_they_have_no_work_on(client, sold_job):
    _, th = await _trade_login(client, sold_job)
    resp = await client.get(f"/api/v1/portal/jobs/{sold_job['job']['id']}", headers=th)
    assert resp.status_code == 404


async def test_staff_account_is_refused_the_trade_portal(client, sold_job):
    resp = await client.get("/api/v1/portal/me", headers=sold_job["headers"])
    assert resp.status_code == 403
