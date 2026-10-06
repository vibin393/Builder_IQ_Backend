"""Add demo data for the newer modules to the existing demo tenant.

Run the API first, then:  python scripts/seed_extended.py

Adds bid packages, field activity (daily logs, inspections, punch items), a design
appointment and signature, a marketing campaign, homeowner surveys, and a land project,
so every screen has something realistic on it.

Safe to re-run: each section skips what already exists.
"""

import asyncio
import os
import sys
from datetime import UTC, date, datetime, time, timedelta

import httpx

# Override with CORMAN_API_BASE when the API runs somewhere else.
BASE = os.environ.get("CORMAN_API_BASE", "http://127.0.0.1:8400") + "/api/v1"
# Overridable, like scripts/seed_demo_account.py. This was hardcoded, so every
# run signed into the first demo tenant whatever account was asked for - and
# because each section skips what already exists, it reported success while
# creating nothing in the tenant somebody actually meant.
OWNER_EMAIL = os.environ.get("CORMAN_DEMO_EMAIL", "demo@cormanhomes.com")
PASSWORD = os.environ.get("CORMAN_DEMO_PASSWORD", "Demo12345!")


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        login = await c.post("/auth/login", json={"email": OWNER_EMAIL, "password": PASSWORD})
        if login.status_code != 200:
            print(
                f"Could not sign in as {OWNER_EMAIL}. Run scripts/seed_demo_account.py first.",
                file=sys.stderr,
            )
            sys.exit(1)

        h = {"Authorization": f"Bearer {login.json()['access_token']}"}

        async def P(path, payload=None, ok=(200, 201), quiet=False):
            """POST, and say so when it does not work.

            This used to return None on any failure without a word, so a block
            that failed every call still reported success and left the screen
            empty. Some calls here are genuinely optional - those pass quiet.
            """
            r = await c.post(path, headers=h, json=payload)
            if r.status_code not in ok:
                if not quiet:
                    print(
                        f"  ! {path} -> {r.status_code} {r.text[:200]}",
                        file=sys.stderr,
                    )
                return None
            return r.json()

        async def G(path):
            """GET a list. Paged endpoints return {items, total}; unpaged ones a
            bare list. Normalising here beats remembering which is which."""
            r = await c.get(path, headers=h)
            r.raise_for_status()
            body = r.json()
            return body["items"] if isinstance(body, dict) and "items" in body else body

        codes = {x["code"]: x for x in await G("/cost-codes")}
        vendors = {x["code"]: x for x in await G("/vendors")}
        plans = {x["code"]: x for x in await G("/plans")}
        jobs = await G("/jobs")
        if not jobs:
            print("No homes found. Run scripts/seed_demo_account.py first.", file=sys.stderr)
            sys.exit(1)

        building = next((j for j in jobs if j["status"] == "under_construction"), jobs[0])
        closed = next((j for j in jobs if j["status"] == "closed"), jobs[-1])
        today = date.today()

        # --- Bidding ---------------------------------------------------------
        if not await G("/bid-packages"):
            pkg = await P(
                "/bid-packages",
                {
                    "name": "Electrical rough-in, The Maple",
                    "cost_code_id": codes["16-100"]["id"],
                    "plan_id": plans["P3"]["id"],
                    "scope": "Complete rough-in per plan set rev 1, including EV conduit.",
                    "due_date": (today + timedelta(days=10)).isoformat(),
                },
            )
            if pkg:
                pkg = await P(
                    f"/bid-packages/{pkg['id']}/invite",
                    {"vendor_ids": [vendors["SPARK"]["id"], vendors["PLUMB"]["id"]]},
                )
                for bid, amount in zip(pkg["bids"], ("22800.00", "24150.00"), strict=False):
                    await P(
                        f"/bid-packages/{pkg['id']}/bids/{bid['id']}/submit",
                        {"amount": amount, "notes": "Excludes fixtures."},
                    )

            pkg2 = await P(
                "/bid-packages",
                {
                    "name": "Drywall, all plans",
                    "cost_code_id": codes["09-300"]["id"],
                    "scope": "Hang, tape and finish, level 4.",
                },
            )
            if pkg2:
                await P(
                    f"/bid-packages/{pkg2['id']}/invite", {"vendor_ids": [vendors["DRY"]["id"]]}
                )
            print("Bidding      2 packages, 3 invitations, 2 bids submitted")

        # --- Field operations ------------------------------------------------
        if not await G("/field/daily-logs"):
            # Dated across three days. These had no date at all, so all three
            # landed on today - and a house with three contradictory logs for one
            # day is the opposite of a record that substantiates anything.
            for days_ago, (weather, crew, work, delay) in enumerate(
                [
                    ("Clear, 22C", 8, "Framing continued on the second floor.", None),
                    ("Rain, 14C", 3, "Interior work only.", "Half day lost to rain."),
                    ("Overcast, 18C", 7, "Roof decking started.", None),
                ]
            ):
                await P(
                    "/field/daily-logs",
                    {
                        "job_id": building["id"],
                        "log_date": (today - timedelta(days=days_ago)).isoformat(),
                        "weather": weather,
                        "crew_count": crew,
                        "trades_on_site": "Titan Framing",
                        "work_performed": work,
                        "delays": delay,
                    },
                )

            # A real inspection is a checklist, and the interesting one fails.
            insp1 = await P(
                "/field/inspections",
                {
                    "job_id": building["id"],
                    "name": "Foundation inspection",
                    "inspection_type": "municipal",
                    "scheduled_date": (today - timedelta(days=20)).isoformat(),
                    "inspector": "BCP Council building control",
                },
            )
            if insp1:
                for desc in [
                    "Reinforcement to drawing",
                    "Damp proof membrane lapped and sealed",
                    "Concrete cover to rebar",
                ]:
                    item = await P(
                        f"/field/inspections/{insp1['id']}/items",
                        {"description": desc, "vendor_id": vendors["FRAME"]["id"]},
                    )
                    if item:
                        await P(
                            f"/field/inspections/items/{item['id']}/result",
                            {"result": "pass"},
                        )
                await P(f"/field/inspections/{insp1['id']}/result", {"result": "passed"})

            insp2 = await P(
                "/field/inspections",
                {
                    "job_id": building["id"],
                    "name": "Framing inspection",
                    "inspection_type": "municipal",
                    "scheduled_date": (today - timedelta(days=3)).isoformat(),
                    "inspector": "BCP Council building control",
                },
            )
            if insp2:
                # Two of five checks fail. The punch list is then generated from
                # them rather than retyped, which is the whole point: retyping
                # thirty failed checks is how three of them get missed.
                checks = [
                    ("Truss connections strapped", "fail", "Hurricane ties missing at four connections."),
                    ("Wall plates fixed to spec", "pass", None),
                    ("Notching within limits", "fail", "Joist notched through the centre of the span."),
                    ("Fire stopping at party wall", "pass", None),
                    ("Roof bracing complete", "pass", None),
                ]
                for desc, result, note in checks:
                    item = await P(
                        f"/field/inspections/{insp2['id']}/items",
                        {"description": desc, "vendor_id": vendors["FRAME"]["id"]},
                    )
                    if item:
                        await P(
                            f"/field/inspections/items/{item['id']}/result",
                            {"result": result, "notes": note},
                        )
                await P(f"/field/inspections/{insp2['id']}/result", {"result": "failed"})
                await P(f"/field/inspections/{insp2['id']}/punch-from-failures")
                # Small, relentless, and nobody argues one back to a trade
                # without a number against it.
                await P(
                    f"/field/inspections/{insp2['id']}/reinspect",
                    {
                        "scheduled_date": (today + timedelta(days=4)).isoformat(),
                        "fee": "150.00",
                    },
                )

            await P(
                "/field/inspections",
                {
                    "job_id": building["id"],
                    "name": "Buyer walkthrough",
                    "inspection_type": "walkthrough",
                    "scheduled_date": (today + timedelta(days=30)).isoformat(),
                },
            )

            # Severity is what makes the handover gate possible: without it the
            # rule would have to be either every scuff or nothing at all.
            for desc, loc, vendor, severity, walkthrough, stage in [
                (
                    "Soil pipe cracked below the ground floor WC",
                    "Ground floor WC",
                    "PLUMB",
                    "blocking",
                    False,
                    "open",
                ),
                (
                    "Bathroom extract not ducted to outside",
                    "Family bathroom",
                    "SPARK",
                    "major",
                    True,
                    "review",
                ),
                (
                    "Scuff on hallway wall",
                    "Upstairs hallway",
                    "DRY",
                    "cosmetic",
                    True,
                    "signed",
                ),
                (
                    "Outlet cover plate cracked",
                    "Master bedroom",
                    "SPARK",
                    "minor",
                    True,
                    "open",
                ),
            ]:
                item = await P(
                    "/field/punch-items",
                    {
                        "job_id": building["id"],
                        "description": desc,
                        "location": loc,
                        "severity": severity,
                        "vendor_id": vendors[vendor]["id"],
                        "due_date": (today + timedelta(days=7)).isoformat(),
                        "from_walkthrough": walkthrough,
                    },
                )
                if not item or stage == "open":
                    continue
                # Walk it far enough to show the state it is meant to sit in. A
                # list where everything is open never exercises the sign-off
                # step, which is the step this feature exists to add.
                await P(f"/field/punch-items/{item['id']}/submit", {"note": "Fix made."})
                if stage == "signed":
                    await P(f"/field/punch-items/{item['id']}/verify")
            print(
                "Field ops    3 daily logs, 4 inspections (1 failed + re-inspection),"
                " 8 checks, punch list incl. 1 blocking handover"
            )

        # --- Design studio and e-signature -----------------------------------
        if not await G("/appointments"):
            await P(
                "/appointments",
                {
                    "job_id": building["id"],
                    # A real date. This was omitted, and the studio showed a row
                    # reading "Not set" - an appointment with no time is a note.
                    "scheduled_for": (
                        datetime.combine(today + timedelta(days=5), time(10, 0), tzinfo=UTC)
                    ).isoformat(),
                    "location": "Design studio, Poole",
                    "duration_minutes": 180,
                    # Close enough to show up on the cutoff board, which is the
                    # state the screen exists to make visible.
                    "structural_cutoff": (today + timedelta(days=14)).isoformat(),
                },
            )
            # Without an allowance the studio panel has nothing to show, and the
            # question the room is there to answer cannot be answered.
            await P(f"/jobs/{building['id']}/allowance", {"amount": "15000.00"})

            sig = await P(
                "/signatures",
                {
                    "job_id": building["id"],
                    "document_kind": "addendum",
                    "document_name": "Design selections addendum #1",
                    "signer_name": "Dana Reyes",
                    "signer_email": "dana.reyes@example.com",
                },
            )
            if sig:
                await P(f"/signatures/{sig['id']}/send")
            print("Design       1 appointment, 1 signature request sent")

        # --- Marketing --------------------------------------------------------
        # Three campaigns that disagree with each other on purpose: the one with
        # the best open rate is not the one with the best cost per sale, and the
        # road signs have no open rate at all. A set where the loudest campaign is
        # also the best one would teach the screen nothing.
        if not await G("/campaigns"):
            parade = await P(
                "/campaigns",
                {
                    "code": "spring-parade",
                    "name": "Spring Parade of Homes",
                    "channel": "email",
                    "subject": "Tour our new Oak Ridge models this weekend",
                    "budget": "5000.00",
                    "cost": "4800.00",
                },
            )
            if parade:
                await P(f"/campaigns/{parade['id']}/launch", {})
                await P(
                    f"/campaigns/{parade['id']}/delivery",
                    {"sent_count": 4200, "opened_count": 1134, "clicked_count": 287},
                )
                await P(f"/campaigns/{parade['id']}/attach-leads", quiet=True)
                await P(f"/campaigns/{parade['id']}/complete", {})

            quick = await P(
                "/campaigns",
                {
                    "code": "quick-move-in",
                    "name": "Quick move-in homes",
                    "channel": "email",
                    "subject": "Move in before the holidays",
                    "budget": "2000.00",
                    "cost": "1900.00",
                },
            )
            if quick:
                await P(f"/campaigns/{quick['id']}/launch", {})
                # A better open rate than the parade, and - once the leads are
                # counted - a worse cost per sale. That contrast is the whole
                # argument for leading with money rather than with opens.
                await P(
                    f"/campaigns/{quick['id']}/delivery",
                    {"sent_count": 1800, "opened_count": 612, "clicked_count": 198},
                )
                await P(f"/campaigns/{quick['id']}/attach-leads", quiet=True)
                await P(f"/campaigns/{quick['id']}/complete", {})

            boards = await P(
                "/campaigns",
                {
                    "code": "a31-boards",
                    "name": "A31 roadside boards",
                    "channel": "signage",
                    "budget": "3000.00",
                    "cost": "3400.00",
                    "starts_on": (today - timedelta(days=60)).isoformat(),
                    "ends_on": (today + timedelta(days=30)).isoformat(),
                    "notes": "Two boards on the eastbound approach.",
                },
            )
            if boards:
                # Over budget, running, and with no open rate to hide behind.
                await P(f"/campaigns/{boards['id']}/launch", {})

            print("Marketing    3 campaigns: 2 email, 1 signage, all costed")

        # --- Surveys ----------------------------------------------------------
        if not await G("/surveys"):
            for job, stage, score, sales, cons, qual, warr, comment in [
                (closed, "thirty_day", 10, 5, 5, 5, 4, "Smoothest build we could have hoped for."),
                (closed, "eleven_month", 6, 4, 3, 3, 2,
                 "Warranty response has been slow. Tiles still not fixed."),
                (building, "contract", 9, 5, 4, 4, 4, "Sales process was excellent."),
            ]:
                s = await P("/surveys", {"job_id": job["id"], "stage": stage})
                if s:
                    await P(
                        f"/surveys/{s['id']}/respond",
                        {
                            "would_recommend": score,
                            "rating_sales": sales,
                            "rating_construction": cons,
                            "rating_quality": qual,
                            "rating_warranty": warr,
                            "comments": comment,
                        },
                    )
            print("Surveys      3 responses (1 promoter, 1 passive, 1 detractor)")

        # --- Land development -------------------------------------------------
        if not await G("/land-projects"):
            proj = await P(
                "/land-projects",
                {
                    "code": "OAK-P2",
                    "name": "Oak Ridge Phase 2",
                    "acres": "42.50",
                    "planned_lots": 96,
                    "acquisition_cost": "1850000.00",
                    "start_date": (today - timedelta(days=120)).isoformat(),
                },
            )
            if proj:
                for category, budget, actual in [
                    ("Engineering and entitlement", "240000.00", "218400.00"),
                    ("Earthwork", "810000.00", "846000.00"),
                    ("Wet utilities", "620000.00", "410000.00"),
                    ("Roads and paving", "540000.00", "0"),
                    ("Landscaping and amenities", "180000.00", "0"),
                ]:
                    await P(
                        f"/land-projects/{proj['id']}/budget-lines",
                        {"category": category, "budget_amount": budget, "actual_amount": actual},
                    )
            print("Land         1 project, 5 development budget lines")

        # --- Summary ----------------------------------------------------------
        sat = await G("/reports/satisfaction")
        perf = await G("/reports/campaign-performance")
        print()
        print(f"NPS          {sat['nps']} from {sat['responses']} responses")
        print(f"Campaigns    {len(perf['campaigns'])} tracked")
        print("\nAll seeded. Reload the app.")


if __name__ == "__main__":
    asyncio.run(main())
