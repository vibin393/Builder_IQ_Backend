"""Add demo data for the Procore-derived modules to the existing demo tenant.

Run the API first, then:  python scripts/seed_procore.py

Covers prequalification and compliance documents, RFIs, submittals, drawing sets and
transmittals, specifications, observations, incidents, action plans, meetings,
correspondence and timecards.

The data is deliberately not all clean: one trade has a lapsed insurance certificate
and one is rejected, so the purchase-order gate has something real to refuse; there
are overdue RFIs and an observation past its due date, so the chase lists are not
empty. A demo where everything is green shows you nothing.

Safe to re-run: each section skips what already exists.
"""

import asyncio
import os
import sys
from datetime import date, timedelta

import httpx

# Override with CORMAN_API_BASE when the API runs somewhere else.
BASE = os.environ.get("CORMAN_API_BASE", "http://127.0.0.1:8400") + "/api/v1"
# Overridable, like the other seeds. Hardcoded, every run signed into the first
# demo tenant whatever account was asked for - and because each section skips what
# already exists, it reported success while creating nothing in the tenant
# somebody actually meant.
DOMAIN = os.environ.get("CORMAN_DEMO_DOMAIN", "cormanhomes.com")
OWNER_EMAIL = os.environ.get("CORMAN_DEMO_EMAIL", f"demo@{DOMAIN}")
PASSWORD = os.environ.get("CORMAN_DEMO_PASSWORD", "Demo12345!")
# A second pair of eyes, for the checklist steps that need witnessing. The rule is
# that whoever did the work cannot sign it off, so seeding a witnessed step needs
# two accounts.
WITNESS_EMAIL = os.environ.get("CORMAN_DEMO_WITNESS", f"super@{DOMAIN}")


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

        witness = await c.post(
            "/auth/login", json={"email": WITNESS_EMAIL, "password": PASSWORD}
        )
        h2 = (
            {"Authorization": f"Bearer {witness.json()['access_token']}"}
            if witness.status_code == 200
            else None
        )

        async def P(path, payload=None, ok=(200, 201)):
            r = await c.post(path, headers=h, json=payload)
            if r.status_code not in ok:
                print(f"  ! {path} -> {r.status_code} {r.text[:160]}", file=sys.stderr)
                return None
            return r.json()

        async def G(path):
            """GET a list.

            Paged endpoints return {items, total}; unpaged ones a bare list.
            Normalising here beats remembering which is which - and without it
            every `if not await G(...)` guard below reads a dict, which is always
            truthy, so the section it guards is skipped forever and the script
            still says it seeded.
            """
            r = await c.get(path, headers=h)
            r.raise_for_status()
            body = r.json()
            return body["items"] if isinstance(body, dict) and "items" in body else body

        codes = {x["code"]: x for x in await G("/cost-codes")}
        vendors = {x["name"]: x for x in await G("/vendors")}
        plans = {x["code"]: x for x in await G("/plans")}
        jobs = await G("/jobs")
        if not jobs:
            print("No homes found. Run scripts/seed_demo_account.py first.", file=sys.stderr)
            sys.exit(1)

        building = next((j for j in jobs if j["status"] == "under_construction"), jobs[0])
        today = date.today()
        d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731

        def vendor(name_fragment):
            for name, v in vendors.items():
                if name_fragment.lower() in name.lower():
                    return v
            return next(iter(vendors.values()))

        framing = vendor("Titan")
        electrical = vendor("Sparks")
        plumbing = vendor("Clearwater")
        drywall = vendor("Precision")

        # --- Prequalification -------------------------------------------------
        existing_pq = {p["vendor_id"] for p in await G("/prequalification")}
        if len(existing_pq) < 3:
            print("prequalification")

            # Fully compliant: approved, certificates current.
            pq = await P(
                "/prequalification",
                {
                    "vendor_id": framing["id"],
                    "single_job_limit": "300000",
                    "aggregate_limit": "900000",
                    "review_due": d(240),
                    "notes": "Annual review clean. Three years without a lost-time incident.",
                },
            )
            if pq:
                await P(
                    f"/prequalification/{pq['id']}/documents",
                    {
                        "document_type": "public_liability",
                        "reference": "GL-884213",
                        "issuer": "Northbridge Insurance",
                        "coverage_amount": "2000000",
                        "issued_on": d(-300),
                        "expires_on": d(65),
                    },
                )
                await P(
                    f"/prequalification/{pq['id']}/documents",
                    {
                        "document_type": "employers_liability",
                        "reference": "WC-20551",
                        "issuer": "State Fund",
                        "coverage_amount": "1000000",
                        "issued_on": d(-300),
                        "expires_on": d(65),
                    },
                )
                await P(f"/prequalification/{pq['id']}/approve")

            # Approved, but the liability certificate lapses in under a month.
            # This is what the chase list is for.
            pq = await P(
                "/prequalification",
                {
                    "vendor_id": electrical["id"],
                    "single_job_limit": "150000",
                    "review_due": d(120),
                },
            )
            if pq:
                await P(
                    f"/prequalification/{pq['id']}/documents",
                    {
                        "document_type": "public_liability",
                        "reference": "GL-119002",
                        "issuer": "Aviva",
                        "coverage_amount": "2000000",
                        "issued_on": d(-350),
                        "expires_on": d(18),
                    },
                )
                await P(
                    f"/prequalification/{pq['id']}/documents",
                    {
                        "document_type": "trade_licence",
                        "reference": "EL-4471",
                        "issuer": "State Board",
                        "issued_on": d(-700),
                        "expires_on": d(210),
                    },
                )
                await P(f"/prequalification/{pq['id']}/approve")

            # Approved, and then a certificate lapsed underneath it. This is the
            # case that actually catches builders out, and the one the gate exists
            # for: the trade was cleared months ago and nobody re-checked.
            #
            # It has to be seeded in this order because `approve` refuses to clear
            # a trade whose paperwork has already lapsed - so the lapse is added
            # after approval, exactly as it happens in real life.
            pq = await P(
                "/prequalification",
                {
                    "vendor_id": drywall["id"],
                    "single_job_limit": "90000",
                    "review_due": d(150),
                },
            )
            if pq:
                await P(
                    f"/prequalification/{pq['id']}/documents",
                    {
                        "document_type": "public_liability",
                        "reference": "GL-77310",
                        "issuer": "Aviva",
                        "coverage_amount": "2000000",
                        "issued_on": d(-400),
                        "expires_on": d(180),
                    },
                )
                await P(f"/prequalification/{pq['id']}/approve")
                await P(
                    f"/prequalification/{pq['id']}/documents",
                    {
                        "document_type": "employers_liability",
                        "reference": "WC-77310",
                        "issuer": "State Fund",
                        "coverage_amount": "1000000",
                        "issued_on": d(-400),
                        "expires_on": d(-12),
                    },
                )

            # One outright rejection, so the refusal path is visible too.
            pq = await P(
                "/prequalification",
                {
                    "vendor_id": plumbing["id"],
                    "single_job_limit": "120000",
                    "review_due": d(90),
                },
            )
            if pq:
                await P(
                    f"/prequalification/{pq['id']}/reject",
                    ok=(200, 201),
                )

        # --- Drawing sets and specifications ---------------------------------
        if not await G("/drawing-sets"):
            print("drawing sets")
            plan_id = plans.get("P3", next(iter(plans.values())))["id"]
            s = await P(
                "/drawing-sets",
                {
                    "name": "The Maple — construction issue",
                    "plan_id": plan_id,
                    "notes": "Issued for construction. Trades price from this revision.",
                },
            )
            if s:
                for sheet, title, disc in [
                    ("A-101", "Ground floor plan", "architectural"),
                    ("A-102", "First floor plan", "architectural"),
                    ("A-201", "Elevations", "architectural"),
                    ("S-101", "Foundation plan", "structural"),
                    ("E-101", "Power and lighting", "electrical"),
                    ("P-101", "Plumbing rough-in", "plumbing"),
                ]:
                    await P(
                        f"/drawing-sets/{s['id']}/drawings",
                        {"sheet_number": sheet, "title": title, "discipline": disc},
                    )
                await P(f"/drawing-sets/{s['id']}/issue")

                # A revision, so the supersession chain is visible in the UI.
                rev = await P(f"/drawing-sets/{s['id']}/revise")
                if rev:
                    await P(f"/drawing-sets/{rev['id']}/issue")
                    t = await P(
                        "/transmittals",
                        {
                            "subject": "Revised structural drawings — beam at grid C",
                            "message": (
                                "Revision 2 issued. The beam at grid C has changed section. "
                                "Confirm receipt before continuing framing."
                            ),
                            "drawing_set_id": rev["id"],
                            "job_id": building["id"],
                            "response_due": d(3),
                            "vendor_ids": [framing["id"], electrical["id"], plumbing["id"]],
                        },
                    )
                    # One trade confirms; two are left outstanding on purpose.
                    if t:
                        await P(f"/transmittals/{t['id']}/acknowledge/{framing['id']}")

        if not await G("/specifications"):
            print("specifications")
            for section, title, code, body in [
                ("03 30 00", "Cast-in-place concrete", "03-100",
                 "28-day strength 25 MPa minimum. Slump 100mm +/- 25mm."),
                ("06 10 00", "Rough carpentry", "06-100",
                 "Kiln-dried SPF No.2 or better. Moisture content below 19% at close-in."),
                ("09 30 00", "Tiling", "09-300",
                 "Buyer-selected tile per option catalog. Uncoupling membrane to all wet areas."),
                ("22 40 00", "Plumbing fixtures", "15-100",
                 "Fixtures per buyer selection. Submittal required before order."),
            ]:
                cc = codes.get(code)
                await P(
                    "/specifications",
                    {
                        "section": section,
                        "title": title,
                        "body": body,
                        "cost_code_id": cc["id"] if cc else None,
                    },
                )

        # --- RFIs -------------------------------------------------------------
        if not await G("/rfis"):
            print("rfis")
            await P(
                "/rfis",
                {
                    "job_id": building["id"],
                    "subject": "Beam bearing at grid C",
                    "question": (
                        "Structural shows a W200x27 bearing on the block wall at grid C, but the "
                        "architectural plan puts a window head at the same elevation. "
                        "Which governs?"
                    ),
                    "priority": "urgent",
                    "assigned_vendor_id": framing["id"],
                    "cost_code_id": codes.get("06-100", {}).get("id"),
                    "due_on": d(-4),          # already overdue, on purpose
                    "is_blocking": True,
                },
            )
            await P(
                "/rfis",
                {
                    "job_id": building["id"],
                    "subject": "Panel location, garage wall",
                    "question": "Can the panel move 600mm left to clear the garage door track?",
                    "priority": "normal",
                    "assigned_vendor_id": electrical["id"],
                    "cost_code_id": codes.get("16-100", {}).get("id"),
                    "due_on": d(5),
                },
            )
            answered = await P(
                "/rfis",
                {
                    "job_id": building["id"],
                    "subject": "Tile substrate in ensuite",
                    "question": "Is cement board required behind the freestanding tub?",
                    "priority": "low",
                    "due_on": d(9),
                },
            )
            if answered:
                await P(
                    f"/rfis/{answered['id']}/answer",
                    {
                        "answer": (
                            "Yes. Cement board and membrane to full height behind the tub, per "
                            "spec 09 30 00."
                        ),
                        "cost_impact": False,
                        "schedule_impact_days": 0,
                    },
                )

        # --- Submittals -------------------------------------------------------
        if not await G("/submittals"):
            print("submittals")
            options = await G("/options")
            tile = next((o for o in options if "tile" in o["name"].lower()), None)

            await P(
                "/submittals",
                {
                    "title": "Kitchen tap — brushed nickel",
                    "job_id": building["id"],
                    "manufacturer": "Delta",
                    "model_number": "9159-DST",
                    "spec_section": "22 40 00",
                    "vendor_id": plumbing["id"],
                    "cost_code_id": codes.get("15-100", {}).get("id"),
                    "option_id": tile["id"] if tile else None,
                    "due_on": d(6),
                },
            )
            revise = await P(
                "/submittals",
                {
                    "title": "Ensuite floor tile",
                    "job_id": building["id"],
                    "manufacturer": "Olympia",
                    "model_number": "Carrara 300x600",
                    "spec_section": "09 30 00",
                    "due_on": d(-2),
                },
            )
            if revise:
                await P(
                    f"/submittals/{revise['id']}/review",
                    {
                        "status": "revise_and_resubmit",
                        "review_notes": (
                            "Submitted tile is 300x600; the buyer selected 600x600. Resubmit with "
                            "the selected size."
                        ),
                    },
                )

        # --- Observations and incidents --------------------------------------
        # Only the hand-raised ones count for this guard. A failed inspection now
        # raises an observation by itself, so an earlier seed leaves one here -
        # and "are there any observations" then answers yes and skips the block,
        # leaving the screen with one row that nothing in this script created.
        existing = await G("/observations?size=100")
        if not [o for o in existing if not o.get("inspection_id")]:
            print("observations")
            inspections = await G(f"/field/inspections?job_id={building['id']}")
            failed = next((i for i in inspections if i.get("result") == "failed"), None)

            await P(
                "/observations",
                {
                    "job_id": building["id"],
                    "title": "Unguarded stair opening, first floor",
                    "description": (
                        "Temporary guardrail removed for material delivery and not replaced."
                    ),
                    "location": "First floor landing",
                    "observation_type": "safety",
                    "severity": "critical",
                    "assigned_vendor_id": framing["id"],
                    "due_on": d(-1),          # overdue, on purpose
                    "inspection_id": failed["id"] if failed else None,
                },
            )
            screw_pops = await P(
                "/observations",
                {
                    "job_id": building["id"],
                    "title": "Drywall screw pops, main bedroom",
                    "description": "Screw pops along the ceiling perimeter, likely truss uplift.",
                    "location": "Main bedroom",
                    "observation_type": "quality",
                    "severity": "medium",
                    "assigned_vendor_id": drywall["id"],
                    "due_on": d(7),
                },
            )
            # Claimed but not yet accepted, so the review step is visible rather
            # than only described. A fix nobody has looked at is not a closed one.
            if screw_pops:
                await P(
                    f"/observations/{screw_pops['id']}/submit",
                    {"resolution": "Pops refixed and skimmed; awaiting a look before decorating."},
                    ok=(200,),
                )

            # One that went round the loop: claimed, rejected, claimed again,
            # accepted. Without an example of it the not-accepted state reads as
            # theoretical.
            seal = await P(
                "/observations",
                {
                    "job_id": building["id"],
                    "title": "Soil stack not sealed at the first floor penetration",
                    "location": "Airing cupboard",
                    "observation_type": "safety",
                    "severity": "high",
                    "assigned_vendor_id": plumbing["id"],
                },
            )
            if seal:
                await P(
                    f"/observations/{seal['id']}/submit",
                    {"resolution": "Foamed around the stack."}, ok=(200,),
                )
                await P(
                    f"/observations/{seal['id']}/reject",
                    {"reason": "Expanding foam is not a fire stop. Needs an intumescent collar."},
                    ok=(200,),
                )
                await P(
                    f"/observations/{seal['id']}/submit",
                    {"resolution": "Intumescent collar fitted and the foam cut back."},
                    ok=(200,),
                )
                await P(f"/observations/{seal['id']}/close", ok=(200,))

        if not await G("/incidents"):
            print("incidents")
            await P(
                "/incidents",
                {
                    "job_id": building["id"],
                    "incident_type": "near_miss",
                    "title": "Sheet material blown from second-floor deck",
                    "occurred_on": d(-6),
                    "severity": "high",
                    "description": (
                        "Sheathing left unsecured overnight; wind lifted two sheets onto the "
                        "driveway. Nobody was on site."
                    ),
                    "location": "Rear elevation",
                    "vendor_id": framing["id"],
                    "immediate_action": (
                        "All loose material strapped. Toolbox talk held next morning."
                    ),
                },
            )
            # Twenty days old, reportable, and nobody has told the HSE. The
            # deadline was ten days, so this is the state the screen exists to
            # make impossible to miss.
            cut = await P(
                "/incidents",
                {
                    "job_id": building["id"],
                    "incident_type": "injury",
                    "title": "Laceration to forearm from utility knife",
                    "occurred_on": d(-20),
                    "severity": "medium",
                    "person_involved": "R. Mbeki",
                    "vendor_id": drywall["id"],
                    "medical_treatment": True,
                    "immediate_action": "First aid on site, driven to urgent care. Four stitches.",
                },
            )
            if cut:
                await P(
                    f"/incidents/{cut['id']}/root-cause",
                    {
                        "root_cause": (
                            "Fixed-blade knives in use across the drywall crew. Replaced with "
                            "self-retracting knives and a toolbox talk held."
                        )
                    },
                    ok=(200,),
                )

            # And one handled properly end to end, so the finished state is on
            # screen beside the unfinished one.
            fall = await P(
                "/incidents",
                {
                    "job_id": building["id"],
                    "incident_type": "injury",
                    "title": "Fall from a step-up working platform",
                    "occurred_on": d(-40),
                    "severity": "high",
                    "person_involved": "T. Okafor",
                    "vendor_id": framing["id"],
                    "lost_time": True,
                    "immediate_action": "Ambulance called. Platform removed from site the same day.",
                },
            )
            if fall:
                await P(
                    f"/incidents/{fall['id']}/root-cause",
                    {"root_cause": "Platform used on uneven ground with no outriggers fitted."},
                    ok=(200,),
                )
                await P(
                    f"/incidents/{fall['id']}/hse-report",
                    {"reported_on": d(-32), "reference": "RIDDOR-2026-114892"},
                    ok=(200,),
                )
                await P(f"/incidents/{fall['id']}/close", ok=(200,))

        # --- Action plans -----------------------------------------------------
        if not await G("/action-plans"):
            print("action plans")
            plan = await P(
                "/action-plans",
                {
                    "name": "Pre-drywall walk",
                    "job_id": building["id"],
                    "description": "Run before any board goes up. Every step signed off.",
                    "due_on": d(4),
                },
            )
            if plan:
                steps = [
                    ("Framing inspection passed and posted", True, framing["id"]),
                    ("Electrical rough-in inspected", True, electrical["id"]),
                    ("Plumbing rough-in pressure tested", True, plumbing["id"]),
                    ("Fire blocking complete at all penetrations", True, framing["id"]),
                    ("Insulation and vapour barrier complete", False, None),
                    ("Buyer walk booked and confirmed", False, None),
                ]
                for i, (desc, signoff, vid) in enumerate(steps):
                    await P(
                        f"/action-plans/{plan['id']}/items",
                        {
                            "description": desc,
                            "requires_signoff": signoff,
                            "vendor_id": vid,
                            "due_on": d(2 + i),
                        },
                    )
                # Walk the first three, in order, so the progress meter is
                # partway along - and witness two of them, so a plan that is
                # waiting on a second pair of eyes is on screen rather than only
                # described.
                fresh = await G("/action-plans")
                mine = next((p for p in fresh if p["id"] == plan["id"]), None)
                done = sorted((mine or {}).get("items", []), key=lambda i: i["sequence"])[:3]
                for item in done:
                    await P(
                        f"/action-plans/{plan['id']}/items/{item['id']}/complete", ok=(200,)
                    )
                if h2 is not None:
                    for item in done[:2]:
                        r = await c.post(
                            f"/action-plans/{plan['id']}/items/{item['id']}/sign-off",
                            headers=h2,
                        )
                        if r.status_code != 200:
                            print(f"  ! sign-off -> {r.status_code} {r.text[:120]}")

            # A template with steps on it. Without them it cannot be started, and
            # an empty template on the screen reads as something half-built.
            handover = await P(
                "/action-plans",
                {
                    "name": "Handover checklist",
                    "description": "Standard sequence for every closing.",
                    "is_template": True,
                },
            )
            if handover:
                for desc, signoff in [
                    ("Final inspection passed", True),
                    ("Snagging list cleared and signed off", True),
                    ("Meters read and photographed", False),
                    ("Building control completion certificate received", True),
                    ("Warranty documentation pack assembled", False),
                    ("Keys, manuals and alarm codes handed over", True),
                ]:
                    await P(
                        f"/action-plans/{handover['id']}/items",
                        {"description": desc, "requires_signoff": signoff},
                        ok=(200,),
                    )

        # --- Meetings and correspondence -------------------------------------
        if not await G("/meetings"):
            print("meetings")
            m = await P(
                "/meetings",
                {
                    "title": "Pre-construction meeting — Lot 14",
                    "job_id": building["id"],
                    "meeting_date": d(-9),
                    "location": "Site office",
                    "attendees": "A. Corman, J. Whitfield, M. Hale, Titan Framing",
                },
            )
            if m:
                await P(
                    f"/meetings/{m['id']}/items",
                    {
                        "topic": "Site access and deliveries",
                        "discussion": "Deliveries before 09:00 only. Neighbour complaint on file.",
                        "is_action": False,
                    },
                )
                await P(
                    f"/meetings/{m['id']}/items",
                    {
                        "topic": "Confirm beam detail at grid C",
                        "discussion": "Raised as an RFI. Blocking framing above ground floor.",
                        "is_action": True,
                        "due_on": d(-2),
                    },
                )
                await P(
                    f"/meetings/{m['id']}/items",
                    {
                        "topic": "Insurance certificates for all trades",
                        "discussion": "Two certificates lapse within the month.",
                        "is_action": True,
                        "due_on": d(5),
                    },
                )
                await P(f"/meetings/{m['id']}/hold")

            await P(
                "/meetings",
                {
                    "title": "Weekly trade coordination",
                    "job_id": building["id"],
                    "meeting_date": d(3),
                    "location": "Site office",
                },
            )

        if not await G("/correspondence"):
            print("correspondence")
            await P(
                "/correspondence",
                {
                    "subject": "Notice — insurance certificate lapsing",
                    "body": (
                        "Your general liability certificate expires within 30 days. Provide a "
                        "renewed certificate before further purchase orders can be issued."
                    ),
                    "kind": "notice",
                    "direction": "outbound",
                    "vendor_id": electrical["id"],
                    "occurred_on": d(-3),
                },
            )
            await P(
                "/correspondence",
                {
                    "subject": "Site instruction — revised beam detail",
                    "body": "Proceed with the revised detail per drawing set revision 2.",
                    "kind": "site_instruction",
                    "direction": "outbound",
                    "job_id": building["id"],
                    "vendor_id": framing["id"],
                    "occurred_on": d(-1),
                },
            )

        # --- Timecards --------------------------------------------------------
        if not await G("/timecards"):
            print("timecards")

            # Each trade gets a standard rate, so the timecards below do not carry
            # one and the fallback is exercised rather than merely described.
            for vid, rate in [
                (framing["id"], "48.00"),
                (electrical["id"], "52.00"),
                (plumbing["id"], "50.00"),
                (drywall["id"], "41.00"),
            ]:
                r = await c.patch(f"/vendors/{vid}", headers=h, json={"labour_rate": rate})
                if r.status_code != 200:
                    print(f"  ! vendor rate -> {r.status_code} {r.text[:120]}", file=sys.stderr)

            crew = [
                # name, cost code, trade, rate typed on the entry (None = use the
                # trade's standard rate)
                ("J. Whitfield", "06-100", framing["id"], None),
                ("T. Okafor", "06-100", framing["id"], "44.50"),   # a lower-graded hand
                ("L. Novak", "16-100", electrical["id"], None),
                ("S. Ahmed", "15-100", plumbing["id"], None),
                ("R. Mbeki", "09-300", drywall["id"], None),
            ]
            booked = []
            for offset in range(10):
                for name, code, vid, rate in crew:
                    cc = codes.get(code)
                    if not cc:
                        continue
                    overtime = "2.0" if offset % 4 == 0 and name == "J. Whitfield" else "0"
                    card = await P(
                        "/timecards",
                        {
                            "job_id": building["id"],
                            "worker_name": name,
                            "hours": "8",
                            "overtime_hours": overtime,
                            "worked_on": d(-offset - 1),
                            "cost_code_id": cc["id"],
                            "vendor_id": vid,
                            **({"hourly_rate": rate} if rate else {}),
                        },
                    )
                    if card:
                        booked.append((offset, card["id"]))

            # An agency hand nobody set a rate for. One is enough to make the
            # point on screen: the hours are real, the cost is unknown, and the
            # entry cannot be approved until somebody says what it cost.
            await P(
                "/timecards",
                {
                    "job_id": building["id"],
                    "worker_name": "Agency labourer (name not recorded)",
                    "hours": "8",
                    "worked_on": d(-2),
                    "cost_code_id": (codes.get("06-100") or {}).get("id"),
                    "notes": "Sent by the agency at short notice; rate to be confirmed.",
                },
            )

            # Last week's are signed off; this week's are not. A screen where
            # everything is approved never shows the approval step, and a screen
            # where nothing is never shows what approved looks like.
            older = [cid for off, cid in booked if off >= 5]
            if older:
                await P("/timecards/approve", {"timecard_ids": older}, ok=(200,))

            # The targets the hours are measured against. Framing is deliberately
            # over: a screen where everything is inside its budget demonstrates
            # nothing, and over-budget is the state somebody opens this page to
            # find.
            targets = [
                ("06-100", "160"),     # ~176 booked, so over
                ("16-100", "120"),
                ("15-100", "100"),
                ("09-300", "110"),
            ]
            rows = [
                {"cost_code_id": codes[code]["id"], "budgeted_hours": hrs}
                for code, hrs in targets
                if code in codes
            ]
            if rows:
                r = await c.put(
                    f"/timecards/budgeted-hours/{building['id']}", headers=h, json=rows
                )
                if r.status_code != 200:
                    print(
                        f"  ! budgeted hours -> {r.status_code} {r.text[:160]}", file=sys.stderr
                    )

        # --- Summary ----------------------------------------------------------
        print("\nseeded:")
        for label, path in [
            ("prequalifications", "/prequalification"),
            ("rfis", "/rfis"),
            ("submittals", "/submittals"),
            ("drawing sets", "/drawing-sets"),
            ("specifications", "/specifications"),
            ("transmittals", "/transmittals"),
            ("observations", "/observations"),
            ("incidents", "/incidents"),
            ("action plans", "/action-plans"),
            ("meetings", "/meetings"),
            ("correspondence", "/correspondence"),
            ("timecards", "/timecards"),
        ]:
            print(f"  {label:18} {len(await G(path))}")


if __name__ == "__main__":
    asyncio.run(main())
