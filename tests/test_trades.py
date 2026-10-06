"""The trades directory: profile, payment terms, contacts, specialisms, the overview."""

from datetime import date, timedelta
from decimal import Decimal

from app.api.v1.vendors import tax_year_start
from tests.conftest import post

API = "/api/v1"


async def _issued_po(client, h, sold_job, amount="10000.00"):
    po = await post(
        client, h, f"{API}/purchase-orders",
        {"job_id": sold_job["job"]["id"], "vendor_id": sold_job["vendor"]["id"],
         "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": amount}]},
    )
    return await post(
        client, h,
        f"{API}/purchase-orders/{po['id']}/issue?allow_over_budget=true&skip_compliance=true",
        None, expect=200,
    )


async def test_the_trades_payment_terms_set_the_invoice_due_date(client, sold_job):
    h = sold_job["headers"]
    r = await client.patch(
        f"{API}/vendors/{sold_job['vendor']['id']}", headers=h,
        json={"payment_terms_days": 14, "company_number": "01234567",
              "vat_number": "GB123456789", "address": "Unit 4, Poole", "postcode": "BH15 1AA"},
    )
    assert r.status_code == 200 and r.json()["payment_terms_days"] == 14
    po = await _issued_po(client, h, sold_job)
    inv = await post(
        client, h, f"{API}/invoices",
        {"invoice_number": "T-1", "purchase_order_id": po["id"],
         "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "100.00"}]},
    )
    assert inv["due_date"] == (date.today() + timedelta(days=14)).isoformat()


async def test_contacts_and_the_primary_one(client, builder):
    h = builder["headers"]
    vid = builder["vendor"]["id"]
    first = await post(
        client, h, f"{API}/trades/{vid}/contacts",
        {"name": "Sam Spark", "role": "Director", "email": "sam@sparks.example.com"},
    )
    assert first["is_primary"] is True                     # the first becomes primary
    qs = await post(
        client, h, f"{API}/trades/{vid}/contacts",
        {"name": "Priya Q", "role": "QS", "phone": "07700 900123", "is_primary": True},
    )
    contacts = (await client.get(f"{API}/trades/{vid}/overview", headers=h)).json()["contacts"]
    assert [c["name"] for c in contacts if c["is_primary"]] == ["Priya Q"]   # only one
    vendor = (await client.get(f"{API}/vendors/{vid}", headers=h)).json()
    assert vendor["contact_name"] == "Priya Q"             # the record follows the primary

    r = await client.delete(f"{API}/vendor-contacts/{qs['id']}", headers=h)
    assert r.status_code == 204


async def test_specialisms_filter_the_directory(client, builder):
    h = builder["headers"]
    vid = builder["vendor"]["id"]
    r = await client.put(
        f"{API}/trades/{vid}/specialisms", headers=h,
        json={"cost_code_ids": [builder["electrical"]["id"]]},
    )
    assert r.status_code == 200 and r.json()[0]["code"] == "16-100"
    url = f"{API}/trades/directory?cost_code_id="
    sparks = (await client.get(url + builder["electrical"]["id"], headers=h)).json()
    assert [v["id"] for v in sparks] == [vid]
    framers = (await client.get(url + builder["framing"]["id"], headers=h)).json()
    assert framers == []


async def test_directory_and_overview_carry_the_money(client, sold_job):
    h = sold_job["headers"]
    vid = sold_job["vendor"]["id"]
    po = await _issued_po(client, h, sold_job, amount="10000.00")
    inv = await post(
        client, h, f"{API}/invoices",
        {"invoice_number": "T-2", "purchase_order_id": po["id"], "lien_waiver_received": True,
         "lines": [{"cost_code_id": sold_job["electrical"]["id"], "amount": "4000.00"}]},
    )
    await post(client, h, f"{API}/invoices/{inv['id']}/approve", None, expect=200)

    listing = (await client.get(f"{API}/trades/directory", headers=h)).json()
    [row] = [v for v in listing if v["id"] == vid]
    assert Decimal(row["open_commitment"]) == Decimal("6000.00")
    assert Decimal(row["outstanding"]) == Decimal("4000.00")
    # No prequalification on file: flagged, not blocked - the gate's deliberate rule.
    assert row["compliant"] is True and "no prequalification" in row["compliance_note"]

    ov = (await client.get(f"{API}/trades/{vid}/overview", headers=h)).json()
    assert Decimal(ov["committed"]) == Decimal("10000.00")
    assert Decimal(ov["billed"]) == Decimal("4000.00")
    assert ov["homes"] == 1 and ov["recent_invoices"][0]["number"] == "T-2"
    assert ov["compliance"]["applies"] is True

    await post(client, h, f"{API}/invoices/{inv['id']}/pay", None, expect=200)
    ov = (await client.get(f"{API}/trades/{vid}/overview", headers=h)).json()
    assert Decimal(ov["paid_this_tax_year"]) == Decimal("4000.00")

    s = (await client.get(f"{API}/trades/summary", headers=h)).json()
    assert s["cis_unverified"] >= 1 and Decimal(s["outstanding"]) == Decimal("0")


def test_the_tax_year_starts_on_6_april():
    assert tax_year_start(date(2026, 9, 28)) == date(2026, 4, 6)
    assert tax_year_start(date(2027, 4, 5)) == date(2026, 4, 6)
    assert tax_year_start(date(2027, 4, 6)) == date(2027, 4, 6)
