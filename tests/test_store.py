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
