"""Change orders, scheduling, invoices, warranty, CRM and the trade portal."""

from decimal import Decimal

from tests.conftest import post

# --- Change orders ------------------------------------------------------------


async def test_buyer_funded_change_order_moves_price_and_budget(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    co = await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders",
        {
            "description": "Buyer added a rear patio slab",
            "responsible_party": "buyer",
            "price_delta": "6000.00",
            "schedule_days": 2,
            "cost_lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "3800.00"}],
        },
    )
    assert Decimal(co["cost_delta"]) == Decimal("3800.00")
    assert Decimal(co["margin_impact"]) == Decimal("2200.00")

    result = await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders/{co['id']}/approve", None, expect=200
    )
    assert Decimal(result["price_delta"]) == Decimal("6000.00")

    contract = (await client.get(f"/api/v1/jobs/{job_id}/contract", headers=h)).json()
    budget = (await client.get(f"/api/v1/jobs/{job_id}/budget", headers=h)).json()
    assert Decimal(contract["options_total"]) == Decimal("6000.00")
    assert Decimal(budget["revised_total"]) == Decimal("83800.00")


async def test_builder_funded_change_order_costs_but_does_not_charge(client, sold_job):
    """The builder absorbs it: budget rises, the buyer is not billed. Margin drops."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    co = await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders",
        {
            "description": "Rock encountered in footings",
            "responsible_party": "builder",
            "price_delta": "4000.00",
            "cost_lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "4000.00"}],
        },
    )
    await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders/{co['id']}/approve", None, expect=200
    )

    contract = (await client.get(f"/api/v1/jobs/{job_id}/contract", headers=h)).json()
    budget = (await client.get(f"/api/v1/jobs/{job_id}/budget", headers=h)).json()

    assert Decimal(contract["options_total"]) == Decimal("0")       # buyer not charged
    assert Decimal(budget["revised_total"]) == Decimal("84000.00")  # cost still incurred


async def test_trade_backcharge_moves_neither_price_nor_budget(client, sold_job):
    """A trade's error is recovered from the trade, not added to the job budget."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    co = await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders",
        {
            "description": "Framer cut a joist incorrectly",
            "responsible_party": "trade",
            "price_delta": "0",
            "cost_lines": [{"cost_code_id": sold_job["framing"]["id"], "amount": "1500.00"}],
        },
    )
    await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders/{co['id']}/approve", None, expect=200
    )

    contract = (await client.get(f"/api/v1/jobs/{job_id}/contract", headers=h)).json()
    budget = (await client.get(f"/api/v1/jobs/{job_id}/budget", headers=h)).json()
    assert Decimal(contract["options_total"]) == Decimal("0")
    assert Decimal(budget["revised_total"]) == Decimal("80000.00")


async def test_approved_change_order_cannot_be_rejected(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    co = await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders",
        {"description": "x", "price_delta": "100.00"},
    )
    await post(
        client, h, f"/api/v1/jobs/{job_id}/change-orders/{co['id']}/approve", None, expect=200
    )
    resp = await client.post(
        f"/api/v1/jobs/{job_id}/change-orders/{co['id']}/reject", headers=h
    )
    assert resp.status_code == 409


# --- Scheduling ---------------------------------------------------------------


async def _template(client, h, sold_job):
    tmpl = await post(
        client, h, "/api/v1/schedule-templates", {"code": "STD", "name": "Standard build"}
    )
    for seq, (name, offset, dur, code) in enumerate(
        [
            ("Foundation", 0, 5, None),
            ("Framing", 5, 10, sold_job["framing"]["id"]),
            ("Electrical rough-in", 15, 4, sold_job["electrical"]["id"]),
            ("Drywall", 19, 6, None),
        ],
        start=1,
    ):
        await post(
            client, h, f"/api/v1/schedule-templates/{tmpl['id']}/tasks",
            {
                "sequence": seq,
                "name": name,
                "offset_days": offset,
                "duration_days": dur,
                "cost_code_id": code,
                "vendor_id": sold_job["vendor"]["id"] if name.startswith("Electrical") else None,
            },
        )
    return tmpl


async def test_schedule_generates_from_template(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    tmpl = await _template(client, h, sold_job)

    tasks = await post(
        client, h, f"/api/v1/jobs/{job_id}/schedule",
        {"template_id": tmpl["id"], "start_date": "2026-01-05"},
    )
    assert len(tasks) == 4
    assert tasks[0]["name"] == "Foundation"
    assert tasks[0]["scheduled_start"] == "2026-01-05"
    assert tasks[0]["scheduled_finish"] == "2026-01-09"   # 5 days inclusive
    assert tasks[0]["status"] == "ready"                  # nothing in front of it
    # 10 January 2026 is a Saturday. This used to expect it, which is the
    # calendar-day bug written down: five working days after Monday the 5th is
    # Monday the 12th, because nobody frames at the weekend.
    assert tasks[1]["scheduled_start"] == "2026-01-12"


async def test_a_late_task_shifts_everything_behind_it(client, sold_job):
    """The ripple. Without it the schedule goes stale and every date becomes fiction."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    tmpl = await _template(client, h, sold_job)

    tasks = await post(
        client, h, f"/api/v1/jobs/{job_id}/schedule",
        {"template_id": tmpl["id"], "start_date": "2026-01-05"},
    )
    before = (await client.get(f"/api/v1/jobs/{job_id}", headers=h)).json()

    # Foundation was due 9 Jan; it finishes on the 12th - three days late. The
    # date goes in the body now rather than the query string.
    result = await post(
        client, h, f"/api/v1/tasks/{tasks[0]['id']}/complete",
        {"actual_finish": "2026-01-12"}, expect=200,
    )
    assert result["days_late"] == 3
    assert result["tasks_shifted"] == 3

    after_tasks = (await client.get(f"/api/v1/jobs/{job_id}/schedule", headers=h)).json()
    assert after_tasks[1]["scheduled_start"] == "2026-01-15"  # was Mon 12 Jan
    assert after_tasks[1]["status"] == "ready"                # framing can now start

    # The *forecast* moves; the promise does not. This used to assert that
    # `projected_completion` grew, which meant the target retreated ahead of every
    # slip and the job could never be reported late.
    after_job = (await client.get(f"/api/v1/jobs/{job_id}", headers=h)).json()
    assert after_job["projected_completion"] == before["projected_completion"]
    assert after_job["forecast_completion"] > before["forecast_completion"]


async def test_schedule_cannot_be_generated_twice(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    tmpl = await _template(client, h, sold_job)
    await post(client, h, f"/api/v1/jobs/{job_id}/schedule", {"template_id": tmpl["id"]})
    resp = await client.post(
        f"/api/v1/jobs/{job_id}/schedule", headers=h, json={"template_id": tmpl["id"]}
    )
    assert resp.status_code == 409


# --- Invoices and actual cost -------------------------------------------------


async def _issued_po(client, h, sold_job, amount="20000.00"):
    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": sold_job["vendor"]["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": amount}],
        },
    )
    await post(client, h, f"/api/v1/purchase-orders/{po['id']}/issue", None, expect=200)
    return po


async def test_approving_an_invoice_records_actual_cost(client, sold_job):
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]
    po = await _issued_po(client, h, sold_job)

    inv = await post(
        client, h, "/api/v1/invoices",
        {
            "invoice_number": "INV-1001",
            "purchase_order_id": po["id"],
            "lien_waiver_received": True,
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "20000.00"}],
        },
    )
    assert inv["status"] == "received"

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["actual_total"]) == Decimal("0")  # not yet approved

    await post(client, h, f"/api/v1/invoices/{inv['id']}/approve", None, expect=200)

    cost = (await client.get(f"/api/v1/jobs/{job_id}/cost", headers=h)).json()
    assert Decimal(cost["committed_total"]) == Decimal("20000.00")
    assert Decimal(cost["actual_total"]) == Decimal("20000.00")
    assert Decimal(cost["exposure_total"]) == Decimal("20000.00")


async def test_invoice_over_its_po_is_refused(client, sold_job):
    """A vendor billing above their PO is the classic margin leak."""
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job, "20000.00")

    inv = await post(
        client, h, "/api/v1/invoices",
        {
            "invoice_number": "INV-1002",
            "purchase_order_id": po["id"],
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "24000.00"}],
        },
    )
    resp = await client.post(f"/api/v1/invoices/{inv['id']}/approve", headers=h)
    assert resp.status_code == 409
    assert "purchase order" in resp.json()["detail"]


async def test_payment_requires_a_lien_waiver(client, sold_job):
    h = sold_job["headers"]
    po = await _issued_po(client, h, sold_job)
    inv = await post(
        client, h, "/api/v1/invoices",
        {
            "invoice_number": "INV-1003",
            "purchase_order_id": po["id"],
            "lien_waiver_received": False,
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "5000.00"}],
        },
    )
    await post(client, h, f"/api/v1/invoices/{inv['id']}/approve", None, expect=200)

    resp = await client.post(f"/api/v1/invoices/{inv['id']}/pay", headers=h)
    assert resp.status_code == 409
    assert "lien waiver" in resp.json()["detail"]


# --- Warranty -----------------------------------------------------------------


async def test_warranty_resolves_when_all_service_orders_complete(client, sold_job):
    h = sold_job["headers"]

    req = await post(
        client, h, "/api/v1/warranty/requests",
        {
            "job_id": sold_job["job"]["id"],
            "title": "Cracked tile in master bathroom",
            "location": "master bathroom",
            "priority": "normal",
        },
    )
    assert req["status"] == "submitted"
    assert req["buyer_id"] is not None  # picked up from the contract

    order = await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"], "instructions": "Replace cracked tile"},
    )

    after = (await client.get(f"/api/v1/warranty/requests/{req['id']}", headers=h)).json()
    assert after["status"] == "triaged"

    await post(
        client, h, f"/api/v1/warranty/service-orders/{order['id']}/complete?resolution=Replaced",
        None, expect=200,
    )

    resolved = (await client.get(f"/api/v1/warranty/requests/{req['id']}", headers=h)).json()
    assert resolved["status"] == "resolved"
    assert resolved["resolved_date"] is not None


# --- CRM ----------------------------------------------------------------------


async def test_lead_converts_to_a_buyer_and_keeps_the_link(client, builder):
    h = builder["headers"]

    lead = await post(
        client, h, "/api/v1/leads",
        {
            "first_name": "Sam",
            "last_name": "Okafor",
            "email": "sam@example.com",
            "source": "website",
            "campaign": "spring-parade",
        },
    )
    assert lead["status"] == "new"

    await post(
        client, h, f"/api/v1/leads/{lead['id']}/activities",
        {"activity_type": "call", "summary": "Left a voicemail"},
    )
    after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=h)).json()
    assert after["status"] == "contacted"

    converted = await post(
        client, h, f"/api/v1/leads/{lead['id']}/convert", None, expect=200
    )
    assert converted["status"] == "converted"
    assert converted["converted_buyer_id"] is not None

    # Source survives conversion, which is what makes marketing attribution possible.
    pipeline = (await client.get("/api/v1/reports/sales-pipeline", headers=h)).json()
    website = next(r for r in pipeline["by_source"] if r["source"] == "website")
    assert website["converted"] == 1
    assert website["conversion_rate"] == 1.0


async def test_a_lead_cannot_be_converted_twice(client, builder):
    h = builder["headers"]
    lead = await post(
        client, h, "/api/v1/leads",
        {"first_name": "A", "last_name": "B", "email": "ab@example.com"},
    )
    for step in ("contacted", "qualified"):
        await post(client, h, f"/api/v1/leads/{lead['id']}/status", {"status": step}, expect=200)
    await post(client, h, f"/api/v1/leads/{lead['id']}/convert", None, expect=200)
    resp = await client.post(f"/api/v1/leads/{lead['id']}/convert", headers=h)
    assert resp.status_code == 409


# --- Reporting ----------------------------------------------------------------


async def test_dashboard_rolls_up_the_whole_tenant(client, sold_job):
    h = sold_job["headers"]
    data = (await client.get("/api/v1/reports/dashboard", headers=h)).json()

    assert data["construction"]["jobs_total"] == 1
    assert Decimal(data["money"]["budget_revised"]) == Decimal("80000.00")
    assert Decimal(data["sales"]["sold_value"]) == Decimal("0")  # contract not yet signed


async def test_job_margin_uses_exposure_not_just_invoices(client, sold_job):
    h = sold_job["headers"]
    await _issued_po(client, h, sold_job, "20000.00")

    report = (await client.get("/api/v1/reports/job-margin", headers=h)).json()
    job = report["jobs"][0]

    assert Decimal(job["revenue"]) == Decimal("427000.00")
    assert Decimal(job["committed"]) == Decimal("20000.00")
    assert Decimal(job["actual"]) == Decimal("0")
    assert Decimal(job["exposure"]) == Decimal("20000.00")  # committed counts
    assert Decimal(job["margin"]) == Decimal("407000.00")
