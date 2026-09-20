"""
store.py -- run the SAME assertions against BOTH backends.

WHY PARAMETRISE RATHER THAN WRITE TWO SUITES. The entire value of the LeadStore
interface is that callers cannot tell the implementations apart. A suite that
tests them separately would let them drift, and the drift would only surface
after switching STORE_BACKEND in production -- the worst possible moment. Every
test here runs twice, so a divergence is a failure, not a discovery.
"""

import pytest

from app.schema import Lead
from app.store import decode_cursor, encode_cursor

BACKENDS = ["memory_store", "sqlite_store"]


@pytest.fixture(params=BACKENDS)
def store(request):
    return request.getfixturevalue(request.param)


async def test_job_round_trips(store):
    job = await store.create_job(total=3, user_id="u1")
    assert job.status == "queued"
    fetched = await store.get_job(job.id)
    assert fetched.id == job.id and fetched.total == 3


async def test_unknown_job_is_none_not_an_error(store):
    assert await store.get_job("nope") is None


async def test_leads_append_in_order_and_counters_agree(store):
    job = await store.create_job(total=2, user_id="u1")
    await store.append_lead(job.id, Lead(first_name="A", source_filename="a.jpg"))
    await store.append_lead(job.id, Lead(source_filename="b.jpg",
                                         status="parse_error", error="truncated"))
    fetched = await store.get_job(job.id)
    assert [lead.source_filename for lead in fetched.leads] == ["a.jpg", "b.jpg"]
    summary = fetched.summary()
    assert (summary["processed"], summary["succeeded"], summary["failed"]) == (2, 1, 1)


async def test_append_lead_assigns_an_id(store):
    job = await store.create_job(total=1, user_id="u1")
    lead = Lead(first_name="A")
    await store.append_lead(job.id, lead)
    assert lead.id, "the store must stamp an id onto the lead it was given"
    assert (await store.get_job(job.id)).leads[0].id == lead.id


async def test_status_transitions_stamp_finished_at(store):
    job = await store.create_job(total=1, user_id="u1")
    await store.set_status(job.id, "running")
    assert (await store.get_job(job.id)).finished_at is None
    await store.set_status(job.id, "done")
    assert (await store.get_job(job.id)).finished_at


# --- THE ISOLATION CASE ---------------------------------------------------
#
# This is the security property the whole user_id column exists for, and it is
# tested through every read path rather than just one: a leak in any single
# accessor is a leak.

async def test_one_user_cannot_see_anothers_jobs_leads_or_images(store):
    job_a = await store.create_job(total=1, user_id="alice")
    lead_a = Lead(first_name="Alice", image_sha256="a" * 64)
    await store.append_lead(job_a.id, lead_a)

    job_b = await store.create_job(total=1, user_id="bob")
    await store.append_lead(job_b.id, Lead(first_name="Bob", image_sha256="b" * 64))

    # list_jobs
    alice_jobs = await store.list_jobs("alice")
    assert [j.id for j in alice_jobs.jobs] == [job_a.id]

    # all_leads (the "export everything" path)
    alice_leads = await store.all_leads("alice")
    assert [lead.first_name for lead in alice_leads] == ["Alice"]

    # get_lead -- the image route's lookup
    assert (await store.get_lead(lead_a.id, "alice")).first_name == "Alice"
    assert await store.get_lead(lead_a.id, "bob") is None, "BOB READ ALICE'S LEAD"

    # A nonexistent id and someone else's id must be indistinguishable, or the
    # difference becomes an oracle for enumerating valid ids.
    assert await store.get_lead("0" * 16, "bob") is None


async def test_get_job_does_not_itself_filter_by_user(store):
    # Documenting the actual division of responsibility: get_job returns the
    # job and the ROUTE checks job.owned_by(). get_lead is the opposite. A
    # future reader must not assume get_job is already safe.
    job = await store.create_job(total=1, user_id="alice")
    fetched = await store.get_job(job.id)
    assert fetched is not None
    assert not fetched.owned_by("bob")


# --- pagination -----------------------------------------------------------

async def test_cursor_pagination_covers_every_row_exactly_once(store):
    created = [(await store.create_job(total=1, user_id="u1")).id for _ in range(23)]
    await store.create_job(total=1, user_id="other")

    seen, cursor, pages = [], None, 0
    while True:
        page = await store.list_jobs("u1", limit=10, cursor=cursor)
        seen += [job.id for job in page.jobs]
        pages += 1
        if not page.next_cursor:
            break
        cursor = page.next_cursor
        assert pages < 10, "cursor never terminated"

    assert seen == list(reversed(created)), "newest first, no gaps, no repeats"
    assert len(seen) == len(set(seen))


async def test_no_limit_returns_everything(store):
    for _ in range(5):
        await store.create_job(total=1, user_id="u1")
    page = await store.list_jobs("u1")
    assert len(page.jobs) == 5 and page.next_cursor is None


async def test_a_corrupt_cursor_yields_the_first_page_not_a_crash(store):
    for _ in range(3):
        await store.create_job(total=1, user_id="u1")
    # Cursors arrive from a URL, so they are user input.
    for bad in ["!!!", "", "x" * 500, "Zm9vfA=="]:
        page = await store.list_jobs("u1", limit=2, cursor=bad)
        assert isinstance(page.jobs, list)


def test_cursor_round_trips():
    from app.store import Job
    job = Job(id="abc123", total=1, created_at="2026-09-17T10:00:00+00:00")
    assert decode_cursor(encode_cursor(job)) == (job.created_at, job.id)


# --- image retention ------------------------------------------------------

async def test_referenced_image_hashes_is_the_sweep_keeplist(store):
    job = await store.create_job(total=2, user_id="u1")
    await store.append_lead(job.id, Lead(first_name="A", image_sha256="a" * 64))
    await store.append_lead(job.id, Lead(first_name="B"))          # no image
    assert await store.referenced_image_hashes() == {"a" * 64}


async def test_thumbnails_only_include_leads_that_have_images(store):
    job = await store.create_job(total=2, user_id="u1")
    with_image = Lead(first_name="A", image_sha256="a" * 64)
    await store.append_lead(job.id, with_image)
    await store.append_lead(job.id, Lead(first_name="B"))
    page = await store.list_jobs("u1", limit=10)
    assert page.jobs[0].thumbnail_lead_ids == [with_image.id]


# --- sessions -------------------------------------------------------------
#
# A session groups however many uploads it took to collect a set of cards, so
# one spreadsheet covers the lot. These run against both backends like
# everything else above.

async def test_a_session_collects_jobs_from_several_uploads(store):
    session = await store.create_session("u1")
    assert session.title, "a session must have a readable default title"

    first = await store.create_job(total=1, user_id="u1", session_id=session.id)
    await store.append_lead(first.id, Lead(first_name="A", source_filename="a.jpg"))
    second = await store.create_job(total=2, user_id="u1", session_id=session.id)
    await store.append_lead(second.id, Lead(first_name="B", source_filename="b.jpg"))
    await store.append_lead(second.id, Lead(source_filename="c.jpg",
                                            status="parse_error", error="x"))

    jobs = await store.session_jobs(session.id, "u1")
    assert [j.id for j in jobs] == [first.id, second.id], "oldest upload first"

    # THE POINT OF THE FEATURE: one flat list across every upload, in order.
    leads = await store.session_leads(session.id, "u1")
    assert [lead.source_filename for lead in leads] == ["a.jpg", "b.jpg", "c.jpg"]


async def test_session_rollup_counts_span_all_its_jobs(store):
    session = await store.create_session("u1")
    for name, status in [("a", "ok"), ("b", "ok"), ("c", "parse_error")]:
        job = await store.create_job(total=1, user_id="u1", session_id=session.id)
        await store.append_lead(job.id, Lead(source_filename=name, status=status))

    page = await store.list_sessions("u1", limit=10)
    row = page.sessions[0].to_dict()
    assert row["jobs"] == 3
    assert row["cards"] == 3
    assert row["succeeded"] == 2
    assert row["failed"] == 1


async def test_an_empty_session_lists_with_zero_counts(store):
    # A session opened but not yet used must still appear, or "New session"
    # looks broken until the first upload finishes.
    await store.create_session("u1")
    page = await store.list_sessions("u1", limit=10)
    assert len(page.sessions) == 1
    assert page.sessions[0].to_dict()["cards"] == 0


async def test_sessions_are_isolated_between_users(store):
    mine = await store.create_session("alice")
    job = await store.create_job(total=1, user_id="alice", session_id=mine.id)
    await store.append_lead(job.id, Lead(first_name="Alice"))
    await store.create_session("bob")

    assert await store.get_session(mine.id, "bob") is None, "BOB READ ALICE'S SESSION"
    assert await store.session_leads(mine.id, "bob") == [], "BOB READ ALICE'S LEADS"
    assert await store.session_jobs(mine.id, "bob") == []

    bob_page = await store.list_sessions("bob", limit=10)
    assert all(s.id != mine.id for s in bob_page.sessions)


async def test_session_pagination_is_stable(store):
    created = [(await store.create_session("u1")).id for _ in range(12)]
    seen, cursor = [], None
    while True:
        page = await store.list_sessions("u1", limit=5, cursor=cursor)
        seen += [s.id for s in page.sessions]
        if not page.next_cursor:
            break
        cursor = page.next_cursor
    assert seen == list(reversed(created))
    assert len(seen) == len(set(seen))


# --- measured per-card timing ---------------------------------------------

async def test_no_timing_prior_until_a_job_has_finished(store):
    """
    None, not a guess. The first card genuinely has nothing to predict from,
    and inventing a number is how a progress bar starts lying.
    """
    assert await store.typical_seconds_per_card("u1") is None
    job = await store.create_job(total=1, user_id="u1")
    await store.append_lead(job.id, Lead(first_name="A"))
    # still running -> still no prior
    assert await store.typical_seconds_per_card("u1") is None


async def test_timing_prior_is_per_card_not_per_job(store):
    from app.store import _median_seconds_per_card
    # 300s for 2 cards is 150s per card, not 300.
    assert _median_seconds_per_card([
        ("2026-09-20T10:00:00+00:00", "2026-09-20T10:05:00+00:00", 2),
    ]) == 150


async def test_timing_prior_uses_the_median(store):
    from app.store import _median_seconds_per_card
    # One pathological card must not drag the estimate.
    rates = _median_seconds_per_card([
        ("2026-09-20T10:00:00+00:00", "2026-09-20T10:02:30+00:00", 1),   # 150
        ("2026-09-20T11:00:00+00:00", "2026-09-20T11:02:40+00:00", 1),   # 160
        ("2026-09-20T12:00:00+00:00", "2026-09-20T13:00:00+00:00", 1),   # 3600
    ])
    assert rates == 160, f"a mean would have given {(150+160+3600)/3:.0f}"


async def test_timing_prior_ignores_nonsense_rows(store):
    from app.store import _median_seconds_per_card
    assert _median_seconds_per_card([
        ("2026-09-20T10:00:00+00:00", None, 1),                          # unfinished
        ("2026-09-20T10:00:00+00:00", "2026-09-20T10:05:00+00:00", 0),   # no cards
        ("2026-09-20T10:05:00+00:00", "2026-09-20T10:00:00+00:00", 1),   # clock moved
        ("not-a-date", "2026-09-20T10:05:00+00:00", 1),                  # malformed
    ]) is None


async def test_timing_prior_is_scoped_per_user(store):
    job = await store.create_job(total=1, user_id="alice")
    await store.append_lead(job.id, Lead(first_name="A"))
    await store.set_status(job.id, "done")
    assert await store.typical_seconds_per_card("bob") is None
