"""The advanced RFI module: the response thread, the lifecycle and the analytics.

The change-order join has its own file (test_rfi_change_order.py). This covers what
grew around it: a thread with one official answer, an append-only activity log, the
reopen/void lifecycle, and the summary the coordinator reads.
"""

from tests.conftest import post


async def _rfi(client, h, job_id, **extra):
    return await post(
        client, h, "/api/v1/rfis",
        {
            "job_id": job_id,
            "subject": "Beam bearing at grid C",
            "question": "Structural and architectural disagree on the head height. Which governs?",
            **extra,
        },
    )


async def test_raising_an_rfi_opens_it_and_logs_it(client, sold_job):
    """A raised RFI opens with a due date set from its priority and a log entry."""
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job["job"]["id"], priority="urgent", category="structural",
                     location="Plot 47, first floor")

    assert rfi["status"] == "open"
    assert rfi["ball_in_court"] == "builder"
    assert rfi["category"] == "structural"
    assert rfi["location"] == "Plot 47, first floor"
    # Urgent is a two-day response promise, so the due date is set even though none
    # was supplied.
    assert rfi["due_on"] is not None
    assert rfi["response_count"] == 0
    kinds = [a["kind"] for a in rfi["activity"]]
    assert "raised" in kinds


async def test_a_thread_gathers_responses_and_one_official_answer(client, sold_job):
    """Several parties can weigh in; marking one official answers the RFI and hands
    the ball back."""
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job["job"]["id"])

    # A non-official clarification leaves the RFI open.
    after_note = await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/responses",
        {"body": "Checking the calc pack before I answer.", "court": "engineer"},
        expect=201,
    )
    assert after_note["status"] == "open"
    assert after_note["response_count"] == 1
    assert after_note["answer"] is None

    # The official response answers it.
    answered = await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/responses",
        {"body": "Architectural governs; keep 2.4m.", "court": "architect", "is_official": True},
        expect=201,
    )
    assert answered["status"] == "answered"
    assert answered["answer"] == "Architectural governs; keep 2.4m."
    assert answered["answered_on"] is not None
    assert sum(1 for r in answered["responses"] if r["is_official"]) == 1
    assert "answered" in [a["kind"] for a in answered["activity"]]


async def test_a_second_official_response_demotes_the_first(client, sold_job):
    """Only one answer stands. A newer official response supersedes the older one."""
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job["job"]["id"])

    await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/responses",
        {"body": "First take: 2.4m.", "court": "architect", "is_official": True},
        expect=201,
    )
    final = await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/responses",
        {"body": "Corrected: 2.5m.", "court": "architect", "is_official": True},
        expect=201,
    )
    official = [r for r in final["responses"] if r["is_official"]]
    assert len(official) == 1
    assert official[0]["body"] == "Corrected: 2.5m."
    assert final["answer"] == "Corrected: 2.5m."


async def test_the_one_shot_answer_records_an_official_response(client, sold_job):
    """The simple /answer path and the thread cannot tell different stories: answering
    leaves exactly one official response behind."""
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job["job"]["id"])

    answered = await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/answer",
        {"answer": "Architectural governs. No change.", "cost_impact": False},
        expect=200,
    )
    assert answered["status"] == "answered"
    assert answered["response_count"] == 1
    assert answered["responses"][0]["is_official"] is True


async def test_close_requires_an_answer_then_reopen_puts_it_back(client, sold_job):
    """An RFI cannot be closed unanswered; a closed one can be reopened."""
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job["job"]["id"])

    refused = await client.post(f"/api/v1/rfis/{rfi['id']}/close", headers=h)
    assert refused.status_code == 409
    assert "answered" in refused.json()["detail"]

    await post(client, h, f"/api/v1/rfis/{rfi['id']}/answer",
               {"answer": "Keep 2.4m."}, expect=200)
    closed = await post(client, h, f"/api/v1/rfis/{rfi['id']}/close", None, expect=200)
    assert closed["status"] == "closed"
    assert closed["ball_in_court"] == "closed"
    assert closed["closed_on"] is not None

    reopened = await post(client, h, f"/api/v1/rfis/{rfi['id']}/reopen", None, expect=200)
    assert reopened["status"] == "open"
    assert reopened["ball_in_court"] == "builder"
    assert reopened["closed_on"] is None
    assert "reopened" in [a["kind"] for a in reopened["activity"]]


async def test_voiding_withdraws_the_question(client, sold_job):
    """A void keeps the record and the number but takes it out of every queue."""
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job["job"]["id"])

    voided = await post(client, h, f"/api/v1/rfis/{rfi['id']}/void", None, expect=200)
    assert voided["status"] == "void"

    # A voided RFI will not accept new responses.
    refused = await client.post(
        f"/api/v1/rfis/{rfi['id']}/responses", headers=h, json={"body": "too late"}
    )
    assert refused.status_code == 409


async def test_reassigning_moves_the_ball_and_logs_it(client, sold_job):
    h = sold_job["headers"]
    rfi = await _rfi(client, h, sold_job["job"]["id"])

    moved = await post(
        client, h, f"/api/v1/rfis/{rfi['id']}/reassign",
        {"ball_in_court": "architect", "note": "Design query, over to you."},
        expect=200,
    )
    assert moved["ball_in_court"] == "architect"
    reassigned = [a for a in moved["activity"] if a["kind"] == "reassigned"]
    assert reassigned and "architect" in reassigned[0]["detail"]


async def test_summary_counts_the_board(client, sold_job):
    """The summary is the board: open, overdue, blocking, in-our-court, by category."""
    h = sold_job["headers"]
    job_id = sold_job["job"]["id"]

    # One blocking, one answered.
    await _rfi(client, h, job_id, subject="Blocking one", is_blocking=True, category="mep")
    a2 = await _rfi(client, h, job_id, subject="To answer", category="design")
    await post(client, h, f"/api/v1/rfis/{a2['id']}/answer", {"answer": "Done."}, expect=200)

    summary = (await client.get("/api/v1/rfis/summary", headers=h)).json()
    assert summary["total"] == 2
    assert summary["open"] == 1
    assert summary["answered"] == 1
    assert summary["blocking"] == 1
    assert summary["in_our_court"] == 1          # the open, builder-court one
    assert summary["by_category"]["mep"] == 1
    assert summary["by_category"]["design"] == 1
    assert set(summary["aging"].keys()) == {"0-3", "4-7", "8-14", "15+"}
