"""
A fake OpenAI-compatible model server, for development and testing.

WHY THIS EXISTS:
The real model is slow (seconds to minutes per card on CPU) and
non-deterministic in its formatting. That makes it a terrible thing to develop
the other 95% of the app against. This stub answers instantly and can be told
to reproduce, on demand, each specific misbehaviour we must handle.

Because the app targets the OpenAI-compatible wire format, the app cannot tell
this apart from Ollama -- which is itself a demonstration that the abstraction
is real. Point MODEL_URL at it and everything downstream works unchanged.

Run:
    ./.venv/bin/uvicorn tools.stub_model_server:app --port 11434

Choose a behaviour with the STUB_MODE env var:
    clean (default) | fenced | prose | malformed | truncated | empty
    | refusal | weird_types | slow | http500
"""

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Stub VLM server")

# A plausible, deliberately MESSY model answer: unformatted phone, mixed-case
# email, honorific on the name. Post-processing must clean all of it up.
_CARD = {
    "first_name": "Asha",
    "last_name": "Rao",
    "title": "Senior Sales Manager",
    "company": "Nimbus Logistics Pvt Ltd",
    "location": "Mumbai",
    "phone": "98200 12345",
    "email": "A.Rao@NimbusLogistics.IN",
}

_MODES = {
    "clean":       lambda: json.dumps(_CARD),
    "fenced":      lambda: "```json\n" + json.dumps(_CARD, indent=2) + "\n```",
    "prose":       lambda: "Sure! Here are the details:\n\n"
                           + json.dumps(_CARD) + "\n\nLet me know if you need more.",
    "malformed":   lambda: '{"first_name": "Asha", "last_name": None, }',
    "truncated":   lambda: '{"first_name": "Asha", "comp',
    "empty":       lambda: "",
    "refusal":     lambda: "I'm sorry, I can't read the text in this image.",
    "weird_types": lambda: json.dumps(
        {"firstName": "Asha", "phone": 9820012345,
         "location": ["Mumbai", "India"], "title": "N/A"}
    ),
}


# Rotating counter used by STUB_MODE=mixed.
_calls = {"n": 0}

# The cycle a "mixed" run walks through, so a demo batch exercises every
# rendering path in the UI: good rows, recovered rows, and both flavours of
# flagged row.
_MIXED_CYCLE = ["clean", "fenced", "clean", "refusal", "clean",
                "weird_types", "clean", "truncated"]

# Different people, so a demo table does not look like one row copied 14 times.
_PEOPLE = [
    ("Asha", "Rao", "Senior Sales Manager", "Nimbus Logistics Pvt Ltd",
     "Mumbai", "98200 12345", "A.Rao@NimbusLogistics.IN"),
    ("Vikram", "Mehta", "Head of Procurement", "Saffron Foods",
     "Pune", "+91 98765 43210", "V.Mehta@SaffronFoods.co.in"),
    ("Lena", "Fischer", "Regional Director", "Brandt Maschinenbau GmbH",
     "Hamburg", "+49 40 123456", "L.Fischer@Brandt-MB.DE"),
    ("Daniel", "Okoro", "Founder", "Lagos Freight Collective",
     "Lagos", "+234 802 555 0134", "daniel@lagosfreight.NG"),
    ("Mei", "Tan", "Operations Lead", "Straits Shipping",
     "Singapore", "+65 6123 4567", "mei.tan@StraitsShipping.SG"),
]


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    mode = os.getenv("STUB_MODE", "clean")

    call_index = _calls["n"]
    _calls["n"] += 1

    if mode == "mixed":
        mode = _MIXED_CYCLE[call_index % len(_MIXED_CYCLE)]
        person = _PEOPLE[call_index % len(_PEOPLE)]
        _CARD.update(dict(zip(
            ("first_name", "last_name", "title", "company",
             "location", "phone", "email"), person)))

    if mode == "http500":
        return JSONResponse({"error": "simulated upstream failure"}, status_code=500)
    if mode == "slow":
        # Longer than any sane MODEL_TIMEOUT_SECONDS in testing, to exercise
        # the timeout path.
        await asyncio.sleep(600)

    # STUB_DELAY simulates realistic CPU inference latency (the real thing is
    # 10-30s per card). Without it the stub answers instantly and the job
    # queue's progress behaviour is impossible to observe.
    delay = float(os.getenv("STUB_DELAY", "0"))
    if delay:
        await asyncio.sleep(delay)

    content = _MODES.get(mode, _MODES["clean"])()

    # Echo back the OpenAI response envelope the app expects.
    return {
        "id": "stub-1",
        "object": "chat.completion",
        "model": body.get("model", "stub"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
