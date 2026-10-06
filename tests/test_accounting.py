"""The accounting hand-off: every event reaches the ledger once, as a balanced journal.

Invoices, payments and retention releases go out in numbered batches; a batch is
exported, then posted or voided; nominal mapping, the period lock and the CIS return.
"""

from datetime import date, timedelta
from decimal import Decimal

from app.services.accounting import tax_month
from tests.conftest import post

ACC = "/api/v1/accounting"
INV = "/api/v1/invoices"


async def _po(client, h, sold_job, amount="20000.00", retainage="0"):
    po = await post(
        client, h, "/api/v1/purchase-orders",
        {
            "job_id": sold_job["job"]["id"],
            "vendor_id": sold_job["vendor"]["id"],
            "retainage_percent": retainage,
            "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": amount}],
        },
    )
    return await post(
        client, h,
        f"/api/v1/purchase-orders/{po['id']}/issue?allow_over_budget=true&skip_compliance=true",
        None, expect=200,
    )


async def _approved(client, h, po, number, amount, **extra):
    inv = await post(
        client, h, INV,
        {
            "invoice_number": number,
            "purchase_order_id": po["id"],
            "lien_waiver_received": True,
            "lines": [{"cost_code_id": po["lines"][0]["cost_code_id"], "amount": amount}],
            **extra,
        },
    )
    return await post(client, h, f"{INV}/{inv['id']}/approve", None, expect=200)


async def _batch(client, h, kind, expect=201, **extra):
    return await post(client, h, f"{ACC}/batches", {"kind": kind, **extra}, expect=expect)


def _sum(journal, side):
    return sum((Decimal(j[side]) for j in journal), Decimal("0"))


async def test_an_invoice_goes_to_the_ledger_once_as_a_balanced_journal(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job, retainage="5")
    await _approved(client, h, po, "ACC-1", "12000.00")

    q = (await client.get(f"{ACC}/queue?kind=invoices", headers=h)).json()
    assert len(q["items"]) == 1 and q["balanced"] is True
    accounts = {(j["account"], j["debit"] != "0", j["credit"] != "0") for j in q["journal"]}
    assert ("5000", True, False) in accounts          # unmapped: the default cost account
    assert ("2150", False, True) in accounts          # retention payable, 600
    assert ("2100", False, True) in accounts          # trade creditors, 11,400
    assert any("no nominal code" in w for w in q["items"][0]["warnings"])

    batch = await _batch(client, h, "invoices")
    assert batch["batch_number"].startswith("EXP-")
    assert batch["item_count"] == 1
    assert _sum(batch["journal"], "debit") == _sum(batch["journal"], "credit") == Decimal("12000")

    # The whole point: it is not offered again.
    q = (await client.get(f"{ACC}/queue?kind=invoices", headers=h)).json()
    assert q["items"] == []
    again = await client.post(f"{ACC}/batches", headers=h, json={"kind": "invoices"})
    assert again.status_code == 409


async def test_mapping_routes_cost_to_its_nominal_code(client, sold_job):
    h = sold_job["headers"]
    r = await client.put(
        f"{ACC}/mapping", headers=h,
        json=[{"cost_code_id": sold_job["electrical"]["id"], "gl_account": "5120"}],
    )
    assert r.status_code == 200, r.text
    assert any(m["gl_account"] == "5120" for m in r.json())

    po = await _po(client, h, sold_job)
    await _approved(client, h, po, "ACC-M", "1000.00")
    q = (await client.get(f"{ACC}/queue?kind=invoices", headers=h)).json()
    debit = next(j for j in q["journal"] if j["debit"] != "0")
    assert debit["account"] == "5120"
    assert q["items"][0]["warnings"] == []


async def test_a_payment_waits_for_its_invoice_then_carries_cis(client, sold_job):
    h = sold_job["headers"]
    await client.patch(
        f"/api/v1/vendors/{sold_job['vendor']['id']}", headers=h,
        json={"cis_status": "net", "utr": "1234567890"},
    )
    po = await _po(client, h, sold_job, retainage="5")
    inv = await _approved(client, h, po, "ACC-P", "12000.00", labour_amount="10000.00")
    await post(client, h, f"{INV}/{inv['id']}/pay?payment_reference=BACS-1", None, expect=200)

    # Paid, but the ledger has not seen the invoice yet: the creditor would not exist.
    q = (await client.get(f"{ACC}/queue?kind=payments", headers=h)).json()
    assert q["items"] == [] and q["waiting"] == 1
    r = await client.post(f"{ACC}/batches", headers=h, json={"kind": "payments"})
    assert r.status_code == 409 and "waiting" in r.json()["detail"]

    await _batch(client, h, "invoices")
    pay = await _batch(client, h, "payments")
    by_account = {j["account"]: j for j in pay["journal"]}
    assert Decimal(by_account["2100"]["debit"]) == Decimal("11400.00")   # 12,000 - 600
    assert Decimal(by_account["2211"]["credit"]) == Decimal("2000.00")   # 20% of 10,000
    assert Decimal(by_account["1200"]["credit"]) == Decimal("9400.00")   # what the trade got


async def test_retention_release_is_its_own_journal(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job, retainage="5")
    inv = await _approved(client, h, po, "ACC-R", "10000.00")
    await _batch(client, h, "invoices")
    await post(client, h, f"{INV}/{inv['id']}/release-retainage", None, expect=200)

    rel = await _batch(client, h, "retention")
    assert Decimal(rel["total"]) == Decimal("500.00")
    by_account = {j["account"]: j for j in rel["journal"]}
    assert Decimal(by_account["2150"]["debit"]) == Decimal("500.00")
    assert Decimal(by_account["1200"]["credit"]) == Decimal("500.00")


async def test_void_puts_items_back_and_posted_cannot_be_voided(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job)
    await _approved(client, h, po, "ACC-V", "1000.00")
    batch = await _batch(client, h, "invoices")

    voided = await post(
        client, h, f"{ACC}/batches/{batch['id']}/void", {"reason": "Wrong through-date"},
        expect=200,
    )
    assert voided["status"] == "void"
    q = (await client.get(f"{ACC}/queue?kind=invoices", headers=h)).json()
    assert len(q["items"]) == 1                        # back in the queue
    dl = await client.get(f"{ACC}/batches/{batch['id']}/download", headers=h)
    assert dl.status_code == 409                        # nothing to import from a void batch

    again = await _batch(client, h, "invoices")
    posted = await post(
        client, h, f"{ACC}/batches/{again['id']}/post", {"ledger_reference": "JNL-88"},
        expect=200,
    )
    assert posted["status"] == "posted" and posted["ledger_reference"] == "JNL-88"
    refused = await client.post(
        f"{ACC}/batches/{again['id']}/void", headers=h, json={"reason": "Too late"}
    )
    assert refused.status_code == 409 and "Reverse it" in refused.json()["detail"]


async def test_an_invoice_batch_cannot_be_voided_under_its_payments(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job)
    inv = await _approved(client, h, po, "ACC-D", "1000.00")
    invoices = await _batch(client, h, "invoices")
    await post(client, h, f"{INV}/{inv['id']}/pay", None, expect=200)
    await _batch(client, h, "payments")

    r = await client.post(
        f"{ACC}/batches/{invoices['id']}/void", headers=h, json={"reason": "Mistake"}
    )
    assert r.status_code == 409 and "ACC-D" in r.json()["detail"]


async def test_download_formats_and_count(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job)
    await _approved(client, h, po, "ACC-F", "2500.00")
    batch = await _batch(client, h, "invoices")
    url = f"{ACC}/batches/{batch['id']}/download"

    sage = await client.get(f"{url}?format=sage50", headers=h)
    assert sage.status_code == 200 and "text/csv" in sage.headers["content-type"]
    assert sage.text.startswith("Type,Account Reference")
    assert "\nJD," in sage.text and "\nJC," in sage.text

    xero = await client.get(f"{url}?format=xero", headers=h)
    assert "*Narration" in xero.text and ",-2500.00," in xero.text   # credits are negative

    plain = await client.get(url, headers=h)          # default format from settings
    assert plain.status_code == 200

    detail = (await client.get(f"{ACC}/batches/{batch['id']}", headers=h)).json()
    assert detail["download_count"] == 3


async def test_the_period_lock_moves_postings_to_the_first_open_day(client, sold_job):
    h = sold_job["headers"]
    today = date.today()
    lock = today - timedelta(days=3)
    r = await client.patch(f"{ACC}/settings", headers=h, json={"locked_through": lock.isoformat()})
    assert r.status_code == 200

    future = await client.patch(
        f"{ACC}/settings", headers=h,
        json={"locked_through": (today + timedelta(days=1)).isoformat()},
    )
    assert future.status_code == 422

    po = await _po(client, h, sold_job)
    old = await post(
        client, h, INV,
        {
            "invoice_number": "ACC-L",
            "purchase_order_id": po["id"],
            "invoice_date": (today - timedelta(days=20)).isoformat(),
            "lines": [{"cost_code_id": po["lines"][0]["cost_code_id"], "amount": "100.00"}],
        },
    )
    await post(client, h, f"{INV}/{old['id']}/approve", None, expect=200)
    q = (await client.get(f"{ACC}/queue?kind=invoices", headers=h)).json()
    # Invoiced 20 days ago but approved today: the ledger date is the approval, which is
    # in an open period, so nothing moves.
    assert q["journal"][0]["original_date"] is None

    r = await client.patch(f"{ACC}/settings", headers=h, json={"locked_through": today.isoformat()})
    assert r.status_code == 422                         # today has not ended yet


async def test_cis_return_for_the_tax_month(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job)
    # Unverified trade: on the return, but flagged.
    a = await _approved(client, h, po, "ACC-C1", "3000.00", labour_amount="2000.00")
    await post(client, h, f"{INV}/{a['id']}/pay", None, expect=200)
    # Materials only: not a CIS payment at all.
    b = await _approved(client, h, po, "ACC-C2", "500.00")
    await post(client, h, f"{INV}/{b['id']}/pay", None, expect=200)

    ret = (await client.get(f"{ACC}/cis-return", headers=h)).json()
    start, end = tax_month(date.today())
    assert ret["period_start"] == start.isoformat() and ret["period_end"] == end.isoformat()
    assert ret["due_date"] == end.replace(day=19).isoformat()
    assert len(ret["rows"]) == 1
    row = ret["rows"][0]
    assert Decimal(row["gross_paid"]) == Decimal("3000.00")
    assert Decimal(row["materials"]) == Decimal("1000.00")
    assert "No UTR on record" in row["problems"]


def test_a_locked_period_moves_the_posting_and_says_so():
    """Approvals through the API are always dated today, so exercise the builder directly."""
    import uuid

    from app.models.accounting import AccountingSettings, ExportKind
    from app.models.invoice import Invoice, InvoiceLine
    from app.services.accounting import Lookups, build

    cfg = AccountingSettings(
        cost_account="5000", creditors_account="2100", retention_account="2150",
        cis_account="2211", bank_account="1200", locked_through=date(2026, 8, 31),
    )
    inv = Invoice(
        id=uuid.uuid4(), invoice_number="OLD-1", vendor_id=uuid.uuid4(), job_id=uuid.uuid4(),
        approved_date=date(2026, 8, 20), retainage_percent=Decimal("0"),
        lines=[InvoiceLine(cost_code_id=uuid.uuid4(), amount=Decimal("100.00"))],
    )
    items, journal = build([inv], ExportKind.INVOICES, cfg, Lookups({}, {}, {}))
    assert {j.posting_date for j in journal} == {date(2026, 9, 1)}
    assert {j.original_date for j in journal} == {date(2026, 8, 20)}
    assert any("locked period" in w for w in items[0].warnings)


def test_cis_tax_months_run_from_the_6th_to_the_5th():
    assert tax_month(date(2026, 9, 28)) == (date(2026, 9, 6), date(2026, 10, 5))
    assert tax_month(date(2026, 9, 5)) == (date(2026, 8, 6), date(2026, 9, 5))
    assert tax_month(date(2027, 1, 2)) == (date(2026, 12, 6), date(2027, 1, 5))
    assert tax_month(date(2026, 12, 6)) == (date(2026, 12, 6), date(2027, 1, 5))


async def test_purchasing_can_look_but_only_accounts_can_export(client, sold_job):
    h = sold_job["headers"]
    await post(
        client, h, "/api/v1/users",
        {"email": "buyer@corman.example.com", "full_name": "Buyer",
         "password": "supersecret123", "role": "purchasing"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "buyer@corman.example.com", "password": "supersecret123"},
    )
    ph = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get(f"{ACC}/queue", headers=ph)).status_code == 200
    r = await client.post(f"{ACC}/batches", headers=ph, json={"kind": "invoices"})
    assert r.status_code == 403
    r = await client.patch(f"{ACC}/settings", headers=ph, json={"cost_account": "9999"})
    assert r.status_code == 403


async def test_summary_and_supplier_master(client, sold_job):
    h = sold_job["headers"]
    po = await _po(client, h, sold_job, amount="10000.00")
    await _approved(client, h, po, "ACC-S", "4000.00")

    s = (await client.get(f"{ACC}/summary", headers=h)).json()
    assert s["ready"]["invoices"]["count"] == 1
    assert Decimal(s["ready"]["invoices"]["value"]) == Decimal("4000.00")
    assert Decimal(s["open_commitments"]) >= Decimal("6000.00")
    assert s["unmapped_cost_codes"] >= 1

    await _batch(client, h, "invoices")
    s = (await client.get(f"{ACC}/summary", headers=h)).json()
    assert s["ready"]["invoices"]["count"] == 0
    assert s["unposted_batches"] == 1
    assert Decimal(s["exported_this_month"]) == Decimal("4000.00")

    csv = await client.get(f"{ACC}/vendors.csv", headers=h)
    assert csv.status_code == 200 and "utr,cis_status" in csv.text
