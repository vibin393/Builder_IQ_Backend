"""The expanded role set.

Two properties matter more than the rest:

  * an external role must never see the builder's money. Architects are new and
    external, so they get the same scrutiny trades already had.
  * a read-only role must be read-only *everywhere*, not only on the endpoints
    somebody remembered to guard. That is enforced at authentication, so these
    tests probe several unrelated surfaces rather than one.
"""

import pytest

from tests.conftest import post


async def _staff(client, headers, email, role):
    """Create a user in the builder's tenant and sign in as them."""
    await post(
        client, headers, "/api/v1/users",
        {
            "email": email,
            "full_name": role.title(),
            "password": "supersecret123",
            "role": role,
        },
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret123"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ======================================================== the new roles =====

@pytest.mark.parametrize(
    "role", ["executive", "accounting", "warranty", "architect"]
)
async def test_each_new_role_can_be_created_and_sign_in(client, builder, role):
    h = builder["headers"]
    theirs = await _staff(client, h, f"{role}@corman.example.com", role)
    me = (await client.get("/api/v1/auth/me", headers=theirs)).json()
    assert me["role"] == role


# ============================================== executive is read-only =====

async def test_executive_can_read_the_business(client, sold_job):
    h = sold_job["headers"]
    exec_h = await _staff(client, h, "exec@corman.example.com", "executive")

    for path in (
        "/api/v1/reports/dashboard",
        "/api/v1/jobs",
        "/api/v1/purchase-orders",
        "/api/v1/reports/job-margin",
        "/api/v1/reports/trade-scorecard",
    ):
        resp = await client.get(path, headers=exec_h)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("post", "/api/v1/vendors",
         {"code": "X", "name": "X", "vendor_type": "trade"}),
        ("post", "/api/v1/cost-codes", {"code": "99-999", "name": "Nope"}),
        ("post", "/api/v1/leads", {"first_name": "A", "last_name": "B"}),
        ("post", "/api/v1/rfis",
         {"job_id": "00000000-0000-0000-0000-000000000000", "subject": "s", "question": "q"}),
    ],
)
async def test_executive_cannot_change_anything(client, sold_job, method, path, payload):
    """Enforced once at authentication, so it holds on endpoints nobody guarded."""
    h = sold_job["headers"]
    exec_h = await _staff(client, h, "exec2@corman.example.com", "executive")

    resp = await getattr(client, method)(path, headers=exec_h, json=payload)
    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"]


async def test_executive_is_refused_before_validation_runs(client, sold_job):
    """A 403 rather than a 422: the role is checked before the body is."""
    h = sold_job["headers"]
    exec_h = await _staff(client, h, "exec3@corman.example.com", "executive")

    resp = await client.post("/api/v1/vendors", headers=exec_h, json={"nonsense": True})
    assert resp.status_code == 403


# ============================================= architect is external =====

async def test_architect_can_work_the_design_surfaces(client, sold_job):
    h = sold_job["headers"]
    arch = await _staff(client, h, "architect@design.example.com", "architect")

    for path in ("/api/v1/rfis", "/api/v1/submittals", "/api/v1/drawing-sets",
                 "/api/v1/specifications"):
        resp = await client.get(path, headers=arch)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/purchase-orders",
        "/api/v1/invoices",
        "/api/v1/leads",
        "/api/v1/reports/job-margin",
        "/api/v1/reports/dashboard",
        "/api/v1/reports/trade-scorecard",
        "/api/v1/vendors",
    ],
)
async def test_architect_never_sees_the_builders_money(client, sold_job, path):
    """The same bar a trade account has to clear. An architect is not staff."""
    h = sold_job["headers"]
    arch = await _staff(client, h, "architect2@design.example.com", "architect")

    resp = await client.get(path, headers=arch)
    assert resp.status_code == 403, f"{path} leaked to an architect ({resp.status_code})"


async def test_architect_cannot_read_a_jobs_budget(client, sold_job):
    h = sold_job["headers"]
    arch = await _staff(client, h, "architect3@design.example.com", "architect")

    resp = await client.get(f"/api/v1/jobs/{sold_job['job']['id']}/cost", headers=arch)
    assert resp.status_code == 403


# ================================================= the other new roles =====

async def test_accounting_can_reach_the_payables_side(client, sold_job):
    h = sold_job["headers"]
    acct = await _staff(client, h, "acct@corman.example.com", "accounting")

    for path in ("/api/v1/invoices", "/api/v1/purchase-orders",
                 "/api/v1/accounting/queue"):
        resp = await client.get(path, headers=acct)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


async def test_warranty_can_reach_claims_and_service(client, sold_job):
    h = sold_job["headers"]
    warr = await _staff(client, h, "warranty@corman.example.com", "warranty")

    resp = await client.get("/api/v1/warranty/requests", headers=warr)
    assert resp.status_code == 200


async def test_warranty_role_is_still_staff_for_reads(client, sold_job):
    """Customer care needs the home's context to answer the phone."""
    h = sold_job["headers"]
    warr = await _staff(client, h, "warranty2@corman.example.com", "warranty")

    resp = await client.get("/api/v1/jobs", headers=warr)
    assert resp.status_code == 200


# ========================================= no orphaned ball-in-court =====

async def test_every_ball_in_court_target_has_a_role_behind_it():
    """An RFI must never be owed by somebody who cannot log in."""
    from app.models.communication import COURT_ROLE, BallInCourt
    from app.models.user import UserRole

    roles = {r.value for r in UserRole}

    # Every court must be in the mapping...
    unmapped = set(BallInCourt) - set(COURT_ROLE)
    assert not unmapped, f"ball_in_court values with no mapping: {unmapped}"

    # ...and every role it names must be one somebody can actually log in as.
    for court, role in COURT_ROLE.items():
        assert role is None or role in roles, f"{court} maps to unknown role {role!r}"
