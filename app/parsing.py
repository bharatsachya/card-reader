"""
Turning the model's text reply into a dict, without ever crashing.

The contract: parse_lead_json() ALWAYS returns. Either a dict of fields, or
None with a reason. It never raises. That is what lets a malformed reply become
a flagged blank row instead of an HTTP 500.

Observed real-world failure modes this handles, in the order we try them:
  1. Clean JSON                     -> {"first_name": "Asha", ...}
  2. Markdown fenced                -> ```json\n{...}\n```
  3. Prose wrapper                  -> Here is the JSON:\n{...}\nLet me know...
  4. Trailing commas                -> {"a": 1,}          (valid JS, invalid JSON)
  5. Python literals                -> {"a": None}        (model imitating Python)
  6. Single quotes                  -> {'a': 'b'}
  7. Truncated / total garbage      -> None + reason
"""

import json
import re
from typing import Any, Optional

from app.schema import LEAD_FIELDS

# Matches ```json ... ``` or plain ``` ... ``` fences, capturing the contents.
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _strip_fences(text: str) -> str:
    """Return the contents of the first markdown code fence, if there is one."""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _find_json_object(text: str) -> Optional[str]:
    """
    Extract the first balanced {...} block from arbitrary text.

    Why brace-counting rather than a regex: regexes cannot match nested
    brackets, and a naive `text[text.find('{'):text.rfind('}')+1]` breaks when
    the model emits prose containing a stray brace. This walks the string,
    tracks depth, and correctly ignores braces inside string literals and
    escaped quotes.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False          # this char was escaped; consume it
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    # Ran off the end with depth > 0 -> the reply was truncated mid-object.
    return None


def _repair(candidate: str) -> str:
    """
    Last-resort textual fixes for near-JSON.

    Applied only AFTER strict json.loads has already failed, so well-formed
    input is never touched by these heuristics.
    """
    repaired = candidate
    # Python/JS literals the model sometimes emits instead of JSON ones.
    # Word boundaries prevent mangling the string "Nonesuch Ltd".
    repaired = re.sub(r"\bNone\b", "null", repaired)
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)
    # Trailing comma before a closing brace/bracket: {"a": 1,}
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    return repaired


def _coerce_scalar(value: Any) -> Optional[str]:
    """
    Force any model-supplied value into "clean string or None".

    Models return surprising types for these fields: a phone as an int, a
    location as a ["Mumbai", "India"] list, a nested {"city": ...} dict, or the
    literal strings "null"/"N/A" instead of JSON null. Normalising here means
    every downstream layer can assume Optional[str] and nothing else.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None                                   # never meaningful here
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_coerce_scalar(v) for v in value]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    if isinstance(value, dict):
        parts = [_coerce_scalar(v) for v in value.values()]
        joined = ", ".join(p for p in parts if p)
        return joined or None

    text = str(value).strip()
    # Placeholder strings that mean "absent". Compared case-insensitively.
    if text.lower() in {"", "null", "none", "n/a", "na", "not available",
                        "not provided", "unknown", "-", "--"}:
        return None
    return text


def normalise_fields(data: dict) -> dict:
    """
    Project an arbitrary parsed dict onto exactly LEAD_FIELDS.

    Guarantees: every expected key is present, extra keys the model invented
    are dropped, and every value is Optional[str]. Key matching is
    case/format-insensitive so "First Name", "firstName" and "first_name" all
    land in the same slot -- models are inconsistent about this.
    """
    lookup = {
        re.sub(r"[^a-z0-9]", "", str(key).lower()): value
        for key, value in data.items()
    }
    result = {}
    for expected in LEAD_FIELDS:
        flat = expected.replace("_", "")
        result[expected] = _coerce_scalar(lookup.get(flat))
    return result


def parse_lead_json(raw_text: str) -> tuple[Optional[dict], Optional[str]]:
    """
    Parse the model's reply into normalised fields.

    Returns (fields, None) on success, or (None, reason) on failure.
    NEVER raises -- that is the whole point of this module.
    """
    if not raw_text or not raw_text.strip():
        return None, "model returned an empty reply"

    stripped = _strip_fences(raw_text)

    # Try, in increasing order of desperation. The first success wins.
    candidates: list[str] = [stripped]
    extracted = _find_json_object(stripped)
    if extracted and extracted != stripped:
        candidates.append(extracted)
    candidates.extend(_repair(c) for c in list(candidates))

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        # A bare list is occasionally returned for a single card; unwrap it.
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        if isinstance(data, dict):
            return normalise_fields(data), None

    preview = raw_text.strip().replace("\n", " ")[:200]
    return None, f"could not parse JSON from model reply: {preview!r}"
