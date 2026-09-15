"""
Receiving uploaded files safely.

Two jobs, both about not trusting the client:

1. SPOOL TO DISK, NOT RAM, WITH THE LIMIT ENFORCED *DURING* THE STREAM.
   The naive `raw = await file.read()` pulls an entire file into memory before
   you can check its size -- so a 2 GB upload is already resident by the time
   you could reject it. Reading in fixed chunks and counting as we go means an
   oversized file is aborted after ~1 MB, and a 50-file batch never costs more
   than one chunk of RAM at a time.

2. NEVER TRUST THE CLIENT-SUPPLIED FILENAME.
   `file.filename` is attacker-controlled. A filename of "../../../etc/cron.d/x"
   would, with naive path joining, write outside the upload directory -- a
   path-traversal vulnerability. We therefore generate our own random on-disk
   name and keep the original only as a display label that is never used to
   build a path.
"""

import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass

from fastapi import UploadFile

from app.config import settings

# Read in 1 MB chunks: big enough to be efficient, small enough that the
# over-size check fires almost immediately on a huge upload.
CHUNK_SIZE = 1024 * 1024


class UploadTooLarge(Exception):
    """A single file exceeded max_upload_bytes."""


@dataclass
class SpooledUpload:
    """An accepted upload, waiting on disk for the worker."""

    path: str            # our generated path -- safe, never client-derived
    display_name: str    # the client's filename, for display ONLY
    size_bytes: int


def make_batch_dir() -> str:
    """Create an isolated directory for one job's uploads."""
    parent = settings.upload_dir or None
    if parent:
        os.makedirs(parent, exist_ok=True)
    return tempfile.mkdtemp(prefix="cardreader-", dir=parent)


def cleanup_batch_dir(path: str) -> None:
    """Delete a job's upload directory. Safe to call twice."""
    shutil.rmtree(path, ignore_errors=True)


async def spool_upload(file: UploadFile, batch_dir: str) -> SpooledUpload:
    """
    Stream one upload to disk, enforcing the size cap as we read.

    Raises UploadTooLarge if the file exceeds the limit; the partial file is
    removed so a rejected upload leaves nothing behind.
    """
    display_name = os.path.basename(file.filename or "upload")
    # Our own name. The client's filename never touches the filesystem path.
    dest = os.path.join(batch_dir, f"{uuid.uuid4().hex}.bin")

    size = 0
    limit = settings.max_upload_bytes
    try:
        with open(dest, "wb") as handle:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise UploadTooLarge(
                        f"{display_name} exceeds the {limit // (1024 * 1024)} MB limit"
                    )
                handle.write(chunk)
    except UploadTooLarge:
        # Do not leave a partial file on disk after a rejection.
        try:
            os.remove(dest)
        except OSError:
            pass
        raise

    return SpooledUpload(path=dest, display_name=display_name, size_bytes=size)
