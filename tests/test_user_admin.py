"""User administration: the rules that stop a builder losing control of its own account,
plus search, the summary, last sign-in and password reset."""

from tests.conftest import PASSWORD, post

API = "/api/v1"


async def _user(client, h, email, role, name="Someone Else"):
    return await post(
        client, h, f"{API}/users",
        {"email": email, "full_name": name, "password": PASSWORD, "role": role},
    )


async def _login(client, email, password=PASSWORD):
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _me(client, h):
    return (await client.get(f"{API}/users/me", headers=h)).json()


async def test_an_admin_cannot_make_themselves_owner(client, builder):
    """The escalation: PATCH your own role to owner."""
    h = builder["headers"]
    admin = await _user(client, h, "admin@corman.example.com", "admin")
    ah = await _login(client, "admin@corman.example.com")

    r = await client.patch(f"{API}/users/{admin['id']}", headers=ah, json={"role": "owner"})
    assert r.status_code == 403
    r = await client.post(
        f"{API}/users", headers=ah,
        json={"email": "x@corman.example.com", "full_name": "New Owner", "password": PASSWORD,
              "role": "owner"},
    )
    assert r.status_code == 403


async def test_nobody_locks_themselves_out(client, builder):
    h = builder["headers"]
    me = await _me(client, h)
    r = await client.patch(f"{API}/users/{me['id']}", headers=h, json={"is_active": False})
    assert r.status_code == 409
    r = await client.patch(f"{API}/users/{me['id']}", headers=h, json={"role": "sales"})
    assert r.status_code == 409


async def test_the_last_owner_stays_an_owner(client, builder):
    h = builder["headers"]
    me = await _me(client, h)
    second = await _user(client, h, "owner2@corman.example.com", "owner", "Second Owner")
    oh = await _login(client, "owner2@corman.example.com")

    # Two owners: the second may demote the first...
    r = await client.patch(f"{API}/users/{me['id']}", headers=oh, json={"role": "admin"})
    assert r.status_code == 200
    # ...and now nobody can remove the only one left.
    r = await client.patch(f"{API}/users/{second['id']}", headers=h, json={"is_active": False})
    assert r.status_code in (403, 409)


async def test_search_filters_and_summary(client, builder):
    h = builder["headers"]
    await _user(client, h, "sally@corman.example.com", "sales", "Sally Seller")
    await _user(client, h, "pat@corman.example.com", "purchasing", "Pat Buyer")
    trade = await _user(client, h, "trade@sparks.example.com", "trade", "Sparks Portal")

    found = (await client.get(f"{API}/users?search=seller", headers=h)).json()
    assert [u["email"] for u in found["items"]] == ["sally@corman.example.com"]
    buyers = (await client.get(f"{API}/users?role=purchasing", headers=h)).json()
    assert buyers["total"] == 1

    await client.patch(f"{API}/users/{trade['id']}", headers=h, json={"is_active": False})
    inactive = (await client.get(f"{API}/users?active=false", headers=h)).json()
    assert [u["id"] for u in inactive["items"]] == [trade["id"]]

    s = (await client.get(f"{API}/users/summary", headers=h)).json()
    assert s["total"] == 4 and s["inactive"] == 1 and s["owners"] == 1
    assert s["never_signed_in"] == 2          # Sally and Pat; the owner signed up


async def test_sign_in_is_recorded_and_a_password_can_be_reset(client, builder):
    h = builder["headers"]
    u = await _user(client, h, "forgetful@corman.example.com", "sales", "Forgetful Fred")
    assert u["last_login_at"] is None
    await _login(client, "forgetful@corman.example.com")
    again = (await client.get(f"{API}/users/{u['id']}", headers=h)).json()
    assert again["last_login_at"] is not None

    r = await post(
        client, h, f"{API}/users/{u['id']}/reset-password",
        {"new_password": "temporary-pass-1"}, expect=200,
    )
    assert "Forgetful Fred" in r["detail"]
    await _login(client, "forgetful@corman.example.com", "temporary-pass-1")
    old = await client.post(
        f"{API}/auth/login", json={"email": "forgetful@corman.example.com", "password": PASSWORD}
    )
    assert old.status_code == 401
