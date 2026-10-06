from httpx import AsyncClient

REGISTRATION = {
    "company_name": "Corman Homes",
    "full_name": "Test Owner",
    "email": "owner@corman.example.com",
    "password": "supersecret123",
}


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_register_returns_tokens(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/register", json=REGISTRATION)
    assert resp.status_code == 201
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]


async def test_login_and_me(client: AsyncClient) -> None:
    await client.post("/api/v1/auth/register", json=REGISTRATION)

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": REGISTRATION["email"], "password": REGISTRATION["password"]},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == REGISTRATION["email"]
    assert me.json()["role"] == "owner"


async def test_login_rejects_bad_password(client: AsyncClient) -> None:
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": REGISTRATION["email"], "password": "wrong-password"},
    )
    assert resp.status_code == 401


async def test_me_requires_token(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == 401
