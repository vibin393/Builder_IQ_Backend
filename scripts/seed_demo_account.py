"""Create the fixed demo account with a full set of realistic data.

Run the API first, then:  python scripts/seed_demo_account.py

Creates one builder with staff accounts for each role, a trade account for the portal,
two communities, three plans, an option catalog, and several homes at different stages -
sold, under construction, and closed with a warranty claim.

Idempotent enough for demo use: it exits cleanly if the demo tenant already exists.
"""

import asyncio
import os
import sys
from datetime import date, timedelta

import httpx

# Override with CORMAN_API_BASE when the API runs somewhere else.
BASE = os.environ.get("CORMAN_API_BASE", "http://127.0.0.1:8400") + "/api/v1"

# The demo identity. Overridable so a second demo tenant can be seeded alongside
# an existing one - the script refuses to touch a tenant that already exists, and
# re-seeding by deleting the old one would throw away whatever was done in it.
DOMAIN = os.environ.get("CORMAN_DEMO_DOMAIN", "cormanhomes.com")
COMPANY = os.environ.get("CORMAN_DEMO_COMPANY", "Corman Homes Demo")
OWNER_EMAIL = os.environ.get("CORMAN_DEMO_EMAIL", f"demo@{DOMAIN}")
PASSWORD = os.environ.get("CORMAN_DEMO_PASSWORD", "Demo12345!")

STAFF = [
    (f"sales@{DOMAIN}", "Priya Raman", "sales"),
    (f"purchasing@{DOMAIN}", "Marcus Hale", "purchasing"),
    (f"super@{DOMAIN}", "Joe Whitfield", "superintendent"),
]
TRADE_EMAIL = f"trade@sparks.{DOMAIN}"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        reg = await c.post(
            "/auth/register",
            json={
                "company_name": COMPANY,
                "full_name": "Alex Corman",
                "email": OWNER_EMAIL,
                "password": PASSWORD,
            },
        )
        if reg.status_code == 409 or (
            reg.status_code == 500 and "duplicate" in reg.text.lower()
        ):
            print(f"Demo account already exists. Sign in as {OWNER_EMAIL} / {PASSWORD}")
            return
        reg.raise_for_status()

        h = {"Authorization": f"Bearer {reg.json()['access_token']}"}

        async def P(path, payload=None):
            r = await c.post(path, headers=h, json=payload)
            r.raise_for_status()
            return r.json()

        # --- Staff and trade accounts ---
        staff = {}
        for email, name, role in STAFF:
            staff[role] = await P(
                "/users",
                {"email": email, "full_name": name, "password": PASSWORD, "role": role},
            )
        await P(
            "/users",
            {
                "email": TRADE_EMAIL,
                "full_name": "Sparks Electrical Office",
                "password": PASSWORD,
                "role": "trade",
            },
        )

        # --- Cost codes ---
        codes = {}
        for code, name, div in [
            ("03-100", "Concrete and foundations", "03 Concrete"),
            ("06-100", "Framing labour", "06 Wood"),
            ("07-200", "Roofing", "07 Thermal"),
            ("09-300", "Drywall", "09 Finishes"),
            ("09-600", "Flooring", "09 Finishes"),
            ("15-100", "Plumbing", "15 Mechanical"),
            ("16-100", "Electrical", "16 Electrical"),
        ]:
            codes[code] = await P("/cost-codes", {"code": code, "name": name, "division": div})

        # --- Trades ---
        vendors = {}
        for code, name, email in [
            ("SPARK", "Sparks Electrical", TRADE_EMAIL),
            ("FRAME", "Titan Framing", None),
            ("PLUMB", "Clearwater Plumbing", None),
            ("DRY", "Precision Drywall", None),
        ]:
            vendors[code] = await P(
                "/vendors", {"code": code, "name": name, "email": email, "vendor_type": "trade"}
            )

        # --- Communities and lots ---
        oak = await P(
            "/communities",
            {"code": "OAK", "name": "Oak Ridge", "city": "Poole", "county": "Dorset"},
        )
        mill = await P(
            "/communities",
            {"code": "MILL", "name": "Millbrook", "city": "Wimborne", "county": "Dorset"},
        )

        # Real postcode districts for the two towns, so address handling is
        # exercised against plausible data rather than placeholders.
        lots = {}
        for comm, district, numbers in (
            (oak, "BH15", ["12", "14", "47", "48"]),
            (mill, "BH21", ["03", "05"]),
        ):
            for i, n in enumerate(numbers):
                lots[f"{comm['code']}-{n}"] = await P(
                    f"/communities/{comm['id']}/lots",
                    {
                        "lot_number": n,
                        "address": f"{n} {comm['name']} Way",
                        "postcode": f"{district} {i + 1}AA",
                        "premium": "15000.00" if n in ("47", "03") else "0",
                    },
                )

        # --- Plans with standard costs ---
        plan_costs = {
            "P1": [("03-100", "31000"), ("06-100", "44000"), ("07-200", "16000"),
                   ("09-300", "12000"), ("15-100", "17000"), ("16-100", "18000")],
            "P2": [("03-100", "35000"), ("06-100", "52000"), ("07-200", "18500"),
                   ("09-300", "14000"), ("15-100", "19000"), ("16-100", "20000")],
            "P3": [("03-100", "39000"), ("06-100", "58000"), ("07-200", "21000"),
                   ("09-300", "16000"), ("15-100", "21500"), ("16-100", "22000")],
        }
        plans = {}
        for code, name, sqft, beds, price in [
            ("P1", "The Aspen", 1850, 3, "352000.00"),
            ("P2", "The Birch", 2150, 4, "389000.00"),
            ("P3", "The Maple", 2400, 4, "420000.00"),
        ]:
            plans[code] = await P(
                "/plans",
                {
                    "code": code,
                    "name": name,
                    "square_feet": sqft,
                    "bedrooms": beds,
                    "bathrooms": "2.5",
                    "base_price": price,
                },
            )
            for cc, amt in plan_costs[code]:
                await P(
                    f"/plans/{plans[code]['id']}/cost-lines",
                    {"cost_code_id": codes[cc]["id"], "amount": amt},
                )

        # --- Option catalog ---
        options = {}
        for code, name, cat, price, days, lines in [
            ("KIT-GRM", "Gourmet kitchen package", "interior", "8500.00", 3,
             [("16-100", "1200", "SPARK"), ("06-100", "4000", "FRAME")]),
            ("FLR-HW", "Hardwood throughout main floor", "flooring", "6200.00", 2,
             [("09-600", "3900", None)]),
            ("BTH-SPA", "Spa bathroom upgrade", "plumbing", "5400.00", 2,
             [("15-100", "2600", "PLUMB"), ("16-100", "600", "SPARK")]),
            ("ELE-EV", "EV charger rough-in", "electrical", "1800.00", 0,
             [("16-100", "900", "SPARK")]),
            ("EXT-PATIO", "Covered rear patio", "exterior", "11500.00", 5,
             [("03-100", "4200", None), ("06-100", "3100", "FRAME")]),
        ]:
            options[code] = await P(
                "/options",
                {
                    "code": code,
                    "name": name,
                    "category": cat,
                    "price": price,
                    "schedule_days": days,
                },
            )
            for cc, amt, vendor in lines:
                await P(
                    f"/options/{options[code]['id']}/cost-lines",
                    {
                        "cost_code_id": codes[cc]["id"],
                        "amount": amt,
                        "vendor_id": vendors[vendor]["id"] if vendor else None,
                    },
                )

        # --- Schedule template ---
        tmpl = await P("/schedule-templates", {"code": "STD", "name": "Standard 120-day build"})
        for seq, (name, offset, dur, cc, vendor, milestone) in enumerate(
            [
                ("Permits and site prep", 0, 10, None, None, True),
                ("Foundation", 10, 12, "03-100", None, True),
                ("Framing", 22, 20, "06-100", "FRAME", True),
                ("Roofing", 42, 8, "07-200", None, False),
                ("Plumbing rough-in", 50, 6, "15-100", "PLUMB", False),
                ("Electrical rough-in", 56, 6, "16-100", "SPARK", False),
                ("Drywall", 62, 12, "09-300", "DRY", True),
                ("Flooring", 74, 10, "09-600", None, False),
                ("Final finishes", 84, 14, None, None, False),
                ("Final inspection", 98, 3, None, None, True),
            ],
            start=1,
        ):
            await P(
                f"/schedule-templates/{tmpl['id']}/tasks",
                {
                    "sequence": seq,
                    "name": name,
                    "offset_days": offset,
                    "duration_days": dur,
                    "cost_code_id": codes[cc]["id"] if cc else None,
                    "vendor_id": vendors[vendor]["id"] if vendor else None,
                    "is_milestone": milestone,
                },
            )

        # --- Referring agents ---
        # Two with a track record and one brand new, so the performance table has
        # something to rank and the "no rate yet" case is visible.
        agents = {}
        for code, name, brokerage, pct in [
            ("COLE", "Dana Cole", "Cole & Co Residential", "2.50"),
            ("MARSH", "Ben Marsh", "Marsh Property", "2.00"),
            ("NEWAG", "Priti Shah", "Harbour Estates", "2.75"),
        ]:
            agents[code] = await P(
                "/realtors",
                {"code": code, "name": name, "brokerage": brokerage, "commission_percent": pct},
            )

        # --- Automated lead intake ---
        # Four channels, because the interesting states are the ones where a
        # channel misbehaves: one healthy, one that has never been heard from, one
        # holding an unreadable payload.
        channels = {}
        for code, name, kind, lead_source, extras in [
            ("website", "Website enquiry form", "web_form", "website",
             {"default_community_id": oak["id"], "default_campaign": "autumn-2026",
              "honeypot_field": "website_url", "rate_limit_per_minute": 60}),
            ("rightmove", "Rightmove", "rightmove", "portal", {}),
            ("facebook", "Facebook lead ads", "meta_lead_ads", "social", {}),
            ("inbox", "Enquiries inbox", "email", "portal", {}),
        ]:
            channels[code] = await P(
                "/intake/sources",
                {"code": code, "name": name, "kind": kind,
                 "lead_source": lead_source, **extras},
            )

        async def hook(code, body):
            """Post to the public endpoint the way a real channel would."""
            r = await c.post(
                f"/intake/hooks/{code}",
                headers={"X-Intake-Key": channels[code]["key"]},
                json=body,
            )
            r.raise_for_status()
            return r.json()

        # --- Leads ---
        # Deliberately spread across the pipeline, and across the states the list
        # is read for: overdue, due today, unowned, contingent, converted, lost.
        # A seed where every lead looks the same tells you nothing about whether
        # the queues work.
        sales_id = staff["sales"]["id"]
        d = lambda n: (date.today() + timedelta(days=n)).isoformat()  # noqa: E731

        LEADS = [
            # first, last, source, extra fields, how far to take it
            ("Dana", "Reyes", "website", {
                "email": "dana.reyes@example.com", "phone": "01202 555 101",
                "campaign": "spring-parade", "city": "Poole",
                "rating": "warm", "assigned_to_id": sales_id,
                "price_min": "400000", "price_max": "470000", "bedrooms_needed": 4,
                "next_follow_up": d(-4),            # overdue: needs chasing
            }, "contacted"),
            ("Sam", "Okafor", "referral", {
                "email": "sam.okafor@example.com", "mobile": "07700 900 210",
                "referred_by": "Existing owner, Lot 12", "city": "Bournemouth",
                "rating": "hot", "assigned_to_id": sales_id,
                "is_prequalified": True, "lender_name": "Halifax",
                "financing_type": "conventional",
                "price_min": "430000", "price_max": "520000", "bedrooms_needed": 4,
                "next_follow_up": d(0),             # due today
            }, "qualified"),
            ("Lin", "Zhao", "realtor", {
                "email": "lin.zhao@example.com", "phone": "01202 555 118",
                "realtor_id": agents["COLE"]["id"], "city": "Christchurch",
                "rating": "hot", "assigned_to_id": sales_id,
                "is_prequalified": True, "financing_type": "cash",
                "price_max": "600000", "desired_move_in": d(120),
                "next_follow_up": d(2),
            }, "appointment"),
            ("Tom", "Bergeron", "website", {
                "email": "tom.bergeron@example.com",
                "campaign": "spring-parade", "city": "Wimborne",
                "rating": "cold",
                "has_home_to_sell": True,           # contingent: not their timeline
                "next_follow_up": d(-11),           # overdue, and nobody owns it
            }, None),
            ("Nadia", "Haddad", "walk_in", {
                "email": "nadia.haddad@example.com", "mobile": "07700 900 344",
                "preferred_contact": "text", "best_time_to_call": "Evenings",
                "city": "Poole", "assigned_to_id": sales_id,
                "next_follow_up": d(5),
            }, "contacted"),
            ("Priya", "Mistry", "signage", {
                "email": "priya.mistry@example.com", "phone": "01202 555 190",
                "city": "Ferndown",
            }, None),                               # brand new, untouched
            ("Owen", "Fitzgerald", "realtor", {
                "email": "owen.fitz@example.com", "phone": "01202 555 233",
                "realtor_id": agents["COLE"]["id"], "assigned_to_id": sales_id,
                "rating": "hot", "is_prequalified": True, "financing_type": "va",
            }, "convert"),                          # closed - proves attribution
            ("Grace", "Adeyemi", "social", {
                "email": "grace.adeyemi@example.com",
                "realtor_id": agents["MARSH"]["id"], "assigned_to_id": sales_id,
            }, "lost"),
        ]

        for first, last, source, extra, ending in LEADS:
            lead = await P(
                "/leads",
                {
                    "first_name": first, "last_name": last, "source": source,
                    "community_id": oak["id"], **extra,
                },
            )
            lid = lead["id"]

            # A real touch, so "last contacted" and the trail are not empty. The
            # untouched lead is left untouched on purpose: it is what the "nobody
            # has rung these" queue is for.
            if ending is not None:
                await P(
                    f"/leads/{lid}/activities",
                    {
                        "activity_type": "call",
                        "summary": f"Spoke to {first} about the Maple.",
                        "outcome": "reached",
                        "occurred_on": d(-6),
                    },
                )

            if ending in ("qualified", "appointment", "convert", "lost"):
                await P(f"/leads/{lid}/status", {"status": "qualified"})
            if ending == "appointment":
                await P(f"/leads/{lid}/status", {"status": "appointment"})
            if ending == "convert":
                await P(f"/leads/{lid}/convert")
            if ending == "lost":
                await P(
                    f"/leads/{lid}/lost",
                    {"reason": "Beaten on price - 15k under us on the Maple",
                     "lost_to": "Barratt"},
                )

        # --- A few leads that arrived by themselves ---
        # Sent through the public endpoint rather than created directly, so the
        # intake screen shows real events and the round-robin actually ran.
        await hook("website", {
            "name": "Ama Boateng", "email": "ama.boateng@example.com",
            "telephone": "+44 7700 900123", "postcode": "bh151aa",
            "bedrooms": "4", "budget": "$465,000",
            "message": "Interested in the Maple.",
        })
        await hook("facebook", {
            "leadgen_id": "fb-4471", "campaign_name": "Poole launch",
            "field_data": [
                {"name": "full_name", "values": ["Chidi Nwosu"]},
                {"name": "email", "values": ["chidi.nwosu@example.com"]},
                {"name": "phone_number", "values": ["07700900222"]},
            ],
        })
        # A bot: refused outright rather than captured and archived later.
        await hook("website", {
            "name": "Cheap Pills", "email": "spam@example.com",
            "website_url": "http://spam.example.com",
        })
        # And one we cannot read, so the failed queue is not empty on a fresh
        # install. This is the state the screen exists to make visible.
        await hook("website", {"enquirer": "Someone", "note": "field names changed"})

        # --- Homes at different stages ---
        today = date.today()

        # 1. Sold, budgeted, with selections, under construction.
        job1 = await P(
            "/jobs",
            {
                "community_id": oak["id"],
                "lot_id": lots["OAK-47"]["id"],
                "plan_id": plans["P3"]["id"],
                "start_date": (today - timedelta(days=40)).isoformat(),
            },
        )
        await P(
            f"/jobs/{job1['id']}/contract",
            {
                "buyer": {
                    "first_name": "Dana",
                    "last_name": "Reyes",
                    "email": "dana.reyes@example.com",
                },
                "base_price": "420000.00",
                "lot_premium": "15000.00",
                "incentives": "8000.00",
            },
        )
        await P(f"/jobs/{job1['id']}/contract/sign")
        await P(f"/jobs/{job1['id']}/budget")
        await P(
            f"/jobs/{job1['id']}/schedule",
            {"template_id": tmpl["id"], "start_date": (today - timedelta(days=40)).isoformat()},
        )
        for opt in ("KIT-GRM", "FLR-HW", "ELE-EV"):
            sel = await P(f"/jobs/{job1['id']}/selections", {"option_id": options[opt]["id"]})
            await P(f"/jobs/{job1['id']}/selections/{sel['id']}/confirm")
        # Through the lifecycle route, which freezes the projection. Deliberately
        # a few days past it, so the "running late" queue has something in it on a
        # fresh install.
        await P(
            f"/jobs/{job1['id']}/permit/apply",
            {
                "permit_number": "BCP/2026/1180",
                "permit_applied_on": (today - timedelta(days=60)).isoformat(),
            },
        )
        await P(f"/jobs/{job1['id']}/permit/issued", {"number": "BCP/2026/1180"})
        await P(
            f"/jobs/{job1['id']}/start",
            {
                "start_date": (today - timedelta(days=40)).isoformat(),
                "projected_completion": (today - timedelta(days=5)).isoformat(),
            },
        )

        # Progress the schedule and commit some money.
        sched = (await c.get(f"/jobs/{job1['id']}/schedule", headers=h)).json()
        for task in sched[:3]:
            await P(f"/tasks/{task['id']}/complete")

        po1 = await P(
            "/purchase-orders",
            {
                "job_id": job1["id"],
                "vendor_id": vendors["FRAME"]["id"],
                "lines": [{"cost_code_id": codes["06-100"]["id"], "amount": "58000.00"}],
            },
        )
        await P(f"/purchase-orders/{po1['id']}/issue")
        po2 = await P(
            "/purchase-orders",
            {
                "job_id": job1["id"],
                "vendor_id": vendors["SPARK"]["id"],
                "lines": [{"cost_code_id": codes["16-100"]["id"], "amount": "22500.00"}],
            },
        )
        await P(f"/purchase-orders/{po2['id']}/issue")

        inv = await P(
            "/invoices",
            {
                "invoice_number": "TF-88120",
                "purchase_order_id": po1["id"],
                "lien_waiver_received": True,
                "lines": [{"cost_code_id": codes["06-100"]["id"], "amount": "58000.00"}],
            },
        )
        await P(f"/invoices/{inv['id']}/approve")

        # A builder-funded change order, so margin impact is visible.
        co = await P(
            f"/jobs/{job1['id']}/change-orders",
            {
                "description": "Rock encountered in footings, extra excavation",
                "reason": "Unforeseen site condition",
                "responsible_party": "builder",
                "price_delta": "0",
                "schedule_days": 4,
                "cost_lines": [{"cost_code_id": codes["03-100"]["id"], "amount": "5200.00"}],
            },
        )
        await P(f"/jobs/{job1['id']}/change-orders/{co['id']}/approve")

        # 2. Sold and budgeted, not yet started.
        job2 = await P(
            "/jobs",
            {
                "community_id": oak["id"],
                "lot_id": lots["OAK-12"]["id"],
                "plan_id": plans["P2"]["id"],
            },
        )
        await P(
            f"/jobs/{job2['id']}/contract",
            {
                "buyer": {"first_name": "Sam", "last_name": "Okafor"},
                "base_price": "389000.00",
                "incentives": "5000.00",
            },
        )
        await P(f"/jobs/{job2['id']}/budget")
        sel = await P(f"/jobs/{job2['id']}/selections", {"option_id": options["EXT-PATIO"]["id"]})
        await P(f"/jobs/{job2['id']}/selections/{sel['id']}/confirm")

        # 3. A closed home with a warranty claim in progress.
        job3 = await P(
            "/jobs",
            {
                "community_id": mill["id"],
                "lot_id": lots["MILL-03"]["id"],
                "plan_id": plans["P1"]["id"],
                "start_date": (today - timedelta(days=210)).isoformat(),
            },
        )
        await P(
            f"/jobs/{job3['id']}/contract",
            {
                "buyer": {"first_name": "Lin", "last_name": "Zhao"},
                "base_price": "352000.00",
                "lot_premium": "15000.00",
            },
        )
        await P(f"/jobs/{job3['id']}/contract/sign")
        await P(f"/jobs/{job3['id']}/budget")
        # A finished home, walked through properly so its cycle time is real
        # rather than typed in.
        await P(
            f"/jobs/{job3['id']}/start",
            {
                "start_date": (today - timedelta(days=210)).isoformat(),
                "projected_completion": (today - timedelta(days=100)).isoformat(),
            },
        )
        await P(
            f"/jobs/{job3['id']}/complete",
            {"completed_on": (today - timedelta(days=95)).isoformat()},
        )
        await P(
            f"/jobs/{job3['id']}/close",
            {"closing_date": (today - timedelta(days=90)).isoformat(), "warranty_months": 24},
        )
        await P(f"/jobs/{job3['id']}/warranty")
        wr = await P(
            "/warranty/requests",
            {
                "job_id": job3["id"],
                "title": "Cracked tile in master bathroom",
                "description": "Two tiles by the shower have hairline cracks.",
                "location": "Master bathroom",
                "priority": "normal",
            },
        )
        await P(
            f"/warranty/requests/{wr['id']}/service-orders",
            {
                "vendor_id": vendors["PLUMB"]["id"],
                "instructions": "Inspect and replace cracked tiles.",
                "scheduled_date": (today + timedelta(days=3)).isoformat(),
            },
        )

        # 4. Unsold inventory.
        await P(
            "/jobs",
            {
                "community_id": oak["id"],
                "lot_id": lots["OAK-14"]["id"],
                "plan_id": plans["P1"]["id"],
            },
        )

        print("Demo data seeded.\n")

        # --- Two homes in the states nobody demos but everybody has ---
        # A build that has stopped, and a sale that fell through. Without these
        # the "Stopped" tile and the cancelled state are permanently zero, and the
        # plot-freeing behaviour is invisible.
        held_lot = await P(
            f"/communities/{oak['id']}/lots",
            {"lot_number": "52", "address": "52 Oak Ridge Way", "postcode": "BH15 5AA"},
        )
        job5 = await P(
            "/jobs",
            {
                "community_id": oak["id"],
                "lot_id": held_lot["id"],
                "plan_id": plans["P1"]["id"],
                "elevation": "Brick",
            },
        )
        await P(
            f"/jobs/{job5['id']}/contract",
            {
                "buyer": {
                    "first_name": "Owen",
                    "last_name": "Fitzgerald",
                    "email": f"owen.fitzgerald@{DOMAIN}",
                },
                "base_price": "398000.00",
            },
        )
        await P(f"/jobs/{job5['id']}/contract/sign")
        await P(
            f"/jobs/{job5['id']}/start",
            {
                "start_date": (today - timedelta(days=25)).isoformat(),
                "projected_completion": (today + timedelta(days=70)).isoformat(),
            },
        )
        await P(
            f"/jobs/{job5['id']}/hold",
            {"reason": "inspection", "note": "Waiting on building control for the raft"},
        )

        cancelled_lot = await P(
            f"/communities/{oak['id']}/lots",
            {"lot_number": "54", "address": "54 Oak Ridge Way", "postcode": "BH15 6AA"},
        )
        job6 = await P(
            "/jobs",
            {
                "community_id": oak["id"],
                "lot_id": cancelled_lot["id"],
                "plan_id": plans["P3"]["id"],
            },
        )
        await P(
            f"/jobs/{job6['id']}/cancel",
            {"reason": "finance_fell_through", "note": "Buyer's mortgage offer withdrawn"},
        )
        # And the plot is free again, which a plain unique constraint on the lot
        # would have made impossible.
        await P(
            "/jobs",
            {
                "community_id": oak["id"],
                "lot_id": cancelled_lot["id"],
                "plan_id": plans["P1"]["id"],
                "elevation": "Render",
            },
        )


        print("  Sign in at http://localhost:4300/login")
        print(f"  Owner        {OWNER_EMAIL:<32} {PASSWORD}")
        for email, _name, role in STAFF:
            print(f"  {role.capitalize():<12} {email:<32} {PASSWORD}")
        print(f"  Trade        {TRADE_EMAIL:<32} {PASSWORD}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.HTTPStatusError as exc:
        print(
            f"Failed: {exc.request.method} {exc.request.url.path} -> "
            f"{exc.response.status_code} {exc.response.text[:400]}",
            file=sys.stderr,
        )
        sys.exit(1)
