"""Add a few more named staff accounts to the existing demo tenant.

Run the API first, then:  python scripts/seed_more_staff.py

Adds Wayne (a super-admin / admin), plus Mandy, Megan O'Brien and Susan across
a spread of departments so the People/Users screen and the "assigned to" pickers
have real names on them.

Safe to re-run: an account that already exists is left alone.
"""

import asyncio
import os

import httpx

BASE = os.environ.get("CORMAN_API_BASE", "http://127.0.0.1:8400") + "/api/v1"
DOMAIN = os.environ.get("CORMAN_DEMO_DOMAIN", "cormanhomes.com")
OWNER_EMAIL = os.environ.get("CORMAN_DEMO_EMAIL", f"demo@{DOMAIN}")
PASSWORD = os.environ.get("CORMAN_DEMO_PASSWORD", "Demo12345!")

# (email, full name, role). "Super admin" maps to the admin role — the highest
# non-owner role the platform has.
NEW_STAFF = [
    (f"wayne@{DOMAIN}", "Wayne", "admin"),
    (f"mandy@{DOMAIN}", "Mandy", "sales"),
    (f"megan.obrien@{DOMAIN}", "Megan O'Brien", "superintendent"),
    (f"susan@{DOMAIN}", "Susan", "purchasing"),
]


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        login = await c.post(
            "/auth/login", json={"email": OWNER_EMAIL, "password": PASSWORD}
        )
        login.raise_for_status()
        h = {"Authorization": f"Bearer {login.json()['access_token']}"}

        created = 0
        for email, name, role in NEW_STAFF:
            r = await c.post(
                "/users",
                headers=h,
                json={"email": email, "full_name": name, "password": PASSWORD, "role": role},
            )
            if r.status_code == 409 or (r.status_code >= 400 and "exist" in r.text.lower()) or (
                r.status_code == 500 and "duplicate" in r.text.lower()
            ):
                print(f"  = {name} <{email}> already exists")
                continue
            if r.status_code >= 400:
                print(f"  ! {name} <{email}> failed: {r.status_code} {r.text[:160]}")
                continue
            created += 1
            print(f"  + {name:16} {email:32} {role}")

        print(f"\nDone. {created} new account(s). All sign in with password: {PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
