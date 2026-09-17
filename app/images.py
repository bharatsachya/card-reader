"""
Content-addressed storage for the normalised card images.

WHAT IS KEPT, AND WHAT IS NOT.
The original upload is deleted the moment it has been read -- it is the large,
untrusted, arbitrary-format thing (a 12 MP HEIC, possibly a decompression
bomb). What is retained is the NORMALISED JPEG: EXIF-rotated, downscaled to
1024px, re-encoded. That is 50-260 KB instead of up to 15 MB, it is a format
every browser can display without a plugin, and -- the part that matters --
it is byte-for-byte the image the model was shown. When someone asks "why did
it read the phone number wrong?", the answer has to be the exact evidence the
model saw, not a re-derivation that might differ.

WHY CONTENT-ADDRESSED (the filename IS the sha256 of the contents).

  1. DEDUPLICATION IS AUTOMATIC. Re-shooting a card whose extraction failed is
     the single most common thing a user does. Identical bytes produce an
     identical hash, so the second upload writes nothing and both leads point
     at one file. With a random filename per lead you would store the same
     image as many times as it was uploaded.

  2. THE URL BECOMES IMMUTABLE, WHICH IS WHAT MAKES CACHING SAFE. The contents
     of /api/leads/<id>/image can never change, because changing the bytes
     would change the hash and therefore be a different file. That is the
     precise condition `Cache-Control: immutable` requires. A mutable URL
     cached for a year is a bug that takes a year to expire.

  3. CORRUPTION IS DETECTABLE. The name is a checksum, so verifying a file is
     re-hashing it. Nothing else needs to be stored to know a file is intact.

WHY THE TWO-CHARACTER SUBDIRECTORY (data/images/ab/abcdef...jpg).
Every file in one directory is fine at a hundred and unpleasant at a hundred
thousand: directory lookups degrade, `ls` becomes unusable, and backup tools
slow sharply. Splitting on the first two hex characters spreads files over 256
directories -- the convention git itself uses for the object store, for the
same reason. At the measured ceiling of ~508 cards/day, 30 days is ~15,000
files, or ~60 per directory.
"""

import hashlib
import os
import pathlib

from app.config import settings


def _root() -> pathlib.Path:
    return pathlib.Path(settings.image_dir).expanduser()


def path_for(digest: str) -> pathlib.Path:
    """Where the image with this sha256 lives. Pure; touches no disk."""
    return _root() / digest[:2] / f"{digest}.jpg"


def put(jpeg_bytes: bytes) -> str:
    """
    Store normalised JPEG bytes. Returns the sha256 hex digest.

    Idempotent: storing bytes that are already present is a stat() and nothing
    else, which is the deduplication described above.
    """
    digest = hashlib.sha256(jpeg_bytes).hexdigest()
    destination = path_for(digest)

    if destination.exists():
        # Already stored. Content-addressing means "same name" is a proof of
        # "same bytes", not an assumption -- so there is nothing to compare
        # and nothing to overwrite.
        return digest

    destination.parent.mkdir(parents=True, exist_ok=True)

    # WRITE TO A TEMPORARY NAME, THEN RENAME.
    #
    # Writing straight to the final path means a crash, a full disk or a
    # container eviction mid-write leaves a TRUNCATED file sitting at a name
    # that claims to be the sha256 of complete contents. Nothing would ever
    # detect it: put() would see the path exists and skip the write forever,
    # so every future upload of that card would be silently associated with a
    # half-written JPEG.
    #
    # os.replace() is atomic within a filesystem, so the final path only ever
    # appears once the bytes are all there. The pid suffix keeps two processes
    # writing the same image from colliding on the temp name.
    temporary = destination.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(jpeg_bytes)
        os.replace(temporary, destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise

    return digest


def read(digest: str) -> bytes | None:
    """The stored bytes, or None if the file is gone (expired or swept)."""
    try:
        return path_for(digest).read_bytes()
    except OSError:
        # Covers "not found" and "unreadable" alike. A missing image is an
        # ordinary, expected state -- retention deletes images on purpose --
        # so it is a None to be handled, never an exception to propagate.
        return None


def sweep(referenced: set[str]) -> tuple[int, int]:
    """
    Delete every stored file whose digest is not in `referenced`.

    Returns (files_deleted, bytes_reclaimed).

    THE ORDER OF OPERATIONS MATTERS AND IS THE CALLER'S RESPONSIBILITY.
    `referenced` must be read from the database AFTER the age-based expiry has
    cleared old rows, and this sweep must run after that read. Reversing them
    would delete a file whose lead row was written in between -- and because
    the store deduplicates, that file might be the only copy shared by several
    leads that are not expired at all.

    Doing it in this direction is safe in the other failure case too: a file
    that exists but is referenced by nothing is merely wasted disk, and the
    next sweep catches it. Deleting a referenced file is unrecoverable. When
    an operation is asymmetric like that, err toward the recoverable side.
    """
    root = _root()
    if not root.exists():
        return (0, 0)

    deleted = reclaimed = 0
    for shard in root.iterdir():
        if not shard.is_dir():
            continue
        for entry in shard.iterdir():
            if entry.suffix != ".jpg":
                # Leave anything unrecognised alone. A *.tmp from a process
                # that died mid-write is the expected case; deleting files we
                # do not understand from a directory that might be a
                # misconfigured IMAGE_DIR is how a sweep becomes a disaster.
                continue
            if entry.stem in referenced:
                continue
            try:
                size = entry.stat().st_size
                entry.unlink()
            except OSError:
                # Another process got there first, or permissions changed.
                # A sweep is housekeeping; it must never be the reason the
                # application fails to start.
                continue
            deleted += 1
            reclaimed += size

    return (deleted, reclaimed)
