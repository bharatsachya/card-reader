"""
The extraction prompt.

DESIGN DECISION: portable prompting over server-side constrained decoding.

Several inference servers can *guarantee* valid JSON -- Ollama has a `format`
parameter, llama.cpp has GBNF grammars, vLLM has guided_json. Any of them would
remove malformed output entirely. We deliberately use NONE of them, because
each is spelled differently on each server, and depending on one would hardcode
a model/server assumption into app logic -- exactly what the brief forbids.

Instead we do the portable thing that works on every OpenAI-compatible server:
a strict system prompt, temperature 0, and a defensive parser (parsing.py) that
assumes the model will sometimes misbehave. The trade-off is explicit: we accept
occasional malformed output and handle it, in exchange for the AWS/llama.cpp
migration being a two-env-var change.
"""

from app.schema import LEAD_FIELDS

# Built from LEAD_FIELDS so the prompt can never list a different set of keys
# than the parser and the spreadsheet expect.
_FIELD_LIST = ", ".join(LEAD_FIELDS)

SYSTEM_PROMPT = f"""You are a precise information extraction system.
You are shown a photograph of a single business card.
Extract the contact details and return them as one JSON object.

Return ONLY a JSON object containing exactly these keys:
{_FIELD_LIST}

Rules:
- Output raw JSON only. No markdown, no code fences, no commentary, no explanation.
- If a field does not appear on the card, set it to null. Never guess, infer, or invent a value.
- Copy text exactly as printed. Do not translate, expand abbreviations, or reformat.
- first_name / last_name: split the person's personal name. Drop honorifics (Mr, Ms, Dr).
  If only one name is printed, put it in first_name and set last_name to null.
- title: the person's job title or role, e.g. "Senior Sales Manager".
- company: the organisation name. Ignore taglines and slogans.
- location: the city, or the city and region, as printed on the card.
- phone: the primary phone number, exactly as printed. If several are printed,
  prefer the mobile/cell number.
- email: the email address, exactly as printed.
- If the image is not a business card, or is unreadable, return the JSON object
  with every value set to null."""

USER_PROMPT = "Extract the business card details as JSON."


def build_messages(image_data_url: str) -> list[dict]:
    """
    Build the OpenAI-compatible `messages` array for one card.

    The multimodal shape -- a content LIST mixing {"type": "text"} and
    {"type": "image_url"} blocks -- is the OpenAI vision convention, which
    Ollama, llama.cpp's server and vLLM all implement. Sticking to it is what
    makes the same code work against all three.
    """
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": USER_PROMPT},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        },
    ]
