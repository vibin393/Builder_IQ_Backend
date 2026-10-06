"""Fill in the blanks on a demo tenant's leads.

Why this exists
---------------
The earliest demo tenants were seeded before the Leads feature had contact
details, ratings, owners or follow-up dates. Their leads are real records with
half their fields empty, which makes the screen look broken when it is not: the
contact column shows "No email" because there is no email, and Convert to buyer
is correctly refused because a buyer with no way to reach them cannot be sent a
payment request.

Re-seeding would mean deleting the tenant and everything in it. This does the
smaller thing instead.

What it will and will not touch
-------------------------------
It only ever fills a field that is **empty**. It never overwrites a value that is
already there, never changes a status, and never touches a lead that has been
converted or lost - those are finished records and their emptiness is history, not
a gap.

Run it against whichever demo account you are showing:

    CORMAN_DEMO_EMAIL=demo@cormanhomes.com python scripts/topup_demo_leads.py
"""

import asyncio
import os
import sys
from datetime import date, timedelta

import httpx

BASE = os.environ.get("CORMAN_API_BASE", "http://127.0.0.1:8400") + "/api/v1"
EMAIL = os.environ.get("CORMAN_DEMO_EMAIL", "demo@cormanhomes.com")
PASSWORD = os.environ.get("CORMAN_DEMO_PASSWORD", "Demo12345!")

TOWNS = ["Poole", "Bournemouth", "Wimborne", "Christchurch", "Ferndown"]
RATINGS = ["hot", "warm", "cold", "warm", "hot"]
#: Spread across the states the list is read for: overdue, due today, upcoming,
#: and one left deliberately blank so the "no next step" case is visible.
FOLLOW_UP_OFFSETS = [-9, 0, 4, -3, None]


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        auth = await c.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
        if auth.status_code != 200:
            print(f"Could not sign in as {EMAIL}: {auth.status_code} {auth.text[:200]}")
            sys.exit(1)
        h = {"Authorization": f"Bearer {auth.json()['access_token']}"}

        staff = (await c.get("/users", headers=h)).json()["items"]
        sellers = [u for u in staff if u["role"] in ("sales", "owner") and u["is_active"]]

        leads = (await c.get("/leads?size=200", headers=h)).json()["items"]
        open_leads = [x for x in leads if x["is_open"]]
        if not open_leads:
            print("No open leads to top up.")
            return

        filled = 0
        for i, lead in enumerate(open_leads):
            patch: dict = {}
            surname = lead["last_name"].lower().replace(" ", "")
            first = lead["first_name"].lower().replace(" ", "")

            if not lead["email"]:
                patch["email"] = f"{first}.{surname}@example.com"
            if not lead["phone"] and not lead["mobile"]:
                # A UK mobile, so the number is textable and the list shows the
                # form somebody would actually dial.
                patch["mobile"] = f"07700 9001{i:02d}"
            if not lead["city"]:
                patch["city"] = TOWNS[i % len(TOWNS)]
            if not lead["rating"]:
                patch["rating"] = RATINGS[i % len(RATINGS)]

            if patch:
                r = await c.patch(f"/leads/{lead['id']}", headers=h, json=patch)
                r.raise_for_status()
                filled += 1

            # Ownership and the follow-up date go through their own routes, so the
            # rules and the history entries apply exactly as they would if a
            # person had clicked the buttons.
            if not lead["assigned_to_id"] and sellers and i % 3 != 0:
                await c.post(
                    f"/leads/{lead['id']}/assign",
                    headers=h,
                    json={"assigned_to_id": sellers[i % len(sellers)]["id"]},
                )

            offset = FOLLOW_UP_OFFSETS[i % len(FOLLOW_UP_OFFSETS)]
            if not lead["next_follow_up"] and offset is not None:
                await c.post(
                    f"/leads/{lead['id']}/follow-up",
                    headers=h,
                    json={"next_follow_up": (date.today() + timedelta(days=offset)).isoformat()},
                )

        summary = (await c.get("/leads/summary", headers=h)).json()
        print(f"Topped up {filled} of {len(open_leads)} open leads for {EMAIL}.")
        print(
            f"  open {summary['open']} | unassigned {summary['unassigned']} | "
            f"needs chasing {summary['overdue']} | due today {summary['due_today']}"
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.HTTPStatusError as exc:
        print(
            f"Failed: {exc.request.method} {exc.request.url.path} -> "
            f"{exc.response.status_code} {exc.response.text[:300]}",
            file=sys.stderr,
        )
        sys.exit(1)
