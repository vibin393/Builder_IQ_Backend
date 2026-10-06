"""The property the whole multi-tenant model rests on: tenant A never sees tenant B."""

from httpx import AsyncClient


async def _signup(client: AsyncClient, company: str, email: str) -> str:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "company_name": company,
            "full_name": "Owner",
            "email": email,
            "password": "supersecret123",
        },
    )
    assert resp.status_code == 201
    return resp.json()["access_token"]


async def test_user_list_is_scoped_to_own_tenant(client: AsyncClient) -> None:
    token_a = await _signup(client, "Builder A", "a@a.example.com")
    token_b = await _signup(client, "Builder B", "b@b.example.com")

    for token, expected_email in ((token_a, "a@a.example.com"), (token_b, "b@b.example.com")):
        resp = await client.get(
            "/api/v1/users", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["email"] == expected_email


async def test_cannot_fetch_other_tenants_user_by_id(client: AsyncClient) -> None:
    token_a = await _signup(client, "Builder A", "a@a.example.com")
    token_b = await _signup(client, "Builder B", "b@b.example.com")

    b_users = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token_b}"})
    b_user_id = b_users.json()["items"][0]["id"]

    resp = await client.get(
        f"/api/v1/users/{b_user_id}", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert resp.status_code == 404


async def test_non_admin_cannot_create_users(client: AsyncClient) -> None:
    owner_token = await _signup(client, "Builder A", "owner@a.example.com")

    created = await client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "email": "super@a.example.com",
            "full_name": "Site Super",
            "password": "supersecret123",
            "role": "superintendent",
        },
    )
    assert created.status_code == 201

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "super@a.example.com", "password": "supersecret123"},
    )
    super_token = login.json()["access_token"]

    resp = await client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {super_token}"},
        json={
            "email": "another@a.example.com",
            "full_name": "Another",
            "password": "supersecret123",
            "role": "sales",
        },
    )
    assert resp.status_code == 403
