"""Test fixtures: in-memory SQLite, isolated per test."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.main import app
from app.models import Base

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --- Helpers ------------------------------------------------------------------

PASSWORD = "supersecret123"


async def signup(client: AsyncClient, company: str, email: str) -> dict[str, str]:
    """Register a tenant and return auth headers for its owner."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "company_name": company,
            "full_name": "Owner",
            "email": email,
            "password": PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def post(client: AsyncClient, headers, path: str, payload=None, expect: int = 201):
    """POST and assert the status, surfacing the response body when it does not match."""
    resp = await client.post(path, headers=headers, json=payload)
    assert resp.status_code == expect, f"{path} -> {resp.status_code}: {resp.text}"
    return resp.json()


# --- Domain fixtures ----------------------------------------------------------


@pytest.fixture
async def builder(client: AsyncClient) -> dict:
    """A builder configured the way a real one would be: codes, a trade, a plan, an option.

    The numbers are fixed so tests can assert exact money:
      plan standard cost  58,000 framing + 22,000 electrical = 80,000
      option              price 8,500 / cost 5,200 (4,000 framing + 1,200 electrical) / +3 days
    """
    headers = await signup(client, "Cascade Homes", "owner@cascade.example.com")

    framing = await post(
        client, headers, "/api/v1/cost-codes",
        {"code": "06-100", "name": "Framing labour", "division": "06 Wood"},
    )
    electrical = await post(
        client, headers, "/api/v1/cost-codes",
        {"code": "16-100", "name": "Electrical rough-in", "division": "16 Electrical"},
    )

    vendor = await post(
        client, headers, "/api/v1/vendors",
        {"code": "SPARK", "name": "Sparks Electrical", "vendor_type": "trade"},
    )

    community = await post(
        client, headers, "/api/v1/communities",
        {"code": "OAK", "name": "Oak Ridge", "city": "Poole", "county": "Dorset"},
    )
    lot = await post(
        client, headers, f"/api/v1/communities/{community['id']}/lots",
        {
            "lot_number": "47",
            "address": "12 Oak Ridge Way",
            "postcode": "BH15 1AA",
            "premium": "15000.00",
        },
    )

    plan = await post(
        client, headers, "/api/v1/plans",
        {"code": "P3", "name": "The Maple", "square_feet": 2400, "base_price": "420000.00"},
    )
    await post(
        client, headers, f"/api/v1/plans/{plan['id']}/cost-lines",
        {"cost_code_id": framing["id"], "amount": "58000.00"},
    )
    await post(
        client, headers, f"/api/v1/plans/{plan['id']}/cost-lines",
        {"cost_code_id": electrical["id"], "amount": "22000.00"},
    )

    option = await post(
        client, headers, "/api/v1/options",
        {
            "code": "KIT-UP",
            "name": "Gourmet kitchen package",
            "category": "interior",
            "price": "8500.00",
            "schedule_days": 3,
        },
    )
    await post(
        client, headers, f"/api/v1/options/{option['id']}/cost-lines",
        {"cost_code_id": electrical["id"], "amount": "1200.00", "vendor_id": vendor["id"]},
    )
    await post(
        client, headers, f"/api/v1/options/{option['id']}/cost-lines",
        {"cost_code_id": framing["id"], "amount": "4000.00"},
    )

    return {
        "headers": headers,
        "framing": framing,
        "electrical": electrical,
        "vendor": vendor,
        "community": community,
        "lot": lot,
        "plan": plan,
        "option": option,
    }


@pytest.fixture
async def sold_job(client: AsyncClient, builder: dict) -> dict:
    """A job sold and budgeted: the state a home is in before selections start."""
    h = builder["headers"]

    job = await post(
        client, h, "/api/v1/jobs",
        {
            "community_id": builder["community"]["id"],
            "lot_id": builder["lot"]["id"],
            "plan_id": builder["plan"]["id"],
            "projected_completion": "2026-06-01",
        },
    )
    await post(
        client, h, f"/api/v1/jobs/{job['id']}/contract",
        {
            "buyer": {"first_name": "Dana", "last_name": "Reyes"},
            "base_price": "420000.00",
            "lot_premium": "15000.00",
            "incentives": "8000.00",
        },
    )
    await post(client, h, f"/api/v1/jobs/{job['id']}/budget", None)
    return {**builder, "job": job}
