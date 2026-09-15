"""
Deterministic clean-up applied AFTER the model, before storage.

Guiding principle from the brief: DO NOT TRUST THE MODEL'S FORMATTING.

The model is good at *finding* the phone number on the card. It is unreliable
at *formatting* it: the same number may come back as "98200 12345",
"+91-98200-12345", "(982) 001-2345" or "091 98200 12345" depending on how it
was printed and how the model felt that run. Formatting is a solved,
rule-based problem, so we solve it with a library instead of asking the model
to -- code gives the same answer every time, and a wrong answer is debuggable.

This also keeps the export clean: a spreadsheet of leads where every phone is
E164 can be dialled, deduplicated and imported into a CRM. A mixed-format
column cannot.
"""

import re
from typing import Optional

import phonenumbers

from app.config import settings
from app.schema import LEAD_FIELDS

# Deliberately permissive. This is a sanity filter to reject obvious junk like
# "email not printed", not an RFC 5322 validator -- over-strict validation
# would silently drop legitimate unusual addresses.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def clean_text(value: Optional[str]) -> Optional[str]:
    """Collapse whitespace; turn anything that ends up empty into None."""
    if value is None:
        return None
    # Business card OCR commonly yields non-breaking spaces and newlines.
    collapsed = re.sub(r"\s+", " ", str(value).replace(" ", " ")).strip()
    return collapsed or None


def normalise_email(value: Optional[str]) -> Optional[str]:
    """
    Lowercase and validate an email address.

    Lowercasing is safe and useful: the domain half is case-insensitive by
    spec, and no real-world mail provider treats the local part as
    case-sensitive. Lowercasing makes deduplication work.
    """
    text = clean_text(value)
    if text is None:
        return None
    # Lowercase FIRST, so the case-insensitive fixes below only need one form.
    # (Doing this after the mailto: strip would let "MAILTO:" survive.)
    text = text.lower()
    # Models sometimes prefix the mailto: scheme copied from a card's QR code.
    text = text.removeprefix("mailto:").strip()
    # Cards print "name (at) company.com" to dodge scrapers; models copy it.
    text = re.sub(r"\s*\(\s*at\s*\)\s*", "@", text)
    # Strip trailing punctuation picked up from the card layout.
    text = text.rstrip(".,;:")
    return text if _EMAIL_RE.match(text) else None


def normalise_phone(value: Optional[str]) -> Optional[str]:
    """
    Parse a phone number and return it in E164 form, e.g. "+919820012345".

    E164 is the international standard: country code + number, no spaces or
    punctuation. It is unambiguous, globally dialable and comparable as a
    string -- which is exactly what a CRM import needs.

    `default_phone_region` (IN by default, configurable) tells the parser which
    country to assume when the card prints a number with no "+countrycode" --
    which most domestic cards do. Without a region hint, "98200 12345" is
    unparseable, because the same digits are a valid number in many countries.
    """
    text = clean_text(value)
    if text is None:
        return None

    # A card may print several numbers in one line: "+91 22 1234 5678 / 98200 12345".
    # Split on common separators and take the first candidate that parses.
    candidates = [c.strip() for c in re.split(r"[/|]|\s{2,}|,", text) if c.strip()]
    if not candidates:
        candidates = [text]

    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, settings.default_phone_region)
        except phonenumbers.NumberParseException:
            continue
        # is_valid_number checks the number against the country's real
        # numbering plan (correct length, valid prefix) -- not just "looks
        # like digits". This is what rejects a fax extension or a stray
        # postcode the model mistook for a phone number.
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )

    # Nothing validated. Keep the model's raw text rather than discarding it:
    # a human reading the spreadsheet can still use "ext. 402", and silently
    # dropping data the model correctly read would be worse than leaving it
    # unformatted. The README notes this as a deliberate choice.
    return text


def postprocess_fields(fields: dict) -> dict:
    """Apply the right normaliser to each field. Pure function, easy to test."""
    cleaned = {}
    for name in LEAD_FIELDS:
        value = fields.get(name)
        if name == "email":
            cleaned[name] = normalise_email(value)
        elif name == "phone":
            cleaned[name] = normalise_phone(value)
        else:
            cleaned[name] = clean_text(value)
    return cleaned
