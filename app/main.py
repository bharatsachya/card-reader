"""
FastAPI application entrypoint.

Run it with:  uvicorn app.main:app --reload --port 8000
"app.main" is the module path, ":app" is the variable below that uvicorn serves.
"""

import asyncio
import contextlib
import logging
import pathlib
import time
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import images
from app.auth import User, require_user
from app.config import settings
from app.excel import build_workbook, export_filename
from app.extraction import extract_lead
from app.imaging import HEIC_SUPPORTED
from app.jobs import schedule_job
from app.schema import Lead
from app.store import store
from app.uploads import (
    UploadTooLarge,
    cleanup_batch_dir,
    make_batch_dir,
    spool_upload,
)

# APPLICATION LOGGING.
#
# uvicorn configures handlers for its OWN loggers and leaves the root logger
# untouched: level WARNING, no handlers. So log.info() from application code
# goes nowhere at all, and log.warning() only escapes via Python's lastResort
# fallback. That is a bad default for messages like "retention deleted 900
# images" -- operational facts that are worthless if nobody can see them, and
# actively misleading when their absence reads as "the sweep did not run".
#
# One handler on one named logger, rather than logging.basicConfig(): basicConfig
# mutates the ROOT logger, which is a decision belonging to whoever runs the
# process, and it silently does nothing when handlers already exist -- so it
# would work here and quietly stop working under gunicorn or Azure's log
# collector. The format matches uvicorn's so the two interleave readably.
log = logging.getLogger("card-reader")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    # Our handler already prints it; propagating would let the root logger
    # print it a second time the moment anything configures root.
    log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup and shutdown, run once per process.

    TWO THINGS HAPPEN HERE, AND BOTH ARE ABOUT FAILING AT THE RIGHT MOMENT.

    1. The store is initialised eagerly. Every store method also prepares
       itself lazily, so this is not strictly required -- but without it, a
       DB_PATH pointing at an unwritable directory would boot a perfectly
       healthy-looking container and then fail on the first upload, after the
       user has already waited through the upload. Doing it here means the
       process dies at boot, the platform's health probe never goes green, and
       a bad deploy never takes traffic.

    2. Jobs orphaned by a previous process are failed. See
       LeadStore.reclaim_stale_jobs -- persistence is what creates this
       problem, so persistence is what has to clean up after it.
    """
    await store.initialize()

    if settings.reclaim_stale_jobs:
        reclaimed = await store.reclaim_stale_jobs()
        if reclaimed:
            log.warning(
                "marked %d job(s) as failed: they were still running when a "
                "previous process exited", reclaimed,
            )

    await _sweep_images()

    yield


async def _sweep_images() -> None:
    """
    Enforce image retention, then delete every file nothing points at.

    THE ORDER OF THESE THREE STEPS IS THE WHOLE CORRECTNESS ARGUMENT.

      1. expire  -- clear image_sha256 on leads past the retention window.
      2. read    -- collect the digests still referenced, AFTER step 1, so the
                    keep-list already reflects the expiries.
      3. sweep   -- delete files not in that list.

    Reading the keep-list before expiring would keep files that should have
    gone (harmless, caught next boot). Sweeping before reading would delete
    files that are still referenced (unrecoverable). Because deduplication
    means one file can back many leads, a single wrong deletion can blank the
    image for dozens of unrelated, unexpired rows -- so the sequence is
    ordered to make the recoverable mistake the only possible one.

    WHY A STARTUP SWEEP AND NOT A CRON. Retention here is a disk-space
    guarantee, not a compliance deadline -- 30 days plus however long this
    process happens to stay up is an acceptable window for a bounded,
    measured-at-under-4GB dataset. A scheduler would add a background task to
    supervise, a lock so two replicas do not sweep at once, and a failure mode
    where a silently-dead timer lets the disk fill. A pass at boot is a few
    milliseconds, needs none of that, and runs on exactly the event that
    matters -- the redeploy.
    """
    if settings.image_retention_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.image_retention_days
        )
        expired = await store.expire_images(cutoff.isoformat())
        if expired:
            log.info(
                "image retention: cleared %d lead(s) older than %d days",
                expired, settings.image_retention_days,
            )

    # Runs even when retention is disabled (0 = keep forever): orphans are
    # produced by ordinary failures too -- a crash between writing the file
    # and committing the row -- not only by expiry.
    deleted, reclaimed = images.sweep(await store.referenced_image_hashes())
    if deleted:
        log.info(
            "image sweep: deleted %d unreferenced file(s), reclaimed %.1f MB",
            deleted, reclaimed / (1024 * 1024),
        )


app = FastAPI(
    title="Business Card Lead Extractor",
    description="Extracts structured leads from business card images using a "
                "self-hosted vision-language model.",
    version="0.4.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def no_store_api(request, call_next):
    """
    Forbid caching of every API response.

    An API response describes state that changes. A browser that reuses a
    cached copy shows the user a past answer with no indication it is doing so,
    and the symptoms are baffling rather than obviously cache-shaped:

      * /api/auth-config cached from when auth was ON keeps the sign-in gate up
        after auth has been switched OFF, so no amount of restarting the server
        changes what the page does.
      * /api/jobs/{id} cached during a poll freezes the progress bar at whatever
        it said the first time, making a healthy job look hung.

    Static files get the same treatment above, for the same reason: a cached
    bundle makes a fix look like it did not work.
    """
    response = await call_next(request)

    # A handler that set its own Cache-Control wins. Exactly one does -- the
    # card image -- and it must, because blanket no-store would defeat the
    # whole point of a content-addressed URL.
    #
    # THIS IS THE SUBTLE PART: a middleware that unconditionally overwrites a
    # header is invisible at the call site. The route below would look
    # completely correct, set `immutable`, and still ship `no-store` to the
    # browser, with nothing in the route's own file to explain why. Deferring
    # to the handler keeps the rule "no-store unless a route deliberately says
    # otherwise", which is the behaviour the docstring above already claims.
    handler_set_caching = "cache-control" in response.headers
    if (
        request.url.path.startswith("/api/") or request.url.path == "/health"
    ) and not handler_set_caching:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        # An ETag would let the browser revalidate and reuse the body; for
        # state that changes every second that is exactly what we do not want.
        # MutableHeaders has no .pop(), so delete it explicitly.
        if "etag" in response.headers:
            del response.headers["etag"]
    return response


# Serve the frontend from this same process.
#
# WHY NOT A SEPARATE FRONTEND SERVER: the page is three static files. Putting
# them behind their own host (S3/CloudFront/nginx) would add cost, a deploy
# step, and a CORS configuration -- in exchange for nothing, because the app
# server is idle-waiting on the model anyway. Same origin also means fetch()
# calls need no CORS headers at all.
STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"


class NoCacheStatic(StaticFiles):
    """
    Serve static files with caching disabled.

    Browsers cache JS and CSS aggressively, which during development produces
    one of the most wasteful debugging loops there is: you fix a bug, reload,
    see the OLD file, and conclude the fix did not work. Telling the browser
    not to store these removes that whole class of false signal.

    For a production deployment you would do the opposite -- cache hard and
    bust with a content hash in the filename -- but this app is served by the
    same process that does the work, the files are a few hundred kilobytes,
    and being able to trust a reload is worth more than the bytes.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")


def _strip_between(html: str, start: str, end: str) -> str:
    """Remove everything between two marker comments, inclusive."""
    head, marker, rest = html.partition(start)
    if not marker:
        return html
    _removed, marker, tail = rest.partition(end)
    return head + tail if marker else html


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    """
    Serve the single-page UI, with the sign-in screen removed when auth is off.

    WHY THIS IS DECIDED ON THE SERVER RATHER THAN IN THE BROWSER:

    The page used to always contain the gate and let JavaScript hide it after
    asking /api/auth-config. That put the decision behind two things that can
    go stale independently -- the cached script and the cached config response
    -- and when either did, the browser showed a sign-in screen for an app that
    has no sign-in, and no amount of restarting the server changed it because
    the server was never consulted.

    Not sending markup the user can never use removes that whole class of
    failure: there is no gate in the document to be shown by mistake, and the
    app is visible without JavaScript having to reveal it.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    if not settings.auth_enabled:
        html = _strip_between(html, "<!--GATE_START-->", "<!--GATE_END-->")
        html = _strip_between(html, "<!--AUTHJS_START-->", "<!--AUTHJS_END-->")
        # The app div is hidden by default so the gate can own the screen on
        # first paint. With no gate, it must start visible.
        html = html.replace('<div id="app" hidden>', '<div id="app">')

    return HTMLResponse(
        html, headers={"Cache-Control": "no-store, must-revalidate"}
    )


# Cached result of the model-endpoint probe: (monotonic_time, payload).
_model_probe: tuple[float, dict] | None = None
# How long a probe result is reused. Long enough that a load balancer polling
# every few seconds does not open a socket per poll; short enough that a dead
# backend shows up within a deploy's worth of time.
_MODEL_PROBE_TTL_SECONDS = 15.0
# A TCP handshake to a host that is up takes microseconds locally and low
# milliseconds across a VNet. Two seconds is already pathological, and the cap
# is what keeps /health's worst case bounded.
_MODEL_PROBE_TIMEOUT = 2.0


async def _probe_model_endpoint() -> dict:
    """
    Can we open a TCP connection to the model's host and port?

    THIS IS NOT AN INFERENCE CALL, AND THE DISTINCTION IS THE WHOLE POINT.
    /api/model-check sends a real 1x1 image through the model and takes as
    long as the model takes -- on this hardware, up to 170 seconds. Putting
    that in /health would mean a liveness probe that cannot finish inside any
    sane timeout, so the orchestrator would kill a perfectly healthy container
    because something DOWNSTREAM was slow. That turns a model slowdown into a
    restart loop, which is strictly worse than the original problem.

    A TCP connect answers the question that actually distinguishes the common
    failure -- "MODEL_URL points at nothing" -- in about a millisecond, and it
    cannot block for longer than the timeout above. What it deliberately does
    NOT tell you is whether the model can answer; that is /api/model-check's
    job, and it is a question you ask by hand.
    """
    global _model_probe

    now = time.monotonic()
    if _model_probe and (now - _model_probe[0]) < _MODEL_PROBE_TTL_SECONDS:
        return _model_probe[1]

    parsed = urllib.parse.urlparse(settings.model_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    result = {"host": host, "port": port}
    started = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_MODEL_PROBE_TIMEOUT
        )
        writer.close()
        # Suppressed: we only wanted the handshake, and a peer that resets the
        # connection during close has still proved it is listening.
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        result |= {"reachable": True}
    except (TimeoutError, asyncio.TimeoutError):
        result |= {"reachable": False, "error": "timed out opening a connection"}
    except OSError as exc:
        # Connection refused, DNS failure, no route. The usual and most useful
        # finding: the URL is wrong or the model is not running.
        result |= {"reachable": False, "error": str(exc)}
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)

    _model_probe = (now, result)
    return result


@app.get("/health")
async def health() -> dict:
    """
    Liveness + configuration + a cheap reachability check on the model endpoint.

    Still answers in milliseconds, still never runs inference. See
    _probe_model_endpoint for why the distinction matters, and note the result
    is cached so that a load balancer polling every second does not open a
    socket every second.
    """
    return {
        "status": "ok",
        "service": "card-reader",
        "model_endpoint": await _probe_model_endpoint(),
        # Reported because it depends on an optional wheel being installed:
        # if this is false on the AWS box, every iPhone upload will fail and
        # this is the fastest way to find out.
        "heic_supported": HEIC_SUPPORTED,
        "config": settings.as_public_dict(),
    }


@app.get("/api/auth-config")
def auth_config() -> dict:
    """
    What the browser needs to start Clerk. Public by necessity and by design.

    The publishable key is meant to ship to browsers -- that is what
    "publishable" means. Serving it from here rather than hardcoding it in
    index.html keeps the frontend free of environment-specific values, so the
    same static files work in dev and in production.
    """
    return {
        "auth_enabled": settings.auth_enabled,
        "publishable_key": settings.clerk_publishable_key,
        # Returned so the page can compare it against the `iss` claim in the
        # token it actually holds. A mismatch (a session left over from a
        # different Clerk instance) otherwise presents as an unexplained 401
        # loop: sign in, land back on the sign-in screen, repeat.
        "issuer": settings.clerk_issuer,
    }


@app.get("/api/model-check")
async def model_check() -> dict:
    """
    Actually call the model and report whether it answered.

    This is the question /health deliberately refuses to answer, and it gets
    its own endpoint precisely so /health can stay fast. Nothing polls this;
    you hit it by hand when something looks wrong, or once after a deploy to
    confirm MODEL_URL points somewhere real.

    Sends a 1x1 pixel so the round trip is as cheap as a round trip can be
    while still exercising the whole path: image encoding, the HTTP call, and
    the model's reply.
    """
    import io
    import time

    from PIL import Image

    from app.imaging import preprocess_to_data_url
    from app.model_client import ModelError, complete

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="JPEG")

    started = time.monotonic()
    try:
        reply = await complete(preprocess_to_data_url(buffer.getvalue()))
    except ModelError as exc:
        return {
            "reachable": False,
            "model_url": settings.model_url,
            "model_name": settings.model_name,
            "error": str(exc),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    return {
        "reachable": True,
        "model_url": settings.model_url,
        "model_name": settings.model_name,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "reply_preview": reply[:200],
    }


@app.post("/api/extract")
async def extract_single(
    file: UploadFile = File(...),
    user: User = Depends(require_user),
) -> dict:
    """
    Extract one lead from one image, synchronously.

    Kept alongside the bulk endpoint because it is the easiest thing to curl
    and the easiest thing to debug. One card is a few seconds, which is well
    inside any timeout, so a job is unnecessary ceremony here.

    Always returns HTTP 200 with a lead object: an unreadable card comes back
    as a blank row with status/error set, not as an error response.
    """
    raw_bytes = await file.read()
    if len(raw_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the "
                   f"{settings.max_upload_bytes // (1024 * 1024)} MB limit",
        )
    # retain_image=False: this route persists no lead row, so a retained image
    # would be referenced by nothing and would sit on disk until swept.
    lead = await extract_lead(
        file.filename or "upload", raw_bytes, retain_image=False
    )
    return lead.to_dict()


@app.post("/api/jobs", status_code=202)
async def create_job(
    files: list[UploadFile] = File(...),
    session_id: str | None = Form(default=None),
    user: User = Depends(require_user),
) -> JSONResponse:
    """
    Accept a bulk upload and start processing it in the background.

    Returns 202 Accepted -- "I have taken this work, it is not finished" --
    which is the correct status for an asynchronous job, rather than 200 ("here
    is your result") or 201 ("a resource now exists at this URL").

    The response is immediate regardless of batch size. Progress is polled from
    GET /api/jobs/{job_id}.
    """
    if not files:
        raise HTTPException(status_code=400, detail="no files uploaded")
    if len(files) > settings.max_files_per_request:
        # Reject the whole batch up front rather than accepting some and
        # silently dropping the rest -- a partial success the client did not
        # ask for is worse than a clear rejection.
        raise HTTPException(
            status_code=413,
            detail=f"too many files: {len(files)} "
                   f"(limit {settings.max_files_per_request} per request)",
        )

    batch_dir = make_batch_dir()
    accepted = []
    rejected = []
    try:
        for upload in files:
            try:
                accepted.append(await spool_upload(upload, batch_dir))
            except UploadTooLarge as exc:
                # One oversized file does not sink the batch. It is recorded
                # and reported; the rest still process.
                rejected.append(str(exc))
    except Exception:
        cleanup_batch_dir(batch_dir)
        raise

    if not accepted:
        cleanup_batch_dir(batch_dir)
        raise HTTPException(
            status_code=413, detail="; ".join(rejected) or "no usable files"
        )

    # SESSION RESOLUTION.
    #
    # An upload with no session starts one. That means the API stays usable
    # with a bare `curl -F files=@card.jpg` -- no ceremony, no "create a
    # session first" step -- while the UI, which always has a session open,
    # passes its id and appends to it.
    #
    # A session id that is not yours resolves to None and a NEW session is
    # created rather than raising. Erroring would confirm the id exists, which
    # is the same enumeration leak the 404-not-403 rule elsewhere avoids.
    session = None
    if session_id:
        session = await store.get_session(session_id, user.id)
    if session is None:
        session = await store.create_session(user.id)

    job = await store.create_job(
        total=len(accepted), user_id=user.id, session_id=session.id
    )
    schedule_job(job.id, accepted, batch_dir)

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.id,
            "session_id": session.id,
            "status": job.status,
            "accepted": len(accepted),
            "rejected": rejected,
            "poll": f"/api/jobs/{job.id}",
        },
    )


@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: str,
    summary: bool = False,
    user: User = Depends(require_user),
) -> dict:
    """
    Poll a job.

    `?summary=true` returns progress counters only, without the lead payload.
    A client polling once a second for a 1000-card job would otherwise re-fetch
    the entire growing result set every second.
    """
    job = await store.get_job(job_id)
    # 404 rather than 403 for someone else's job. A 403 would confirm that the
    # id exists, letting an attacker enumerate valid job ids; 404 tells them
    # nothing they did not already know. Same response either way.
    if job is None or not job.owned_by(user.id):
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return job.summary() if summary else job.to_dict()


# --------------------------------------------------------------------------
# Sessions
#
# A session is a named container for however many uploads it took to collect a
# set of cards -- the unit a person actually thinks in ("the cards from this
# conference"), and therefore the unit the spreadsheet should match. A job is
# one upload batch, so exporting per job hands someone who shot twelve cards in
# three goes three files to merge by hand.
# --------------------------------------------------------------------------

DEFAULT_SESSIONS_PAGE = 20
MAX_SESSIONS_PAGE = 100


@app.post("/api/sessions", status_code=201)
async def create_session(
    title: str | None = Form(default=None),
    user: User = Depends(require_user),
) -> dict:
    """Open a new, empty session. 201 because a resource now exists."""
    session = await store.create_session(user.id, title or "")
    return session.to_dict()


@app.get("/api/sessions")
async def list_sessions(
    limit: int = DEFAULT_SESSIONS_PAGE,
    cursor: str | None = None,
    user: User = Depends(require_user),
) -> dict:
    """That user's sessions, newest first, with rollup counts for the sidebar."""
    limit = max(1, min(limit, MAX_SESSIONS_PAGE))
    page = await store.list_sessions(user.id, limit=limit, cursor=cursor)
    return {
        "sessions": [session.to_dict() for session in page.sessions],
        "next_cursor": page.next_cursor,
    }


@app.get("/api/sessions/{session_id}")
async def get_session(
    session_id: str,
    user: User = Depends(require_user),
) -> dict:
    """
    One session with every job it contains and every lead in them.

    This is what the UI loads when a session is opened from the history
    sidebar: the whole conversation, in the order it happened.
    """
    session = await store.get_session(session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")

    jobs = await store.session_jobs(session_id, user.id)

    # The rollup is derived from the jobs already loaded rather than by asking
    # the store to aggregate again. list_sessions computes these in SQL because
    # it renders many sessions and must not load their leads; here the leads
    # are in hand for the response body anyway, so counting them is free and a
    # second query would be pure duplication -- and a chance for the two paths
    # to disagree.
    leads = [lead for job in jobs for lead in job.leads]
    session.job_count = len(jobs)
    session.card_count = len(leads)
    session.succeeded_count = sum(1 for lead in leads if lead.status == "ok")
    session.failed_count = sum(1 for lead in leads if lead.status != "ok")

    return {
        **session.to_dict(),
        "jobs_detail": [job.to_dict() for job in jobs],
    }


@app.get("/api/sessions/{session_id}/export.xlsx")
async def export_session(
    session_id: str,
    only_successful: bool = False,
    user: User = Depends(require_user),
) -> Response:
    """
    One spreadsheet for the whole session, however many uploads it took.

    THE FILE IS NOT STORED AND THEN AMENDED -- it is generated from the
    session's leads on every request. That is why adding cards to an open
    session "updates" the spreadsheet: there is no earlier file to go stale,
    so the download is always exactly the session's current contents. Keeping
    a materialised .xlsx on disk would mean a cache to invalidate, a partial
    file to serve during a rebuild, and a way for the two to disagree.
    """
    session = await store.get_session(session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
    leads = await store.session_leads(session_id, user.id)
    return _xlsx_response(_filter(leads, only_successful), f"leads-session-{session_id}")


@app.get("/api/leads/{lead_id}/image")
async def lead_image(
    lead_id: str,
    user: User = Depends(require_user),
) -> Response:
    """
    Serve the normalised card image for one lead.

    OWNERSHIP IS PART OF THE LOOKUP, NOT A CHECK AFTER IT. store.get_lead()
    takes the user id and resolves it in the same query, so there is no way to
    write this route such that it fetches first and authorises second. The
    other routes check `job.owned_by(user.id)` after fetching, which is correct
    but relies on the author remembering; this one cannot be written wrongly.

    404 FOR ALL FOUR FAILURES -- unknown id, someone else's lead, never
    retained, and expired. Distinguishing them would leak exactly what the
    random ids exist to hide: "403" on a valid id confirms the id is valid.
    The client has one thing to do in every case (show no image), so one
    status is also the honest answer, not merely the cautious one.
    """
    lead = await store.get_lead(lead_id, user.id)
    if lead is None or not lead.image_sha256:
        raise HTTPException(status_code=404, detail="no image for this lead")

    data = images.read(lead.image_sha256)
    if data is None:
        # The row points at a digest whose file is gone: retention swept it,
        # or the volume was replaced. Not an error -- an expected end state.
        raise HTTPException(status_code=404, detail="no image for this lead")

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            # PRIVATE, NOT PUBLIC, AND THE DIFFERENCE IS A DATA BREACH.
            # "public" permits any shared cache on the path -- a corporate
            # proxy, a CDN -- to store the response and serve it to a
            # DIFFERENT user who requests the same URL. These are photographs
            # of named people's contact details, scoped to one account.
            # "private" restricts caching to the requesting browser.
            #
            # immutable + a year is safe here only because the URL is
            # content-addressed: the bytes behind a digest cannot change, so
            # there is no stale version to worry about. On a mutable URL this
            # header would be a bug that takes a year to expire.
            "Cache-Control": "private, max-age=31536000, immutable",
            # Display in place rather than prompting a download. The filename
            # is ours, never the client's -- source_filename is
            # attacker-controlled and has no business in a response header.
            "Content-Disposition": f'inline; filename="{lead.image_sha256[:12]}.jpg"',
            # The bytes are a JPEG we re-encoded ourselves, but this response
            # is reached by user-supplied id, so pin the type: it stops a
            # browser from content-sniffing its way to treating the body as
            # anything other than an image.
            "X-Content-Type-Options": "nosniff",
        },
    )


# Default page size for the history sidebar. Enough to fill a tall screen
# without the first paint waiting on a hundred rows; the client asks for more
# as the user scrolls.
DEFAULT_JOBS_PAGE = 20
MAX_JOBS_PAGE = 100


@app.get("/api/jobs")
async def list_jobs(
    limit: int = DEFAULT_JOBS_PAGE,
    cursor: str | None = None,
    user: User = Depends(require_user),
) -> dict:
    """
    That user's jobs, newest first, as cheap summaries.

    Cursor-paginated rather than offset-paginated. An offset makes the
    database count and discard rows it will not return, so deep pages get
    slower, and it is computed against a list that MOVES -- finishing a job
    while someone is on page 2 shifts every row down one, so page 3 repeats a
    row and hides another. A cursor names a position, so rows appearing above
    it change nothing below.

    `limit` is clamped rather than rejected: a client asking for 10,000 rows
    has made a mistake, not an attack, and the useful response is the largest
    page we are willing to serve.
    """
    limit = max(1, min(limit, MAX_JOBS_PAGE))
    page = await store.list_jobs(user.id, limit=limit, cursor=cursor)
    return {
        "jobs": [job.summary() for job in page.jobs],
        # Explicitly null on the last page, so the client has a single
        # unambiguous stop condition rather than inferring from a short page.
        "next_cursor": page.next_cursor,
    }


# --------------------------------------------------------------------------
# Excel export
# --------------------------------------------------------------------------

# The official MIME type for .xlsx. Getting this wrong is why a download
# sometimes arrives as "download.zip" -- an xlsx IS a zip archive, so browsers
# that fall back to sniffing the bytes label it as one.
XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _xlsx_response(leads: list[Lead], prefix: str) -> Response:
    """Wrap workbook bytes in a download response."""
    data = build_workbook(leads, sheet_title="Leads")
    filename = export_filename(prefix)
    return Response(
        content=data,
        media_type=XLSX_MEDIA_TYPE,
        # "attachment" makes the browser save the file instead of trying to
        # display it; filename= is what it gets called on disk.
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _filter(leads: list[Lead], only_successful: bool) -> list[Lead]:
    return [lead for lead in leads if lead.status == "ok"] if only_successful else leads


@app.get("/api/jobs/{job_id}/export.xlsx")
async def export_job(
    job_id: str,
    only_successful: bool = False,
    user: User = Depends(require_user),
) -> Response:
    """
    Download one job's leads as a spreadsheet.

    Flagged rows are INCLUDED by default, tinted, with their status and the
    failure reason in the last two columns. A spreadsheet that silently omits
    3 of your 14 cards is dangerous: you would never know to re-shoot those
    three. Pass ?only_successful=true for a clean file to import into a CRM.
    """
    job = await store.get_job(job_id)
    if job is None or not job.owned_by(user.id):
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return _xlsx_response(_filter(job.leads, only_successful), f"leads-{job_id}")


@app.get("/api/export.xlsx")
async def export_all(
    only_successful: bool = False,
    user: User = Depends(require_user),
) -> Response:
    """Download every lead this user owns, in one sheet."""
    leads = await store.all_leads(user.id)
    return _xlsx_response(_filter(leads, only_successful), "leads-all")
