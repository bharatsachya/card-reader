"""
Orchestration: one uploaded file in, one Lead out.

This module owns the ERROR POLICY, and it is the reason the app never returns
a 500 for a bad card. Every failure is caught and converted into a Lead with a
status and a message. A batch of 20 cards where 3 fail must still return 20
rows -- losing the other 17 to one exception would be the worst outcome.

The three statuses map to genuinely different causes:
  ok           - parsed cleanly
  parse_error  - the model replied, but we could not get JSON out of it
                 (per-card; other cards in the batch will probably succeed)
  model_error  - the call itself failed: unreachable, timeout, HTTP error
                 (infrastructure; probably affects every card in the batch)
"""

import asyncio

import httpx

from app.config import settings
from app.imaging import ImageProcessingError, preprocess_to_data_url
from app.model_client import ModelError, complete
from app.parsing import parse_lead_json
from app.postprocess import postprocess_fields
from app.schema import Lead


async def _complete_with_retry(
    data_url: str, client: httpx.AsyncClient | None
) -> str:
    """
    Call the model, retrying a few times on failure with exponential backoff.

    WHY RETRY AT ALL: the common failures here are transient. llama.cpp
    briefly refusing connections while it loads a model, a socket reset, a
    request that lost a race for the single inference slot. Turning a
    one-second blip into a permanently blank row -- when the user waited
    twenty minutes for the batch -- is a bad trade.

    WHY BACKOFF: retrying instantly hammers a server that is already
    struggling. Each wait doubles (1s, 2s, 4s), giving a loading model time to
    finish coming up.

    WHY NOT RETRY FOREVER: a genuinely-down model would make a 50-card batch
    take hours to fail. Three attempts bounds the worst case while still
    covering every realistic blip.
    """
    attempts = max(1, settings.model_max_attempts)
    last_error: ModelError | None = None

    for attempt in range(attempts):
        try:
            return await complete(data_url, client=client)
        except ModelError as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            await asyncio.sleep(settings.model_retry_base_seconds * (2 ** attempt))

    raise ModelError(f"{last_error} (after {attempts} attempts)")


async def extract_lead(
    filename: str,
    raw_bytes: bytes,
    client: httpx.AsyncClient | None = None,
) -> Lead:
    """
    Run the full pipeline for a single image. Never raises.

    `client` is passed through so a bulk upload can share one connection pool.
    """
    # --- 0. Reject obviously-unusable input ---------------------------------
    # A zero-byte file is the classic result of a failed drag-and-drop or an
    # interrupted upload. Naming it precisely is far more useful to the user
    # than "cannot identify image file".
    if not raw_bytes:
        return Lead(
            source_filename=filename,
            status="input_error",
            error="the file was empty (0 bytes)",
        )

    # --- 1. Preprocess (deterministic) --------------------------------------
    try:
        data_url = preprocess_to_data_url(raw_bytes)
    except ImageProcessingError as exc:
        # A non-image upload (PDF, .docx, corrupt file) is a user error, not a
        # server error, and NOT a model error -- the model was never called.
        # Its own status, because the user's fix is different: re-shoot or
        # re-export the file rather than retry the server.
        return Lead(
            source_filename=filename,
            status="input_error",
            error=f"could not read image: {exc}",
        )

    # --- 2. Call the model (NON-deterministic) ------------------------------
    try:
        raw_reply = await _complete_with_retry(data_url, client=client)
    except ModelError as exc:
        return Lead(source_filename=filename, status="model_error", error=str(exc))

    # --- 3. Parse (defensive) -----------------------------------------------
    fields, parse_error = parse_lead_json(raw_reply)
    if fields is None:
        # The blank-but-flagged row the brief asks for: the user sees WHICH
        # card failed and can re-upload it, instead of a silent gap.
        return Lead(
            source_filename=filename,
            status="parse_error",
            error=parse_error,
            raw_output=raw_reply[:1000],   # truncated; kept for debugging
        )

    # --- 4. Post-process (deterministic) ------------------------------------
    cleaned = postprocess_fields(fields)

    # --- 5. Build the Lead ---------------------------------------------------
    lead = Lead(source_filename=filename, status="ok", **cleaned)
    if lead.is_empty:
        # Valid JSON, but every field null: an unreadable or non-card image.
        # Technically a success, but useless to the user, so flag it.
        lead.status = "empty"
        lead.error = "model returned no fields for this image"
    return lead
