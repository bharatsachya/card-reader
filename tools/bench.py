"""
Benchmark harness: measure latency, tokens and accuracy per configuration.

Run it ON the box that hosts the model -- Ollama binds to 127.0.0.1, and a
number measured across the internet is measuring the internet.

    python3 tools/bench.py --cards samples/bench --edges 1024,768,640,512,448
    python3 tools/bench.py --cards samples/bench --edges 768 --crop --repeat 3

THE ONE WAY THIS HARNESS CAN LIE, AND HOW IT IS PREVENTED.

Ollama caches the prompt prefix. Send the same image twice and the second call
returns in a fraction of the time with cached_tokens ~= prompt_tokens. On this
box that is 22s against 172s -- a 7.8x "speedup" that is entirely an artefact
of asking the same question twice.

That is not a hypothetical: twelve of the thirteen images in samples/batch/ are
byte-identical, so a benchmark pointed at that directory would report exactly
this fiction. So:

  * every card in the corpus is verified distinct before any timing is taken,
  * cached_tokens is read back from the response on EVERY call and a cache hit
    aborts the run rather than being averaged in,
  * --repeat re-runs the whole matrix rather than re-sending one image, and
    each repeat is preceded by a cache reset.

WHY IT DOES NOT CALL app.model_client.
model_client returns the model's text and nothing else -- deliberately, that is
its whole contract. The benchmark needs token counts and cache state from the
same response, and widening that interface to serve a dev tool would be the
wrong trade. Instead this builds the request with the SAME app.prompt and
app.imaging code the server uses, so what is measured is what production does,
and reads the raw response itself.
"""

import argparse
import base64
import hashlib
import io
import json
import pathlib
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app.parsing import parse_lead_json  # noqa: E402
from app.postprocess import postprocess_fields  # noqa: E402
from app.prompt import build_messages  # noqa: E402
from app.schema import LEAD_FIELDS  # noqa: E402


class CacheHit(RuntimeError):
    """A measurement was served from the prompt cache and is therefore fiction."""


# What counts as "this timing is fiction".
#
# MEASURED, NOT ASSUMED. The first version of this check demanded
# cached_tokens == 0 and aborted immediately on a legitimate run: Ollama had
# cached 299 of 1341 tokens, which is the SYSTEM PROMPT -- identical on every
# request by construction, since prompt.py generates it from LEAD_FIELDS. The
# 1042 image tokens were still computed from scratch, so the timing was real.
#
# The failure mode that actually matters is the whole prompt being cached: the
# same image sent twice returns ~1116 of 1117 tokens cached and 22s instead of
# 172s. That is what this threshold catches. Anything above 60% means image
# tokens are being reused, which only happens if the image repeated.
CACHE_HIT_FRACTION = 0.60


# --------------------------------------------------------------------------
# preprocessing -- deliberately mirrors app/imaging.py, parameterised by edge
# --------------------------------------------------------------------------

def preprocess(raw: bytes, max_edge: int, crop: bool) -> tuple[bytes, tuple[int, int]]:
    """
    Normalise one card exactly as the server would, at a given max edge.

    Returns (jpeg_bytes, (width, height)) so the table can report the dimensions
    that actually reached the model rather than the ones we asked for.
    """
    from PIL import ImageOps

    image = Image.open(io.BytesIO(raw))
    image.load()
    image = ImageOps.exif_transpose(image)
    if image.mode != "RGB":
        image = image.convert("RGB")

    if crop:
        from app.cardcrop import crop_to_card
        image = crop_to_card(image)

    width, height = image.size
    longest = max(width, height)
    if longest > max_edge:
        scale = max_edge / longest
        image = image.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.LANCZOS,
        )

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, subsampling=0, optimize=True)
    return buffer.getvalue(), image.size


# --------------------------------------------------------------------------
# the model call
# --------------------------------------------------------------------------

def _usage(body: dict) -> dict:
    """
    Pull token counts out, tolerating both shapes Ollama has used.

    The OpenAI-compatible path reports prompt_tokens/completion_tokens and may
    nest the cache figure under prompt_tokens_details. Ollama's native shape
    uses prompt_eval_count. Read both rather than assume, because guessing
    wrong here silently reports cached_tokens=0 for every call -- which would
    disable the one check this harness depends on.
    """
    usage = body.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens", body.get("prompt_eval_count", 0)),
        "completion_tokens": usage.get("completion_tokens", body.get("eval_count", 0)),
        "cached_tokens": details.get("cached_tokens", usage.get("cached_tokens", 0)),
    }


def call_model(url: str, model: str, jpeg: bytes, max_tokens: int,
               timeout: float, show_raw: bool = False) -> tuple[str, dict, float]:
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    payload = {
        "model": model,
        "messages": build_messages(data_url),
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    started = time.monotonic()
    response = httpx.post(url, json=payload, timeout=timeout)
    elapsed = time.monotonic() - started
    response.raise_for_status()
    body = response.json()

    if show_raw:
        print("    raw usage:", json.dumps(body.get("usage", {}), indent=6))

    usage = _usage(body)
    prompt_tokens = usage["prompt_tokens"] or 1
    cached_fraction = usage["cached_tokens"] / prompt_tokens
    if cached_fraction >= CACHE_HIT_FRACTION:
        raise CacheHit(
            f"cached_tokens={usage['cached_tokens']} of "
            f"prompt_tokens={usage['prompt_tokens']} ({cached_fraction:.0%}) -- "
            f"the image itself was served from cache, so this timing is not an "
            f"inference. Use distinct images or reset the cache."
        )
    usage["cached_fraction"] = cached_fraction
    content = body["choices"][0]["message"]["content"]
    return content, usage, elapsed


def reset_cache(model: str) -> None:
    """
    Evict the model so no prompt prefix survives into the next measurement.

    `ollama stop` unloads it; the next request reloads 3.2 GB from page cache.
    That reload is itself part of what A1 measures, so this is called BETWEEN
    repeats rather than between cards.
    """
    subprocess.run(["ollama", "stop", model], capture_output=True, check=False)
    time.sleep(1)


# --------------------------------------------------------------------------
# accuracy
# --------------------------------------------------------------------------

def _comparable(field: str, value) -> str:
    """
    Normalise both sides before comparing.

    The model is being scored on whether it READ the card, not on whether it
    formatted the answer -- formatting is postprocess.py's job and is already
    deterministic. So case and internal whitespace are ignored; anything more
    aggressive would start forgiving real mistakes.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split()).strip().lower()
    if field == "phone":
        # Compare digits only: the ground truth is as-printed, the model's
        # output has been through E164 normalisation.
        return "".join(ch for ch in text if ch.isdigit())
    return text


def score(fields: dict | None, truth: dict) -> tuple[int, list[str]]:
    """Fields correct out of seven, plus the names of the ones that are wrong."""
    if fields is None:
        return 0, list(LEAD_FIELDS)
    wrong = []
    for field in LEAD_FIELDS:
        got = _comparable(field, fields.get(field))
        want = _comparable(field, truth.get(field))
        if field == "phone" and got and want:
            # The printed number may omit the country code the model adds.
            ok = got.endswith(want[-9:]) or want.endswith(got[-9:])
        else:
            ok = got == want
        if not ok:
            wrong.append(field)
    return len(LEAD_FIELDS) - len(wrong), wrong


# --------------------------------------------------------------------------
# corpus
# --------------------------------------------------------------------------

def load_cards(directory: pathlib.Path) -> list[dict]:
    cards, digests = [], {}
    for image_path in sorted(directory.glob("*.jpg")):
        truth_path = image_path.with_suffix(".json")
        if not truth_path.exists():
            print(f"  skipping {image_path.name}: no ground truth alongside it")
            continue
        raw = image_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in digests:
            raise SystemExit(
                f"FATAL: {image_path.name} is byte-identical to "
                f"{digests[digest]}. Every timing after the first would be a "
                f"prompt-cache hit. Fix the corpus before benchmarking."
            )
        digests[digest] = image_path.name
        cards.append({
            "id": image_path.stem,
            "raw": raw,
            "truth": json.loads(truth_path.read_text()),
        })
    if not cards:
        raise SystemExit(f"no cards with ground truth in {directory}")
    return cards


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def markdown_table(rows: list[dict], baseline_seconds: float | None) -> str:
    header = (
        "| config | px sent | prompt tok | cached tok | completion tok | "
        "median s | min–max s | fields ok | vs baseline |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = []
    for row in rows:
        times = row["times"]
        median = statistics.median(times)
        delta = ""
        if baseline_seconds:
            change = (median - baseline_seconds) / baseline_seconds * 100
            delta = f"{change:+.0f}%" if abs(change) >= 1 else "—"
        lines.append(
            f"| {row['config']} | {row['px']} | {row['prompt_tokens']:,} | "
            f"{row['cached']} | {row['completion_tokens']} | **{median:.0f}** | "
            f"{min(times):.0f}–{max(times):.0f} | "
            f"{row['correct']}/{row['possible']} ({row['correct']/row['possible']*100:.0f}%) | "
            f"{delta} |"
        )
    return header + "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", default="samples/bench", type=pathlib.Path)
    parser.add_argument("--url", default="http://127.0.0.1:11434/v1/chat/completions")
    parser.add_argument("--model", default="qwen2.5vl:3b")
    parser.add_argument("--edges", default="768",
                        help="comma-separated long-edge values to sweep")
    parser.add_argument("--crop", action="store_true",
                        help="apply card detection before resizing")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--repeat", type=int, default=1,
                        help="re-run the whole matrix N times")
    parser.add_argument("--baseline", type=float, default=None,
                        help="seconds per card to compare against, e.g. 172")
    parser.add_argument("--out", type=pathlib.Path, default=None)
    parser.add_argument("--no-reset", action="store_true",
                        help="skip the cache reset before the first measurement")
    parser.add_argument("--label", default="",
                        help="prefix for the config column, e.g. 'keep_alive=-1'")
    args = parser.parse_args()

    cards = load_cards(args.cards)
    print(f"{len(cards)} distinct cards, {len(LEAD_FIELDS)} fields each")

    # RESET BEFORE THE FIRST MEASUREMENT, NOT JUST BETWEEN REPEATS.
    #
    # Ollama's cache lives as long as the model stays loaded, which is across
    # separate runs of this script. So re-running a configuration measured
    # earlier in the session returns 1340 of 1341 tokens cached and a time that
    # is not an inference. Without this, the benchmark is only correct the
    # first time it is run after a reboot -- and silently wrong every time
    # after, which is the worst possible property for a measurement tool.
    if not args.no_reset:
        print("resetting the model so the first measurement is cold…")
        reset_cache(args.model)
    print()

    rows = []
    first_call = True
    for edge in [int(e) for e in args.edges.split(",")]:
        config = f"{args.label + ' ' if args.label else ''}{edge}px{' +crop' if args.crop else ''}"
        times, prompts, completions, correct, sizes = [], [], [], 0, []
        cached_seen = []

        for repeat in range(args.repeat):
            if repeat:
                reset_cache(args.model)
            for card in cards:
                jpeg, size = preprocess(card["raw"], edge, args.crop)
                sizes.append(size)
                try:
                    text, usage, elapsed = call_model(
                        args.url, args.model, jpeg, args.max_tokens,
                        args.timeout, show_raw=first_call,
                    )
                except CacheHit as exc:
                    print(f"\nABORTED on {card['id']} @ {config}: {exc}")
                    return 1
                first_call = False

                fields, _ = parse_lead_json(text)
                if fields:
                    fields = postprocess_fields(fields)
                got, wrong = score(fields, card["truth"])
                correct += got
                times.append(elapsed)
                prompts.append(usage["prompt_tokens"])
                completions.append(usage["completion_tokens"])

                # cached is printed on every line, not just when it trips the
                # threshold: a number you can see is a number you can sanity
                # check, and a silent cache is how fake results survive review.
                print(f"  {config:<22} {card['id']:<18} {size[0]}x{size[1]:<6} "
                      f"{elapsed:6.1f}s  {usage['prompt_tokens']:>5} tok "
                      f"(cached {usage['cached_tokens']:>4}) "
                      f"{got}/7" + (f"  wrong: {','.join(wrong)}" if wrong else ""))
                cached_seen.append(usage["cached_tokens"])

        rows.append({
            "config": config,
            "px": f"{round(statistics.mean(s[0] for s in sizes))}x"
                  f"{round(statistics.mean(s[1] for s in sizes))}",
            "prompt_tokens": round(statistics.mean(prompts)),
            "completion_tokens": round(statistics.mean(completions)),
            "times": times,
            "correct": correct,
            "possible": len(cards) * len(LEAD_FIELDS) * args.repeat,
            "cached": round(statistics.mean(cached_seen)) if cached_seen else 0,
        })

    table = markdown_table(rows, args.baseline)
    print("\n" + table)
    if args.out:
        args.out.write_text(table + "\n")
        print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
