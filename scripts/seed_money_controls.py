"""Demo data for retainage, progress billing, forecasting and inspection checklists.

Run the API first, then:  python scripts/seed_money_controls.py

Creates a trade commitment with retainage held, a part-billed purchase order, a
change order raised but deliberately left unapproved so the forecast warning has
something to fire on, and a framing inspection with a part-completed checklist.

Safe to re-run: each section skips what already exists.
"""

import asyncio
import os
import sys
from datetime import date, timedelta

import httpx

BASE = os.environ.get("CORMAN_API_BASE", "http://127.0.0.1:8400") + "/api/v1"
OWNER_EMAIL = "demo@cormanhomes.com"
PASSWORD = "Demo12345!"


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

        async def P(path, payload=None, ok=(200, 201)):
            r = await c.post(path, headers=h, json=payload)
            if r.status_code not in ok:
                print(f"  ! {path} -> {r.status_code} {r.text[:200]}", file=sys.stderr)
                return None
            return r.json()

        async def G(path):
            r = await c.get(path, headers=h)
            r.raise_for_status()
            return r.json()

        vendors_raw = await G("/vendors")
        vendors = {x["name"]: x for x in (vendors_raw.get("items", []) if isinstance(vendors_raw, dict) else vendors_raw)}
        jobs_raw = await G("/jobs")
        jobs = jobs_raw.get("items", []) if isinstance(jobs_raw, dict) else jobs_raw
        if not jobs:
            print("No homes found. Run scripts/seed_demo_account.py first.", file=sys.stderr)
            sys.exit(1)

        building = next((j for j in jobs if j["status"] == "under_construction"), jobs[0])
        today = date.today()
        d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731

        def vendor(fragment):
            for name, v in vendors.items():
                if fragment.lower() in name.lower():
                    return v
            return next(iter(vendors.values()))

        framing = vendor("Titan")

        # Size the commitment to the budget that is actually left, rather than a
        # fixed figure. The over-budget guard is real and will refuse a PO that
        # does not fit - and a demo should show the guards passing, not tripping.
        # Pick whichever home actually has budget headroom to commit against;
        # the first under-construction home may have none.
        candidates = [building] + [j for j in jobs if j["id"] != building["id"]]
        cost_now = None
        for cand in candidates:
            c_cost = await G(f"/jobs/{cand['id']}/cost")
            if c_cost.get("lines") and any(float(l["uncommitted"]) > 0 for l in c_cost["lines"]):
                building = cand
                cost_now = c_cost
                break
        if cost_now is None:
            print("No home has uncommitted budget headroom; skipping money-controls seed.", file=sys.stderr)
            return
        roomiest = max(cost_now["lines"], key=lambda line: float(line["uncommitted"]))
        headroom = float(roomiest["uncommitted"])
        commitment = round(headroom * 0.8, 2)
        first_claim = round(commitment * 0.45, 2)
        second_claim = round(commitment * 0.35, 2)
        work_code = {"id": roomiest["cost_code_id"], "code": roomiest["code"]}

        # --- retainage -------------------------------------------------------
        existing = await G("/invoices")
        if not any(float(i.get("retainage_percent", 0)) > 0 for i in existing):
            print(f"retainage (on {work_code['code']}, {commitment:.2f} of {headroom:.2f} left)")
            po = await P(
                "/purchase-orders",
                {
                    "job_id": building["id"],
                    "vendor_id": framing["id"],
                    "retainage_percent": "10",
                    "notes": "Trade labour. 10% held until the scope is signed off.",
                    "lines": [
                        {
                            "cost_code_id": work_code["id"],
                            "amount": f"{commitment:.2f}",
                            "description": "Scope to completion",
                        }
                    ],
                },
            )
            if po:
                await P(f"/purchase-orders/{po['id']}/issue", None, ok=(200,))
                po_line = po["lines"][0]

                # Billed in two stages against the same PO line: the schedule of
                # values in practice, rather than one all-or-nothing invoice.
                first = await P(
                    "/invoices",
                    {
                        "invoice_number": "INV-2041",
                        "purchase_order_id": po["id"],
                        "invoice_date": d(-21),
                        "lien_waiver_received": True,
                        "lines": [
                            {
                                "cost_code_id": work_code["id"],
                                "amount": f"{first_claim:.2f}",
                                "purchase_order_line_id": po_line["id"],
                                "description": "Progress claim 1",
                            }
                        ],
                    },
                )
                if first:
                    await P(f"/invoices/{first['id']}/approve", None, ok=(200,))

                second = await P(
                    "/invoices",
                    {
                        "invoice_number": "INV-2042",
                        "purchase_order_id": po["id"],
                        "invoice_date": d(-4),
                        # No waiver yet: releasing this one is refused, which is
                        # exactly the leverage retainage exists to give.
                        "lien_waiver_received": False,
                        "lines": [
                            {
                                "cost_code_id": work_code["id"],
                                "amount": f"{second_claim:.2f}",
                                "purchase_order_line_id": po_line["id"],
                                "description": "Progress claim 2",
                            }
                        ],
                    },
                )
                if second:
                    await P(f"/invoices/{second['id']}/approve", None, ok=(200,))

        # --- a change order left pending, so the forecast has something to warn on
        existing_cos = await G(f"/jobs/{building['id']}/change-orders")
        if not any(co["status"] in ("draft", "pending_approval") for co in existing_cos):
            print("pending change order")
            await P(
                f"/jobs/{building['id']}/change-orders",
                {
                    "description": "Rock encountered in the footing trench, east elevation",
                    "reason": "Unforeseen ground conditions. Breaker hire and extra excavation.",
                    "responsible_party": "builder",
                    "price_delta": "0.00",
                    "schedule_days": 3,
                    "cost_lines": [
                        {"cost_code_id": work_code["id"], "amount": "11500.00"},
                    ],
                },
            )

        # --- inspection checklist -------------------------------------------
        inspections_raw = await G(f"/field/inspections?job_id={building['id']}")
        inspections = inspections_raw.get("items", []) if isinstance(inspections_raw, dict) else inspections_raw
        if not any(i.get("items") for i in inspections):
            print("inspection checklist")
            insp = await P(
                "/field/inspections",
                {
                    "job_id": building["id"],
                    "name": "Framing inspection",
                    "inspection_type": "municipal",
                    "scheduled_date": d(2),
                    "inspector": "City of Maple Ridge",
                },
            )
            if insp:
                checks = [
                    "Fire blocking complete at all penetrations",
                    "Joist hangers fully nailed",
                    "Hold-downs installed per S-101",
                    "Header sizes match structural drawings",
                    "Sheathing nailing pattern at shear walls",
                    "Temporary bracing still in place",
                ]
                inspection_id = insp["id"]
                for description in checks:
                    insp = await P(
                        f"/field/inspections/{inspection_id}/items",
                        {"description": description, "vendor_id": framing["id"]},
                    )
                    if insp is None:
                        print("  ! could not add checks; is the API running this build?",
                              file=sys.stderr)
                        break

                # Part-completed: two passes, one real failure, three still to check.
                items = (insp or {}).get("items", [])[:3]
                outcomes = [
                    {"result": "pass"},
                    {"result": "pass"},
                    {
                        "result": "fail",
                        "notes": "Two hold-downs missing at the east shear wall.",
                    },
                ]
                for item, outcome in zip(items, outcomes, strict=False):
                    await P(
                        f"/field/inspections/{inspection_id}/items/{item['id']}/result",
                        outcome,
                        ok=(200,),
                    )

        # --- summary ---------------------------------------------------------
        cost = await G(f"/jobs/{building['id']}/cost")
        print("\nstate of", building["job_number"] + ":")
        print(f"  revised budget    {cost['revised_total']}")
        print(f"  exposure          {cost['exposure_total']}")
        print(f"  pending change    {cost['pending_change_total']}")
        print(f"  forecast          {cost['forecast_total']}")
        print(f"  retainage held    {cost['retainage_held']}")
        print(f"  over budget       {cost['over_budget']}")
        print(f"  forecast over     {cost['forecast_over_budget']}")
        print(f"  will go over      {cost['will_go_over']}")


if __name__ == "__main__":
    asyncio.run(main())
