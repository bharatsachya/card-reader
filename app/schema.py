"""
The canonical shape of a lead.

Defined in ONE place so the prompt, the parser, the API response, and the
Excel export can never drift out of sync. If a field is added later, it is
added here and every layer picks it up.
"""

from dataclasses import dataclass, asdict
from typing import Optional

# The seven fields we ask the model for, in display order.
# The prompt is generated from this list, the Excel columns are generated from
# this list, and the parser validates against this list.
LEAD_FIELDS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "title",
    "company",
    "location",
    "phone",
    "email",
)


@dataclass
class Lead:
    """
    One extracted business card.

    Every model-derived field is Optional: a business card legitimately may not
    have a job title, and a failed extraction must still produce a row rather
    than an exception. "Blank" is a valid, expressible state.
    """

    # --- Fields the model fills in ---
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None

    # --- Provenance / diagnostics (not model output) ---

    # Assigned by the store when the lead is recorded, not by the pipeline --
    # it identifies a stored row, and until it is stored there is nothing to
    # identify. None for a lead from /api/extract, which persists nothing.
    #
    # RANDOM, NOT SEQUENTIAL. This id appears in a URL that serves a
    # photograph of a named person's business card. An incrementing integer
    # would let anyone walk 1, 2, 3 and enumerate every card image on the
    # server; the ownership check would stop them, but a single missed check
    # would then expose everything rather than one row. The API already
    # returns 404 instead of 403 for another user's job so that ids cannot be
    # probed -- sequential lead ids would hand that back for free.
    id: Optional[str] = None

    source_filename: str = ""

    # sha256 of the NORMALISED JPEG, which is also its filename on disk.
    # None means no image was retained, or it has since expired -- both are
    # ordinary states the UI must handle rather than errors.
    image_sha256: Optional[str] = None
    # "ok"          -> model returned parseable JSON
    # "empty"       -> valid JSON, but every field was null
    # "parse_error" -> model replied, but we could not get JSON out of it
    # "model_error" -> the model call itself failed (timeout, 500, no server)
    # "input_error" -> the upload was never usable: not an image, corrupt,
    #                  zero bytes, or a decompression bomb. The model was
    #                  never called, so blaming the model would be a lie --
    #                  and the user's fix is different (re-shoot the photo,
    #                  not retry the server).
    status: str = "ok"
    # Human-readable reason when status != "ok". Surfaced in the UI so a user
    # can see WHICH card failed and why, instead of silently losing a row.
    error: Optional[str] = None
    # The model's raw reply, kept only when parsing failed. Invaluable for
    # debugging prompt/model issues; omitted on success to keep payloads small.
    raw_output: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_empty(self) -> bool:
        """True when the model found nothing at all (used for UI flagging)."""
        return all(getattr(self, f) is None for f in LEAD_FIELDS)
