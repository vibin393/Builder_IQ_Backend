"""Seed a demo builder and walk one home through the whole loop, against the real API.

Run the API first, then:  python scripts/demo_seed.py

It sets up a builder, sells lot 47, budgets it, has the buyer add an upgrade, and shows
the job-cost position before and after. Safe to re-run: it creates a fresh tenant each
time using a timestamped email.
"""

import asyncio
import os
from datetime import date, timedelta
from decimal import Decimal

import httpx

# Override with CORMAN_API_BASE when the API runs somewhere else.
BASE = os.environ.get("CORMAN_API_BASE", "http://127.0.0.1:8400") + "/api/v1"


def money(value) -> str:
    return f"{Decimal(value):>12,.2f}"


async def main() -> None:
    stamp = date.today().isoformat().replace("-", "")
    suffix = str(abs(hash(stamp + str(asyncio.get_event_loop().time()))))[:6]

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        reg = await c.post(
            "/auth/register",
            json={
                "company_name": f"Demo Builder {suffix}",
                "full_name": "Demo Owner",
                "email": f"demo{suffix}@demobuilder.example.com",
                "password": "supersecret123",
            },
        )
        reg.raise_for_status()
        h = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        print(f"Registered tenant 'Demo Builder {suffix}'\n")

        async def post(path, payload=None):
            r = await c.post(path, headers=h, json=payload)
            r.raise_for_status()
            return r.json()

        async def get(path):
            r = await c.get(path, headers=h)
            r.raise_for_status()
            return r.json()

        # --- Configuration the builder does once ---
        framing = await post("/cost-codes", {"code": "06-100", "name": "Framing labour"})
        electrical = await post("/cost-codes", {"code": "16-100", "name": "Electrical"})
        vendor = await post("/vendors", {"code": "SPARK", "name": "Sparks Electrical"})

        community = await post("/communities", {"code": "OAK", "name": "Oak Ridge"})
        lot = await post(
            f"/communities/{community['id']}/lots",
            {"lot_number": "47", "premium": "15000.00"},
        )

        plan = await post(
            "/plans", {"code": "P3", "name": "The Maple", "base_price": "420000.00"}
        )
        await post(
            f"/plans/{plan['id']}/cost-lines",
            {"cost_code_id": framing["id"], "amount": "58000.00"},
        )
        await post(
            f"/plans/{plan['id']}/cost-lines",
            {"cost_code_id": electrical["id"], "amount": "22000.00"},
        )

        option = await post(
            "/options",
            {
                "code": "KIT-UP",
                "name": "Gourmet kitchen package",
                "category": "interior",
                "price": "8500.00",
                "schedule_days": 3,
            },
        )
        await post(
            f"/options/{option['id']}/cost-lines",
            {"cost_code_id": electrical["id"], "amount": "1200.00", "vendor_id": vendor["id"]},
        )
        await post(
            f"/options/{option['id']}/cost-lines",
            {"cost_code_id": framing["id"], "amount": "4000.00"},
        )
        print("Catalog configured: 2 cost codes, 1 trade, 1 community, 1 lot, 1 plan, 1 option\n")

        # --- Sell and budget one home ---
        completion = date.today() + timedelta(days=180)
        job = await post(
            "/jobs",
            {
                "community_id": community["id"],
                "lot_id": lot["id"],
                "plan_id": plan["id"],
                "projected_completion": completion.isoformat(),
            },
        )
        await post(
            f"/jobs/{job['id']}/contract",
            {
                "buyer": {"first_name": "Dana", "last_name": "Reyes"},
                "base_price": "420000.00",
                "lot_premium": "15000.00",
                "incentives": "8000.00",
            },
        )
        await post(f"/jobs/{job['id']}/contract/sign")
        await post(f"/jobs/{job['id']}/budget")

        contract = await get(f"/jobs/{job['id']}/contract")
        cost = await get(f"/jobs/{job['id']}/cost")
        print(f"Job {job['job_number']} sold on lot 47, plan {plan['code']}")
        print(f"  contract total   {money(contract['total_price'])}")
        print(f"  budget (revised) {money(cost['revised_total'])}")
        print(f"  completion       {completion}\n")

        # --- The buyer adds an upgrade at the design appointment ---
        selection = await post(
            f"/jobs/{job['id']}/selections", {"option_id": option["id"]}
        )
        cascade = await post(f"/jobs/{job['id']}/selections/{selection['id']}/confirm")

        contract = await get(f"/jobs/{job['id']}/contract")
        cost = await get(f"/jobs/{job['id']}/cost")
        job_after = await get(f"/jobs/{job['id']}")

        print("Buyer confirms 'Gourmet kitchen package'. One decision, three systems:")
        print(f"  contract total   {money(contract['total_price'])}  (+{cascade['price_delta']})")
        print(f"  budget (revised) {money(cost['revised_total'])}  (+{cascade['cost_delta']})")
        print(
            f"  completion       {job_after['projected_completion']}  "
            f"(+{cascade['schedule_days_delta']} days)\n"
        )

        # --- Purchasing commits money against that budget ---
        po = await post(
            "/purchase-orders",
            {
                "job_id": job["id"],
                "vendor_id": vendor["id"],
                "lines": [{"cost_code_id": electrical["id"], "amount": "23000.00"}],
            },
        )
        await post(f"/purchase-orders/{po['id']}/issue")

        over = await c.post(
            "/purchase-orders",
            headers=h,
            json={
                "job_id": job["id"],
                "vendor_id": vendor["id"],
                "lines": [{"cost_code_id": framing["id"], "amount": "99000.00"}],
            },
        )
        blocked = await c.post(
            f"/purchase-orders/{over.json()['id']}/issue", headers=h
        )

        cost = await get(f"/jobs/{job['id']}/cost")
        print(f"PO {po['po_number']} issued to Sparks Electrical for 23,000")
        print(f"  A second PO of 99,000 against framing was refused: HTTP {blocked.status_code}")
        print(f"  {blocked.json()['detail']}\n")

        print("Job cost position:")
        print(f"  {'code':10} {'budget':>12} {'committed':>12} {'remaining':>12}")
        for line in cost["lines"]:
            print(
                f"  {line['code']:10} {money(line['revised_amount'])} "
                f"{money(line['committed_amount'])} {money(line['uncommitted'])}"
            )
        print(
            f"  {'TOTAL':10} {money(cost['revised_total'])} "
            f"{money(cost['committed_total'])} {money(cost['uncommitted_total'])}"
        )


if __name__ == "__main__":
    asyncio.run(main())
