"""
The background worker that processes a bulk upload.

WHY A JOB QUEUE AT ALL (the core scaling decision):

A CPU-hosted Qwen2.5-VL-3B takes 136-172 seconds per card -- MEASURED on the
target box, not estimated. So a 20-card batch is roughly 50 minutes of work. You cannot hold an HTTP request open
for that:
  * browsers abandon fetches,
  * nginx's default proxy_read_timeout is 60s,
  * an AWS ALB's default idle timeout is 60s.
The connection dies minutes in and every completed result is lost, because it
only ever existed in that request's memory.

So POST returns 202 + a job_id immediately, this worker runs detached, and the
client polls. The user gets a progress bar instead of a spinner that dies.

Deliberately, this is an IN-PROCESS asyncio queue -- no Redis, no Celery, no
SQS. For a single-instance deployment that is the right trade: it adds zero
infrastructure and zero cost. Its limits are honest and documented in the
README: jobs are lost on restart, and it does not span multiple instances.
Both are fixed by the same swap -- a real queue plus the database seam in
store.py -- if this ever needs to scale horizontally.

MEMORY SAFETY:
The semaphore below is what makes 1000 uploads survivable. Image decode costs
~100 MB RSS per image in flight, so peak memory is
    max_concurrency * ~100 MB
regardless of batch size. Files wait on DISK, and each is read, processed and
deleted one at a time. Peak RAM is flat in the number of uploads.
"""

import asyncio
import os

import httpx

from app.config import settings
from app.extraction import extract_lead
from app.schema import Lead
from app.store import store
from app.uploads import SpooledUpload, cleanup_batch_dir

# asyncio only holds a WEAK reference to tasks created with create_task, so a
# task with no other reference can be garbage-collected mid-flight. Keeping the
# set is the documented way to prevent that.
_running: set[asyncio.Task] = set()


async def _process_one(
    upload: SpooledUpload,
    job_id: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> None:
    """Handle a single card, then release its disk space immediately."""
    async with semaphore:
        try:
            # Read the bytes only now, inside the semaphore, so at most
            # max_concurrency images are resident at any moment.
            with open(upload.path, "rb") as handle:
                raw_bytes = handle.read()
            lead = await extract_lead(upload.display_name, raw_bytes, client=client)
        except OSError as exc:
            lead = Lead(
                source_filename=upload.display_name,
                status="model_error",
                error=f"could not read spooled upload: {exc}",
            )
        finally:
            # Delete as we go rather than at the end, so a 50-file batch does
            # not hold 750 MB of disk for its whole run.
            try:
                os.remove(upload.path)
            except OSError:
                pass

        # Append per card, not in one batch at the end: this is what lets the
        # UI fill in progressively, and what stops a failure at card 49 from
        # discarding the first 48.
        await store.append_lead(job_id, lead)


async def run_job(job_id: str, uploads: list[SpooledUpload], batch_dir: str) -> None:
    """
    Process every upload in a job. Never raises -- it is a detached task, so an
    escaping exception would vanish into the event loop and leave the job stuck
    on "running" forever.
    """
    try:
        await store.set_status(job_id, "running")
        semaphore = asyncio.Semaphore(settings.max_concurrency)

        # One client for the whole batch: reusing the connection pool avoids a
        # fresh TCP handshake per card.
        async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
            await asyncio.gather(
                *(
                    _process_one(upload, job_id, client, semaphore)
                    for upload in uploads
                )
            )
        await store.set_status(job_id, "done")
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all, see docstring
        await store.set_status(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        cleanup_batch_dir(batch_dir)


def schedule_job(job_id: str, uploads: list[SpooledUpload], batch_dir: str) -> None:
    """Fire the worker off into the background and return immediately."""
    task = asyncio.create_task(run_job(job_id, uploads, batch_dir))
    _running.add(task)
    task.add_done_callback(_running.discard)
