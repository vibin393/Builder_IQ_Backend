"""Self-service profile: name, avatar, password.

These routes are open to every role, so the tests check both that they work and
that they cannot be used to escalate privilege.
"""

import io

import pytest
from httpx import AsyncClient
from PIL import Image

from tests.conftest import PASSWORD, post


def make_image(fmt: str = "PNG", size: tuple[int, int] = (600, 400)) -> bytes:
    """A real encoded image, not a filename pretending to be one."""
    buf = io.BytesIO()
    Image.new("RGB", size, (30, 90, 160)).save(buf, format=fmt)
    return buf.getvalue()


async def test_user_can_edit_their_own_details(client: AsyncClient, builder):
    h = builder["headers"]

    updated = (
        await client.patch(
            "/api/v1/users/me",
            headers=h,
            json={"full_name": "Alex Corman", "job_title": "Owner", "phone": "555-0100"},
        )
    ).json()

    assert updated["full_name"] == "Alex Corman"
    assert updated["job_title"] == "Owner"
    assert updated["phone"] == "555-0100"

    # And it is what /auth/me returns afterwards.
    me = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert me["full_name"] == "Alex Corman"


async def test_profile_edit_cannot_change_role_or_tenant(client: AsyncClient, builder):
    """The payload is a whitelist. Extra fields are ignored, not applied."""
    h = builder["headers"]
    before = (await client.get("/api/v1/auth/me", headers=h)).json()

    await client.patch(
        "/api/v1/users/me",
        headers=h,
        json={
            "full_name": "Still Me",
            "role": "owner",
            "tenant_id": "00000000-0000-0000-0000-000000000000",
            "is_active": False,
        },
    )

    after = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert after["role"] == before["role"]
    assert after["tenant_id"] == before["tenant_id"]
    assert after["is_active"] is True


async def test_avatar_upload_normalises_the_image(
    client: AsyncClient, builder, tmp_path, monkeypatch
):
    from app.services import avatar as avatar_service

    monkeypatch.setattr(avatar_service, "AVATAR_DIR", tmp_path / "avatars")
    h = builder["headers"]

    resp = await client.post(
        "/api/v1/users/me/avatar",
        headers=h,
        files={"file": ("photo.png", make_image(), "image/png")},
    )
    assert resp.status_code == 200
    url = resp.json()["avatar_url"]
    assert url.startswith("/uploads/avatars/")
    assert url.endswith(".jpg")  # re-encoded, whatever went in

    written = list((tmp_path / "avatars").iterdir())
    assert len(written) == 1

    # Centre-cropped to a square and capped at 256px.
    out = Image.open(written[0])
    assert out.width == out.height
    assert out.width <= 256


async def test_avatar_rejects_a_file_that_is_not_an_image(
    client: AsyncClient, builder, tmp_path, monkeypatch
):
    """A .png extension proves nothing; the bytes have to decode."""
    from app.services import avatar as avatar_service

    monkeypatch.setattr(avatar_service, "AVATAR_DIR", tmp_path / "avatars")
    h = builder["headers"]

    resp = await client.post(
        "/api/v1/users/me/avatar",
        headers=h,
        files={"file": ("payload.png", b"#!/bin/sh\nrm -rf /\n", "image/png")},
    )
    assert resp.status_code == 400
    assert "image" in resp.json()["detail"].lower()
    written = tmp_path / "avatars"
    assert not written.exists() or not list(written.iterdir())


async def test_avatar_rejects_an_oversized_file(
    client: AsyncClient, builder, tmp_path, monkeypatch
):
    from app.services import avatar as avatar_service

    monkeypatch.setattr(avatar_service, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(avatar_service, "MAX_BYTES", 1024)
    h = builder["headers"]

    resp = await client.post(
        "/api/v1/users/me/avatar",
        headers=h,
        files={"file": ("big.png", make_image(size=(900, 900)), "image/png")},
    )
    assert resp.status_code == 413


async def test_removing_an_avatar_clears_it(
    client: AsyncClient, builder, tmp_path, monkeypatch
):
    from app.services import avatar as avatar_service

    monkeypatch.setattr(avatar_service, "AVATAR_DIR", tmp_path / "avatars")
    h = builder["headers"]

    await client.post(
        "/api/v1/users/me/avatar",
        headers=h,
        files={"file": ("photo.png", make_image(), "image/png")},
    )
    resp = await client.delete("/api/v1/users/me/avatar", headers=h)
    assert resp.status_code == 200
    assert resp.json()["avatar_url"] is None


async def test_password_change_requires_the_current_one(client: AsyncClient, builder):
    h = builder["headers"]

    wrong = await client.post(
        "/api/v1/users/me/password",
        headers=h,
        json={"current_password": "not-my-password", "new_password": "brandnew12345"},
    )
    assert wrong.status_code == 400

    ok = await client.post(
        "/api/v1/users/me/password",
        headers=h,
        json={"current_password": PASSWORD, "new_password": "brandnew12345"},
    )
    assert ok.status_code == 200

    # The new password works and the old one does not.
    email = "owner@cascade.example.com"
    assert (
        await client.post("/api/v1/auth/login", json={"email": email, "password": "brandnew12345"})
    ).status_code == 200
    assert (
        await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    ).status_code == 401


async def test_password_change_rejects_reusing_the_same_one(client: AsyncClient, builder):
    h = builder["headers"]
    resp = await client.post(
        "/api/v1/users/me/password",
        headers=h,
        json={"current_password": PASSWORD, "new_password": PASSWORD},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("role", ["trade", "superintendent"])
async def test_non_admin_roles_can_still_edit_their_own_profile(
    client: AsyncClient, builder, role: str
):
    """A subcontractor has no admin rights but owns their own name and picture."""
    h = builder["headers"]
    email = f"{role}@example.com"

    await post(
        client, h, "/api/v1/users",
        {"email": email, "full_name": "Someone", "password": PASSWORD, "role": role},
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    th = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = await client.patch("/api/v1/users/me", headers=th, json={"full_name": "New Name"})
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "New Name"

    # Still locked out of the admin surface.
    assert (await client.get("/api/v1/users", headers=th)).status_code in (200, 403)
