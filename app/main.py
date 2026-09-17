"""
FastAPI application entrypoint.

Run it with:  uvicorn app.main:app --reload --port 8000
"app.main" is the module path, ":app" is the variable below that uvicorn serves.
"""

import pathlib

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi import Depends, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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

app = FastAPI(
    title="Business Card Lead Extractor",
    description="Extracts structured leads from business card images using a "
                "self-hosted vision-language model.",
    version="0.3.0",
)


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


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the single-page UI."""
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/health")
def health() -> dict:
    """
    Liveness + configuration check.

    Deliberately does NOT call the model. A health endpoint should answer
    "is this web process up?" in milliseconds, because load balancers poll it
    every few seconds. Calling a CPU-hosted model here would make it take
    30+ seconds and the orchestrator would kill a healthy container.
    """
    return {
        "status": "ok",
        "service": "card-reader",
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
    lead = await extract_lead(file.filename or "upload", raw_bytes)
    return lead.to_dict()


@app.post("/api/jobs", status_code=202)
async def create_job(
    files: list[UploadFile] = File(...),
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

    job = await store.create_job(total=len(accepted), user_id=user.id)
    schedule_job(job.id, accepted, batch_dir)

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.id,
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


@app.get("/api/jobs")
async def list_jobs(user: User = Depends(require_user)) -> dict:
    """That user's jobs, newest first, as cheap summaries."""
    jobs = await store.list_jobs(user.id)
    return {"jobs": [job.summary() for job in jobs]}


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
