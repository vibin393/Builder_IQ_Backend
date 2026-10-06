"""The trade scorecard.

The tests that matter are the ones about honesty rather than arithmetic: a
dimension with no history must be excluded rather than scored zero, and a trade
with almost no record must be flagged rather than ranked confidently.
"""

from datetime import date

from tests.conftest import post

TODAY = date.today()


async def _score(client, h, vendor_id=None):
    path = "/api/v1/reports/trade-scorecard"
    if vendor_id:
        path += f"?vendor_id={vendor_id}"
    resp = await client.get(path, headers=h)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_trade_with_no_history_scores_nothing_rather_than_zero(client, sold_job):
    """Absence of evidence is not evidence of failure."""
    h = sold_job["headers"]
    rows = await _score(client, h, sold_job["vendor"]["id"])
    assert len(rows) == 1

    dims = rows[0]["dimensions"]
    # Nothing has happened to this trade yet, so every measured axis is empty.
    assert dims["schedule"]["score"] is None
    assert dims["quality"]["score"] is None
    assert dims["safety"]["score"] is None
    assert dims["responsiveness"]["score"] is None
    assert dims["schedule"]["detail"] == "no completed tasks yet"


async def test_punch_items_pull_the_quality_score_down(client, sold_job):
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    for i in range(3):
        await post(
            client, h, "/api/v1/field/punch-items",
            {
                "job_id": sold_job["job"]["id"],
                "description": f"Defect {i}",
                "vendor_id": vendor_id,
            },
        )

    rows = await _score(client, h, vendor_id)
    quality = rows[0]["dimensions"]["quality"]
    assert quality["sample"] == 3
    assert quality["score"] == 0          # nothing cleared
    assert "0 of 3" in quality["detail"]


async def test_clearing_punch_items_lifts_the_quality_score(client, sold_job):
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    ids = []
    for i in range(4):
        item = await post(
            client, h, "/api/v1/field/punch-items",
            {"job_id": sold_job["job"]["id"], "description": f"Defect {i}", "vendor_id": vendor_id},
        )
        ids.append(item["id"])

    # Through the lifecycle, not by PATCHing the status. The trade claims the fix
    # and somebody signs it off - which is the point: a cleared punch list means
    # cleared to the builder's satisfaction, not to the trade's.
    for item_id in ids:
        await post(client, h, f"/api/v1/field/punch-items/{item_id}/submit", None, expect=200)
        await post(client, h, f"/api/v1/field/punch-items/{item_id}/verify", None, expect=200)

    rows = await _score(client, h, vendor_id)
    assert rows[0]["dimensions"]["quality"]["score"] == 100


async def test_a_buyer_found_defect_costs_more_than_one_we_found(client, sold_job):
    """A defect the buyer had to find is worse than one our own QA caught."""
    h = sold_job["headers"]
    ours = sold_job["vendor"]["id"]

    theirs = await post(
        client, h, "/api/v1/vendors",
        {"code": "SECOND", "name": "Second Trade", "vendor_type": "trade"},
    )

    # Same story for both: one open punch item. Only the source differs.
    await post(
        client, h, "/api/v1/field/punch-items",
        {"job_id": sold_job["job"]["id"], "description": "Scuff", "vendor_id": ours},
    )
    await post(
        client, h, "/api/v1/field/punch-items",
        {
            "job_id": sold_job["job"]["id"],
            "description": "Scuff",
            "vendor_id": theirs["id"],
            "from_walkthrough": True,
        },
    )

    rows = {r["vendor_id"]: r for r in await _score(client, h)}
    assert (
        rows[theirs["id"]]["dimensions"]["quality"]["score"]
        <= rows[ours]["dimensions"]["quality"]["score"]
    )
    assert "found by the buyer" in rows[theirs["id"]]["dimensions"]["quality"]["detail"]


async def test_a_rejected_trade_scores_zero_on_compliance(client, sold_job):
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": vendor_id})
    await post(client, h, f"/api/v1/prequalification/{pq['id']}/reject", None, expect=200)

    rows = await _score(client, h, vendor_id)
    compliance = rows[0]["dimensions"]["compliance"]
    assert compliance["score"] == 0
    assert "blocked" in compliance["detail"]


async def test_a_reportable_incident_hurts_the_safety_score(client, sold_job):
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    await post(
        client, h, "/api/v1/incidents",
        {
            "job_id": sold_job["job"]["id"],
            "incident_type": "injury",
            "title": "Laceration",
            "vendor_id": vendor_id,
            "reportable": True,
            "lost_time": True,
        },
    )

    rows = await _score(client, h, vendor_id)
    safety = rows[0]["dimensions"]["safety"]
    assert safety["score"] is not None
    assert safety["score"] < 100
    assert "reportable" in safety["detail"]


async def test_thin_evidence_is_flagged_as_provisional(client, sold_job):
    """A score built on two events should not look like a verdict."""
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": vendor_id})
    await post(client, h, f"/api/v1/prequalification/{pq['id']}/approve", None, expect=200)

    rows = await _score(client, h, vendor_id)
    assert rows[0]["provisional"] is True
    assert rows[0]["evidence"] < 6


async def test_the_composite_ignores_dimensions_with_no_data(client, sold_job):
    """Two empty dimensions must not drag the average toward zero."""
    h = sold_job["headers"]
    vendor_id = sold_job["vendor"]["id"]

    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": vendor_id})
    await post(client, h, f"/api/v1/prequalification/{pq['id']}/approve", None, expect=200)

    rows = await _score(client, h, vendor_id)
    row = rows[0]
    scored = [d for d in row["dimensions"].values() if d["score"] is not None]
    # Compliance is the only thing on record, so the composite is just that.
    assert len(scored) == 1
    assert row["overall"] == scored[0]["score"] == 100


async def test_unscored_trades_sink_below_scored_ones(client, sold_job):
    h = sold_job["headers"]
    known = sold_job["vendor"]["id"]

    await post(
        client, h, "/api/v1/vendors",
        {"code": "UNKNOWN", "name": "Never Used", "vendor_type": "trade"},
    )
    pq = await post(client, h, "/api/v1/prequalification", {"vendor_id": known})
    await post(client, h, f"/api/v1/prequalification/{pq['id']}/approve", None, expect=200)

    rows = await _score(client, h)
    assert rows[0]["vendor_id"] == known
    assert rows[-1]["overall"] is None


async def test_a_trade_is_scored_within_its_own_tenant_only(client, sold_job):
    """The scorecard is a competitive asset. It must not leak across tenants."""
    signup = await client.post(
        "/api/v1/auth/register",
        json={
            "company_name": "Rival Homes",
            "full_name": "Owner",
            "email": "rival@rival.example.com",
            "password": "supersecret123",
        },
    )
    assert signup.status_code == 201
    other = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    rows = await _score(client, other)
    names = {r["vendor_name"] for r in rows}
    assert sold_job["vendor"]["name"] not in names


# --- warranty, weights, trend, specialism --------------------------------------


async def test_warranty_call_backs_are_scored(client, sold_job):
    h = sold_job["headers"]
    req = await post(
        client, h, "/api/v1/warranty/requests",
        {"job_id": sold_job["job"]["id"], "title": "No hot water"},
    )
    await post(
        client, h, f"/api/v1/warranty/requests/{req['id']}/service-orders",
        {"vendor_id": sold_job["vendor"]["id"], "instructions": "Replace valve"},
    )
    [row] = await _score(client, h, sold_job["vendor"]["id"])
    w = row["dimensions"]["warranty"]
    assert w["score"] is not None and w["facts"]["callbacks"] == 1
    assert row["overall"] is not None and row["tier"] in ("A", "B", "C", "D")


async def test_the_builder_sets_the_weights(client, sold_job):
    h = sold_job["headers"]
    base = (await client.get("/api/v1/reports/trade-scorecard/weights", headers=h)).json()
    assert base["weights"]["schedule"] == 25 and base["defaults"]["warranty"] == 15

    bad = await client.put(
        "/api/v1/reports/trade-scorecard/weights", headers=h, json={"luck": 50}
    )
    assert bad.status_code == 422
    zero = {k: 0 for k in base["defaults"]}
    assert (
        await client.put("/api/v1/reports/trade-scorecard/weights", headers=h, json=zero)
    ).status_code == 422

    ok = await client.put(
        "/api/v1/reports/trade-scorecard/weights", headers=h, json={"schedule": 60}
    )
    assert ok.status_code == 200 and ok.json()["weights"]["schedule"] == 60
    again = (await client.get("/api/v1/reports/trade-scorecard/weights", headers=h)).json()
    assert again["weights"]["schedule"] == 60 and again["weights"]["quality"] == 20


async def test_each_read_records_this_months_snapshot(client, sold_job):
    h = sold_job["headers"]
    vid = sold_job["vendor"]["id"]
    await _score(client, h)
    await _score(client, h)                    # rewritten, not duplicated
    hist = (
        await client.get(f"/api/v1/reports/trade-scorecard/{vid}/history", headers=h)
    ).json()
    assert len(hist) == 1
    assert hist[0]["period"] == TODAY.replace(day=1).isoformat()
    [row] = await _score(client, h, vid)
    assert "change" in row and row["last_month"] is None


async def test_filter_by_specialism(client, sold_job):
    h = sold_job["headers"]
    code = sold_job["electrical"]["id"]
    url = f"/api/v1/reports/trade-scorecard?cost_code_id={code}"
    none = (await client.get(url, headers=h)).json()
    assert none == []
    await client.put(
        f"/api/v1/trades/{sold_job['vendor']['id']}/specialisms", headers=h,
        json={"cost_code_ids": [code]},
    )
    one = (await client.get(url, headers=h)).json()
    assert [r["vendor_id"] for r in one] == [sold_job["vendor"]["id"]]
