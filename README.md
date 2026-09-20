# Card Reader

Extracts structured sales leads from photographs of business cards using a
self-hosted vision-language model, and exports them as a spreadsheet.

Upload a stack of card photos, each one goes to a Qwen VLM over an
OpenAI-compatible endpoint, and you get back `first_name`, `last_name`,
`title`, `company`, `location`, `phone`, `email` — normalised, tabulated, and
downloadable as `.xlsx`.

**Live:** https://51.20.232.225.sslip.io/

---

## Quick start

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Terminal 1 — a fake model server, so you can run everything with no model
./.venv/bin/uvicorn tools.stub_model_server:app --port 11434

# Terminal 2 — the app
./.venv/bin/uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>, drag `samples/batch/` onto the card, watch it run.

To try it without running anything, the deployed instance is at
**<https://51.20.232.225.sslip.io/>** — the same build, on the hardware every
measurement in [Performance](#performance) was taken on.

To use a real model instead, point `MODEL_URL` at one (see
[Model backends](#model-backends)). Nothing else changes.

```bash
curl -s localhost:8000/health | python3 -m json.tool      # is the app up?
curl -s localhost:8000/api/model-check                    # is the MODEL up?
```

---

## Architecture

### The shape of the thing

```
                          browser (static/)
                                 │
                   POST /api/jobs │ multipart, N files
                                 ▼
  ┌──────────────────────────────────────────────────────────┐
  │  FastAPI (app/main.py)                                   │
  │                                                          │
  │   uploads.py ──► spool each file to DISK, size-checked   │
  │                  while streaming                         │
  │        │                                                 │
  │        ▼         202 Accepted + job_id  ─────────────────┼──►  returns
  │   jobs.py ─────► background asyncio task                 │     immediately
  │        │         bounded by a semaphore                  │
  │        ▼                                                 │
  │   extraction.py  orchestrates one card:                  │
  │        │                                                 │
  │        ├─ 1. imaging.py      EXIF rotate, resize, encode │  deterministic
  │        ├─ 2. prompt.py       build the messages array    │  deterministic
  │        ├─ 3. model_client.py HTTP ──────────────────────►│  NON-deterministic
  │        ├─ 4. parsing.py      text ➜ dict, never raises   │  deterministic
  │        ├─ 5. postprocess.py  E164 phone, lowercase email │  deterministic
  │        └─ 6. schema.py       ➜ Lead                      │
  │                  │                                       │
  │                  ▼                                       │
  │   store.py ────► LeadStore  ──►  SQLite (WAL)             │
  │                                                          │
  │   excel.py ────► GET /api/jobs/{id}/export.xlsx          │
  └──────────────────────────────────────────────────────────┘
                                 ▲
             GET /api/jobs/{id}  │ polled once a second
```

*Measured performance, and one significant negative result, are in
[Performance](#performance).*

### Why it is split this way

**The model call is the only non-deterministic step.** Everything before and
after it is pure, testable code. That boundary is the organising principle of
the whole codebase:

- `model_client.py` does not know what a `Lead` is. It takes an image data URL
  and returns the model's raw text. Nothing more.
- `parsing.py` and `postprocess.py` never touch the network. They are pure
  functions over strings, which makes every failure mode reproducible in a
  unit test.
- Because the boundary is clean, `tools/stub_model_server.py` can stand in for
  the model and exercise the entire app with zero inference cost — which is how
  every behaviour documented below was verified.

### The files

| File | Responsibility |
|---|---|
| `app/config.py` | Every env-driven setting. The only place model details live. |
| `app/schema.py` | `Lead` and the canonical `LEAD_FIELDS` tuple. |
| `app/imaging.py` | EXIF rotation, resize, JPEG re-encode, bomb guard, HEIC. |
| `app/prompt.py` | The system prompt, generated from `LEAD_FIELDS`. |
| `app/model_client.py` | HTTP to `/v1/chat/completions`. The only networked module. |
| `app/parsing.py` | Model text ➜ dict. Never raises. |
| `app/postprocess.py` | E164 phones, lowercased emails, whitespace. |
| `app/extraction.py` | Orchestrates one card. Owns the error policy. |
| `app/uploads.py` | Streams uploads to disk with the size cap enforced mid-stream. |
| `app/jobs.py` | The background worker and its concurrency semaphore. |
| `app/store.py` | `LeadStore` interface, `SqliteLeadStore` (shipped) and `InMemoryLeadStore` (tests). Sessions, jobs, leads, cursor pagination, schema migrations. |
| `app/excel.py` | `.xlsx` generation. |
| `app/auth.py` | Clerk session verification against the public JWKS. |
| `app/main.py` | Routes and static mounting. |
| `static/index.html` | The chat shell and the sign-in gate. |
| `static/app.css` | The Industry design system, as plain CSS. No build step. |
| `static/auth.js` | Clerk integration; wraps `fetch` to attach the token. |
| `static/app.js` | The conversation, uploads, polling, table, download. |
| `tools/stub_model_server.py` | Fake model, for development and failure testing. |
| `tools/make_test_card.py` | Generates the sample cards, including a rotated one. |

---

## Setup

The deployed instance is **<https://51.20.232.225.sslip.io/>** (AWS EC2
`m7i-flex.large`, 2 vCPU, no GPU, eu-north-1), running the app and Ollama on the
same box. See [deploy/](deploy/) for the scripts that put it there, and
[Deployment](#deployment) for how it is wired.

To run it locally instead:

Requires Python 3.11+. Developed and verified on 3.13.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env        # optional; every setting has a default
```

### Configuration

Everything is an environment variable, and everything has a working default.

| Variable | Default | What it does |
|---|---|---|
| `MODEL_URL` | `http://localhost:11434/v1/chat/completions` | OpenAI-compatible endpoint |
| `MODEL_NAME` | `qwen2.5vl:3b` | Model identifier sent in the request body |
| `MODEL_API_KEY` | *(empty)* | Sent as `Authorization: Bearer` when set |
| `MODEL_TIMEOUT_SECONDS` | `600` | Per-request timeout. Slowest measured card is 172 s |
| `MODEL_MAX_ATTEMPTS` | `3` | Retries per card on transient failure. The deployment sets `2` — see [Chosen configuration](#chosen-configuration-and-why) |
| `MODEL_RETRY_BASE_SECONDS` | `1` | First backoff wait; doubles each attempt |
| `MAX_IMAGE_EDGE` | `1024` | Longest edge sent to the model |
| `MAX_IMAGE_PIXELS` | `89478485` | Decompression-bomb ceiling |
| `MAX_UPLOAD_BYTES` | `15728640` | 15 MB per file |
| `MAX_FILES_PER_REQUEST` | `20` | Batch size cap. 20 × 172 s keeps the worst case under an hour |
| `MAX_CONCURRENCY` | `1` | Cards in flight at once — **this bounds peak memory** |
| `MAX_JOBS_RETAINED` | `50` | History cap for the **in-memory** backend only; SQLite keeps everything |
| `DEFAULT_PHONE_REGION` | `IN` | Region assumed for numbers with no country code |
| `UPLOAD_DIR` | *(system temp)* | Where uploads spool while queued |
| `STORE_BACKEND` | `sqlite` | `sqlite` persists across restarts; `memory` is the original dict, used by tests |
| `DB_PATH` | `./data/leads.db` | SQLite file. The deployment uses `/var/lib/card-reader/leads.db` |
| `IMAGE_DIR` | `./data/images` | Retained normalised card images, content-addressed |
| `IMAGE_RETENTION_DAYS` | `30` | How long a card image is kept. The **lead** is kept indefinitely |
| `RECLAIM_STALE_JOBS` | `true` | On startup, fail jobs left `running` by a dead process. Set `false` for >1 replica |
| `AUTH_MODE` | *(inferred)* | `clerk` or `local`. `clerk` without keys is a hard startup failure |

The **deployment** overrides some of these — see
[Chosen configuration](#chosen-configuration-and-why) for what it sets and why.
Where the two differ, the deployment's value is the one running at
<https://51.20.232.225.sslip.io/>.

### Model backends

The app targets the OpenAI-compatible chat-completions shape, which Ollama,
llama.cpp, vLLM and OpenAI all speak. Switching backends is two variables.

```bash
# Ollama (default)
MODEL_URL=http://localhost:11434/v1/chat/completions
MODEL_NAME=qwen2.5vl:3b

# llama.cpp llama-server, if you swap Ollama out
MODEL_URL=http://10.0.1.23:8080/v1/chat/completions
MODEL_NAME=Qwen2.5-VL-3B-Instruct-Q4_K_M
```

---

## Deployment

The app and the model run on **one** AWS EC2 instance: `m7i-flex.large`,
2 vCPU, 7.6 GiB, no GPU, `eu-north-1`. Live at
<https://51.20.232.225.sslip.io/>.

```
:443  nginx ── TLS (Let's Encrypt, auto-renewing)
:80   nginx ── 301 → https, plus the ACME challenge path
        │
        └─→ :8000  card-reader   (systemd, uvicorn, 1 worker)
                      │
                      └─→ 127.0.0.1:11434  Ollama · qwen2.5vl:3b

      /var/lib/card-reader/   SQLite database + retained card images
```

**Why the model is on the same box, not a separate one.** Ollama binds to
`127.0.0.1`, so it is unreachable from the internet at all — there is no
authentication to add because there is no exposed endpoint. No card image
crosses a network. SQLite gets a real local filesystem rather than a network
mount, where its locking is unreliable. And it costs nothing: the app is
`await`-blocked on I/O during the 170 seconds the model is using both cores, so
it is roughly 0.05% of the CPU cost of a card.

Azure Container Apps was evaluated first and rejected —
[deploy/AZURE.md](deploy/AZURE.md) records why, chiefly that scale-to-zero
terminates a replica running a multi-hour batch because the work produces no
HTTP traffic to keep it alive.

| | |
|---|---|
| `deploy/deploy.sh` | Idempotent: venv, `OLLAMA_KEEP_ALIVE=-1`, systemd unit, nginx. Checks Ollama and the model **first** and fails loudly, because every other step can succeed while the app is useless |
| `deploy/card-reader.service` | systemd unit, `After=ollama.service`, hardened (`ProtectSystem=strict`, `NoNewPrivileges`, restricted address families) |
| `deploy/nginx-card-reader.conf` | `proxy_read_timeout 900s` — nginx's 60 s default would kill **every** extraction and the symptom would look like model failure |
| `deploy/enable-tls.sh` | Let's Encrypt on an `sslip.io` hostname, since a public CA will not sign a bare IP |

**Port 80 must stay open.** Renewal uses the HTTP-01 challenge, so closing it
breaks renewal — not immediately, but when the certificate expires, for a
reason disconnected from the change that caused it.

Verified after deployment: reboot the instance and all three services return
with no manual intervention, the database survives, and extraction still works.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The single-page UI |
| `GET` | `/health` | Liveness + resolved config. Never calls the model. |
| `GET` | `/api/auth-config` | Publishable key + whether auth is on. Public by design. |
| `GET` | `/api/model-check` | Actually calls the model. Slow, by design. |
| `POST` | `/api/extract` | One image, synchronously. Easiest thing to curl. |
| `POST` | `/api/jobs` | Bulk upload. Returns `202` + `job_id` immediately. |
| `GET` | `/api/jobs/{id}` | Poll a job. `?summary=true` omits the leads payload. |
| `GET` | `/api/jobs` | All jobs, newest first. |
| `GET` | `/api/jobs/{id}/export.xlsx` | Download. `?only_successful=true` filters. |
| `GET` | `/api/export.xlsx` | Every lead from every job. |

---

## Libraries, and why each one

| Library | Why this one |
|---|---|
| **FastAPI** | Async-native, which matters because this workload is almost entirely *waiting on the model*. Generates OpenAPI docs from type hints for free (`/docs`). |
| **uvicorn** | The ASGI server FastAPI runs on. |
| **python-multipart** | Required for FastAPI to parse `multipart/form-data`. Without it, the first `UploadFile` route raises at startup. |
| **httpx** | An HTTP client with a real `async` API. `requests` is sync-only and would block the event loop on every model call, serialising the whole server. |
| **Pillow** | EXIF handling, LANCZOS resampling, format conversion. The standard for this. |
| **pillow-heif** | HEIC decoding. **Not optional** — HEIC is the default iPhone camera format, and Pillow cannot read it alone. Without this, the single most likely real-world input fails. |
| **openpyxl** | Writes `.xlsx` with formatting, freeze panes and autofilter, with no Excel installed. |
| **PyJWT[crypto]** | Verifies Clerk session tokens. `[crypto]` pulls in `cryptography` for the RS256 signature check. |
| **certifi** | Mozilla's CA bundle. Python does not use the OS certificate store, so JWKS fetching fails without it on macOS and slim Linux images. |
| **phonenumbers** | Google's libphonenumber port. Validates against each country's real numbering plan rather than pattern-matching digits. |

---

## Technical decisions

### 1. Images are resized to 1024px and EXIF-rotated before sending

The highest-leverage code in the project.

**EXIF rotation.** Phone cameras store pixels in the sensor's native landscape
order and record "rotate 90° when displaying" as a tag. Photo viewers honour
it, so the image looks upright *to the person who took it* — but a raw decode
ignores it, and the model receives a sideways card. VLMs are strongly biased
toward horizontal text; a rotated card typically returns nulls or garbage.
`ImageOps.exif_transpose()` bakes the rotation into the pixels.

Verified: `samples/card_rotated.jpg` is *stored* 1200×2000 portrait and leaves
the pipeline as 1024×614 landscape — identical to the upright version.

**Resizing.** A VLM does not "look at" an image; it splits it into patches and
turns each into a token. Token count scales with **pixel area**, so cost is
quadratic in edge length:

```
4032 × 3024 phone photo  = 12.2 M px    ← ~12× the vision tokens
1024 ×  768 resized      =  0.8 M px
```

- **Latency:** see [Performance](#performance) — on Ollama this turns out
  **not** to reduce token count at all, because the model normalises the image
  internally. The claim below is true of VLMs in general and false of this one.
- **Memory:** vision tokens occupy the KV cache; a large enough image overruns
  the context window and the request fails outright.
- **Accuracy does not improve to compensate.** Qwen2.5-VL and Qwen3-VL resize
  internally to their own supported resolution anyway. Sending 4032px means
  paying full upload and preprocessing cost to arrive at a similar resolution.

Doing it ourselves with LANCZOS is sharper than a naive server-side resize and
makes behaviour identical across Ollama, llama.cpp and vLLM.

**Why 1024 and not 512?** Business cards carry ~8pt print. Below roughly 1024
on the long edge, phone digits blur together and the model misreads or drops
them. Tunable via `MAX_IMAGE_EDGE` so it can be benchmarked per deployment.

### 2. `temperature 0`

Extraction has exactly one correct answer, so sampling variance is pure
downside: it makes bugs unreproducible and the same card yield different
results between runs of the same batch. Greedy decoding gives determinism.

### 3. Portable prompting, not server-side constrained decoding

Ollama's `format`, llama.cpp's GBNF grammars and vLLM's `guided_json` can each
*guarantee* valid JSON. All three are spelled differently, so depending on one
would hardcode a server assumption into app logic — the opposite of the
"nothing model-specific" requirement.

Instead: a strict system prompt, `temperature 0`, and a parser that assumes the
model will misbehave. The trade is explicit — we accept occasional malformed
output and handle it, in exchange for backend portability.

### 4. A malformed reply is a flagged row, never a 500

`parsing.py` always returns; it never raises. It tries, in order: strict parse
→ strip markdown fences → extract the first balanced `{...}` by brace-counting
(a regex cannot match nested brackets) → repair Python literals and trailing
commas. Values are coerced to `Optional[str]`, so a phone returned as an int or
a location returned as a list still lands correctly.

Verified against every observed failure mode:

| Model returns | Result |
|---|---|
| clean JSON | `ok` |
| ```` ```json … ``` ```` fences | `ok`, recovered |
| prose wrapping the JSON | `ok`, recovered |
| `{"phone": 9820012345, "location": ["Mumbai","India"]}` | `ok`, coerced |
| trailing comma, Python `None` | `ok`, repaired |
| truncated mid-object | `parse_error` + reason |
| empty reply | `parse_error` + reason |
| a refusal in prose | `parse_error` + reason |
| HTTP 500 from the model | `model_error` + reason |
| model server down | `model_error` + reason |
| upload is not an image | `input_error` + reason |

All of them return **HTTP 200** with a flagged row.

### 5. Post-processing is code, not prompting

The model reliably *finds* the phone number and unreliably *formats* it. So
formatting is done deterministically afterwards:

- **Phones → E164** via `phonenumbers`, with `DEFAULT_PHONE_REGION` supplying
  the country for the domestic numbers most cards print. `is_valid_number()`
  checks against the real numbering plan, which is what rejects a postcode the
  model mistook for a phone number. If nothing validates, the model's raw text
  is kept rather than discarded — a human can still use `ext. 402`.
- **Emails → lowercased**, `mailto:` stripped, `name (at) company.com`
  un-obfuscated, trailing punctuation trimmed.

### 6. Bulk upload is a job queue, not a long request

Qwen2.5-VL-3B on the target box takes **136–172s per card** — measured, not
estimated — so 20 cards is roughly 45–57 minutes. No HTTP request survives that: nginx's default `proxy_read_timeout`
and an AWS ALB's idle timeout are both **60 seconds**, and browsers abandon
fetches. The connection would die minutes in and every completed result would
be lost, because it only existed in that request's memory.

So `POST /api/jobs` returns `202` + a `job_id` in milliseconds, a detached
`asyncio` task does the work, and the client polls. Leads are appended **per
card**, so the UI fills progressively and a failure at card 49 does not discard
the first 48.

Deliberately **in-process** — no Redis, no Celery, no SQS. On a single
instance that adds zero infrastructure and zero cost. Its limits are real and
listed below.

### 7. Memory is bounded by concurrency, not by batch size

The measured cost of decoding one 4032×3024 photo is **~100 MB RSS** (a JPEG is
compressed; the decoded RGB buffer is 35 MB, and Pillow holds source, resize
target and encode buffer at once). Naively, 1000 uploads at once would be ~98 GB.

Three things prevent that:

1. **Uploads stream to disk**, in 1 MB chunks, with the size cap enforced
   *during* the stream — so an oversized file is aborted after ~1 MB rather
   than being fully received and then rejected.
2. **The file is read inside the semaphore**, not before it. All N coroutines
   are created immediately, but all except `MAX_CONCURRENCY` park on the
   semaphore costing a few hundred bytes each while their bytes wait on disk.
3. **Each file is deleted as soon as it is processed**, so a 50-file batch
   never holds 750 MB of disk for its whole run.

Peak memory is `MAX_CONCURRENCY × ~100 MB` — a constant. Measured RSS after
processing a 14-image batch: **32 MB**.

`MAX_CONCURRENCY` defaults to **1** because llama.cpp serves one request at a
time; firing ten at once just queues them while memory climbs. Raise it only
for a backend with real batching (vLLM on a GPU).

### 8. Backpressure rejects work before it costs anything

- More than `MAX_FILES_PER_REQUEST` → `413`, whole batch refused. A partial
  success the client did not ask for is worse than a clear rejection.
- A single file over `MAX_UPLOAD_BYTES` → listed in `rejected[]`, **the rest of
  the batch still runs**. Someone who dragged in 30 cards and one stray
  screenshot wants their 29 cards.
- `MAX_IMAGE_PIXELS` blocks decompression bombs: a 136 KB PNG declaring
  144 million pixels is rejected before Pillow allocates anything.
- Client-side checks in `app.js` mirror these limits purely for instant
  feedback. They are a courtesy, not a control — the server enforces them
  independently, because anyone can bypass the browser with curl.

### 9. Transient model failures are retried with backoff

llama.cpp briefly refusing connections while it loads, a reset socket, a lost
race for the single inference slot — these are transient. Turning a one-second
blip into a permanently blank row, after the user waited twenty minutes for the
batch, is a bad trade. Three attempts with 1s/2s backoff covers every realistic
blip; bounding it at three stops a genuinely-dead backend from making a 50-card
batch take hours to fail.

### 10. Storage is behind an interface, so a database drops in

`store.py` defines `LeadStore` as an abstract base class. `InMemoryLeadStore`
implements it. `build_store()` is the single place that picks one. Adding
Postgres means writing `PostgresLeadStore(LeadStore)` and adding one branch to
`build_store()` — no route, worker or test changes.

Two details make that swap realistic rather than theoretical:

- **Methods are `async`** even though the dict never awaits. A real driver
  (asyncpg) is async; a sync interface would force rewriting every caller.
- **The interface is coarse-grained** (`append_lead(job_id, lead)`), so callers
  cannot reach behind it and the implementation is genuinely free to change.

### 11. Excel details that are easy to get wrong

- **Phone cells are formatted as text (`@`).** Given `+919820012345` in a
  General cell, Excel "helpfully" reformats it as a number — dropping the `+`
  and possibly rendering `9.2E+11`. Every carefully-normalised E164 number
  would arrive mangled.
- **The workbook is built in a `BytesIO`, never on disk** — no temp filenames
  to invent, no cleanup, no two requests racing for the same path.
- **Flagged rows are included and tinted**, with `status` and `error` as the
  last two columns. A spreadsheet that silently omits 3 of your 14 cards is
  dangerous: you would never know to re-shoot those three.
  `?only_successful=true` gives the clean CRM-import file.
- Freeze panes and an autofilter are one line each and are the difference
  between a data dump and a spreadsheet someone can work in.

### 12. Model output reaches the DOM through `textContent`

Every table value came from a language model reading a user-supplied image —
untrusted input twice over. A card printed with `<img src=x onerror=…>` would
be faithfully extracted and, via `innerHTML`, executed. The renderer builds
elements individually and assigns `textContent`, which writes text and never
parses markup.

### 13. The frontend is served by the same process

It is three static files. Hosting them separately (S3/CloudFront/nginx) would
add cost, a deploy step and CORS configuration in exchange for nothing — the
app server is idle-waiting on the model anyway. Same-origin also means `fetch`
needs no CORS headers at all.

---

## Performance

All numbers here were measured on the deployment box — AWS `m7i-flex.large`,
2 vCPU, 7.6 GiB, no GPU, Ubuntu 24.04 — against `qwen2.5vl:3b` served by
Ollama. **No number in this section comes from the stub server**, and the
harness that produced them (`tools/bench.py`) is built so that it cannot
quietly produce one.

### How these were measured, and the trap that makes most such tables wrong

Ollama caches the prompt prefix. Send the same image twice and the second call
returns in **22s** against **172s**, with `cached_tokens` covering essentially
the whole prompt. Any benchmark that reuses an image reports that as a 7.8×
speedup.

This repository shipped with exactly that landmine: **twelve of the thirteen
images in `samples/batch/` are byte-identical.** A resolution sweep pointed at
that directory would have produced a confident, completely fictional table.

So the harness:

* verifies every card in the corpus is distinct **before** taking a timing, and
  aborts if two hash the same;
* reads `cached_tokens` back from every response and refuses to average a cache
  hit into a median;
* evicts the model before the first measurement, because Ollama's cache
  survives across separate runs of the script — which meant the benchmark was
  only trustworthy the first time it ran after a reboot, and silently wrong
  every run after.

The threshold for "this is a cache hit" is 60% of prompt tokens, not zero. The
first version demanded zero and aborted a legitimate run: 299 of 1,341 tokens
were the **system prompt**, which is identical on every request by
construction. Those 299 are cached in normal operation too — a small, real
speedup that costs nothing.

### A2 — Input resolution: a negative result, and the most useful one

The hypothesis was that resizing is the biggest available lever, because vision
tokens scale with image area. **It is not, on this runtime.**

| long edge sent | pixels sent | prompt tokens | wall time |
|---|---|---|---|
| 1024 px | 1024×768 | **1,341** | 169 s |
| 448 px | 448×336 | **1,341** | 169 s |
| 320 px | 320×240 | **1,341** | 136 s |
| 256 px | 256×192 | **1,341** | 137 s |

Prompt tokens are **flat at 1,341 across a 16× range of input pixels**. Ollama
resizes to a fixed grid before the vision encoder, so the resize this
application performs is discarded — the model literally cannot tell a 256 px
input from a 1024 px one in token terms.

**The conclusion is about the runtime, not the parameter.** `MAX_IMAGE_EDGE`
cannot reduce inference cost here at all. Getting control of image token count
requires a serving stack that exposes it — vLLM's `--limit-mm-per-prompt`, or
llama.cpp with explicit projector settings. Swapping runtimes is a two-env-var
change by design (see [Model backends](#model-backends)), so this is a
configuration decision rather than a rewrite.

The resize is **kept anyway**, for two reasons that survive the finding: it
bounds upload size and decode memory (`MAX_CONCURRENCY × ~100 MB` peak), and it
keeps behaviour identical if the backend is swapped for one where resolution
*does* matter.

This also retroactively explains something that should have been suspicious
earlier: the baseline barely moved between 768 px and 1024 px. It was not noise.
It was the resize being thrown away.

### A1 — Keeping the model resident

Ollama unloads an idle model after 5 minutes by default, so the next card pays
a 3.2 GB reload on top of inference. `OLLAMA_KEEP_ALIVE=-1` is set by
`deploy/deploy.sh`.

Measured over the **same six real cards** in both conditions, so the only
variable is whether the model was resident. `--cold-each` evicts the model
before every card, which also clears the prompt cache — every cold measurement
below reports `cached_tokens = 0`, so none of them is a cache artefact.

| condition | median | range | fields correct |
|---|---|---|---|
| **cold** — model evicted before each card | **156 s** | 151–160 s | 35/42 |
| **warm** — model resident | **143 s** | 137–148 s | 35/42 |

**≈ 13 s saved per idle gap**, which is the cost of reading 3.2 GB back in.
Accuracy is identical in both conditions, as it must be — residency cannot
change what the model reads, and seeing that come out equal is a check on the
harness rather than a finding.

On a box where cards arrive in bursts minutes apart, the default 5-minute
unload meant paying that reload repeatedly for no reason; the machine has
7.6 GiB and nothing else wants the RAM.

*An earlier, incidental pair of figures (181 s cold after a reboot against
167 s warm) appeared during deployment verification. They pointed the same way
but were a sample of one each; the table above replaces them.*

### A3 — Card detection and perspective crop

A phone photo of a business card is mostly desk. Across the twelve test images
the card occupies **67% of the frame on average**, so a third of every image is
fabric or granite that costs exactly as much as a phone number.

Because A2 showed token count is fixed, **cropping cannot make inference
faster.** The hypothesis was that it would improve *accuracy* instead: with the
token budget fixed, spending it on card rather than background should raise the
effective resolution of the text. That hypothesis was testable, and it was
tested.

| | result |
|---|---|
| cards detected | **10 of 12** |
| fallback (no plausible quad) | 2 of 12 — original image passed through unchanged |
| pixels removed when detected | **33% on average**, best case 59% |
| detection cost | 15–110 ms per image |

**And then the accuracy measurement said no.**

Six real business cards (supplied by the client, not synthetic), each run with
cropping off and on, scored field-by-field against hand-written ground truth:

| | no crop | with crop |
|---|---|---|
| fields correct | **35/42 (83%)** | **32/42 (76%)** |
| median latency | 143 s | 145 s |
| prompt tokens | 1,343 | 1,356 |

**Cropping made extraction worse, not better.** Two cards regressed and none
improved:

| card | no crop | with crop | newly wrong |
|---|---|---|---|
| SEEMA (two people) | 7/7 | **5/7** | `title`, `location` |
| S. K. Brokers (mononym) | 5/7 | **4/7** | `last_name` |
| the other four | unchanged | unchanged | — |

The likely mechanism, and it is a hypothesis rather than a finding: once the
card fills the frame, the model appears more willing to *assign* a value to a
field that is genuinely absent. On the SEEMA card, `title` is correctly `null`
when the card sits on a granite worktop and becomes a guess once cropped —
probably "REAL ESTATE & INVESTMENTS", which is a strapline, not a job title.
Surrounding context seems to help the model decide a field is missing.

This also refines the A2 result. Tokens are invariant to **scale** but not to
**aspect ratio**: cropping changes the card's proportions and token count moves
between 1,337 and 1,380. So the fixed grid has a fixed *area*, not fixed
dimensions.

**Cropping is therefore implemented and off by default.** The code is in
`app/cardcrop.py`, it detects reliably and fails safe, and on a corpus where
the card were smaller in frame it may well pay off — the six real cards here
are all close-ups where the card already dominates. What is not defensible is
enabling it on the strength of a plausible mechanism after the measurement said
otherwise. n=6 is a small sample and the direction is consistent.

**The dominant error is not cropping at all — it is `location`, wrong in 11 of
12 runs in both configurations.** The model returns the full street address
where the prompt asks for "the city, or the city and region". That is a
prompt-adherence problem, it costs nothing at inference time to fix, and it is
worth more than either A2 or A3: correcting it alone would move accuracy from
83% to roughly 97% on this corpus.

The module is built so every failure path returns the original image. A
detector that occasionally returns a confident crop of the *wrong* rectangle is
worse than one that often declines, because a wrong crop deletes part of the
card and the model dutifully reports what is left.

That is not hypothetical. On a creased card photographed against patterned
fabric, Canny traced a contour just inside the true edge and the warp clipped
the city off the bottom; the model then returned every field except `location`
and nothing about the output looked wrong. The quad is now expanded 2.5%
outward before warping — overshooting costs a sliver of desk, which is free
because tokens are fixed, while undershooting destroys text silently.

### A4 — Not evaluated

Quantization variants (`q4_0` vs the default `q4_K_M`) and smaller models such
as `moondream` (~1.8B) are the **remaining latency lever**, and specifically
because of the A2 result: if input size cannot reduce token cost, then reducing
the cost *per token* is what is left.

They were not evaluated within the time available. Each variant is a fresh
multi-gigabyte download plus a full benchmark pass at ~170 s per card on two
cores, and a result that is not measured properly is worse than an absent one.
Naming the next experiment and why it was not run is the honest version of this
section.

### Chosen configuration, and why

| setting | value | reason |
|---|---|---|
| `MODEL_TIMEOUT_SECONDS` | 600 | Slowest measured card is 172 s; 180 left no headroom, and a timeout mid-card discards the compute already spent |
| `MODEL_MAX_ATTEMPTS` | 2 | At a 600 s timeout, 3 attempts is 30 min on one stuck card. Connection-refused still fails fast |
| `MAX_CONCURRENCY` | 1 | Ollama already saturates both vCPUs on one inference (92%/81% measured); parallelism splits the same cores |
| `MAX_FILES_PER_REQUEST` | 20 | 50 × 172 s = 2 h 23 m in a single request. 20 keeps the worst case under an hour |
| `max_tokens` | 256 | Observed completion length is 83–86 tokens; 256 is triple the need and still bounds a rambling reply |
| `MAX_IMAGE_EDGE` | 1024 | **Does not affect inference cost on Ollama** (A2). Retained to bound upload size and decode memory |
| `OLLAMA_KEEP_ALIVE` | -1 | Saves ≈14 s per idle gap |

### What this means for a 20-card batch

At 167 s per card with concurrency 1: **≈ 56 minutes**. The UI shows a per-card
ETA computed from the job's own observed pace rather than a configured
constant, because the same model varies several-fold between an idle box and a
busy one. Until the first card completes there is no estimate and the page says
so — a fabricated first guess anchors the user and then turns out to be triple.

---

## Known limitations

### Single-process only

This used to be a storage problem and is now a worker problem. With SQLite,
several processes **do** see one truth: polling a job from a different worker
than the one that accepted it returns the right answer, which is exactly what
the dict could not do.

What remains single-process is the **work itself**. A job's worker is an
`asyncio` task inside whichever process accepted the upload, so another worker
can read that job but cannot run, resume or cancel it. If that process dies,
the job is orphaned until startup reconciliation fails it.

So `--workers N` is safe for serving and unsafe for owning work. The deployment
runs one worker deliberately, which is also the right call for a model that
serves one request at a time. Distributing the workers themselves means a real
queue, and that is a different piece of work from the storage seam.

### What survives a restart, and what does not

Jobs and leads are persisted to SQLite (`STORE_BACKEND=sqlite`, the default and
what the deployment runs), so **a process restart keeps everything**: sessions,
jobs, leads, and the retained card images. Verified by killing the service
mid-batch and restarting it.

Two things still do not survive:

- **The work in flight.** A job's worker is an `asyncio` task inside the
  process. Kill the process and the task dies with it — but the row does not,
  so it would claim to be `running` forever and the UI would poll a progress
  bar that can never finish. On startup the app therefore reconciles any job
  left `queued`/`running` to `failed` with the reason `interrupted by restart`.
  Cards already extracted are kept, because leads are committed per card.
  Disable with `RECLAIM_STALE_JOBS=false` if you ever run more than one
  replica, or a replica starting later will kill another's live job.
- **Instance replacement.** The database is a file on the instance's disk
  (`/var/lib/card-reader/leads.db`), not a managed service or an attached
  volume. Terminating and recreating the box loses it. `cp leads.db` is a
  complete backup, which is one of the reasons SQLite was the right choice
  here; a scheduled copy off-box is the next step and is not implemented.

### Auth adds a runtime dependency on Clerk

The sign-in SDK loads from Clerk's CDN and token verification fetches Clerk's
JWKS, so with auth enabled the app needs outbound internet and stops working if
Clerk is down. That is a real change from the otherwise self-contained design.
Setting `CLERK_ISSUER` and `CLERK_PUBLISHABLE_KEY` to blank disables auth and
restores standalone operation.

The `SESSION_EXPIRED` branch in `app/auth.py` is not covered by the tests run
here: producing an expired-but-genuinely-Clerk-signed token is not something
this project can forge. Signature rejection, unknown key ids, malformed tokens
and missing tokens all are covered.

### No presigned-URL uploads

Bytes go through the app server. For very large batches the correct production
answer is to have the browser `PUT` directly to S3 and hand the worker a key —
that removes upload bandwidth, memory and disk from the app server entirely.
It was deliberately not built: it requires S3, CORS and IAM configuration, and
it would only speed up the part that *is not the bottleneck*. The model is.
The worker would still have to download each image to encode it, so the decode
cost returns; it just moves somewhere concurrency can bound it.

### Accuracy is bounded by the model, and is not measured here

There is no evaluation set and no accuracy number in this README, because
measuring it properly needs a labelled corpus of real cards. Expect a small
3B model to struggle with: heavily stylised or script typefaces,
low-contrast foil or embossed print, cards photographed at an angle, dual-language
cards (it may return either language), and deciding which of three printed
numbers is "the" phone number. `first_name`/`last_name` splitting is also
culturally naive — it assumes a Western given-name-then-family-name order.

### Phone normalisation falls back to raw text

If `phonenumbers` cannot validate a number, the model's raw string is kept
rather than dropped. That is deliberate — losing data the model read correctly
is worse than leaving it unformatted — but it means the phone column is not
*guaranteed* uniformly E164. Filter on it if you need that guarantee.

### `DEFAULT_PHONE_REGION` is global

One region for the whole deployment. A batch mixing Indian and German cards
normalises correctly only where the card printed a `+country` prefix, which
most international cards do but most domestic ones do not.

### The job queue is in-process

No retry of an entire failed job, no cancellation, no cross-instance
distribution, no durability. A crash mid-batch loses the remaining work (though
leads already extracted are retained, because they are appended per card).

### Python version

Written against 3.11+; developed and verified on 3.13. No 3.13-only syntax is
used.

---

## Authentication

> **Scope note.** Sign-in and the conversational interface were added after the
> core brief was complete. They are a deliberate extension, not scope drift:
> the app handles personal data (a named person's employer, phone and email),
> and without an owner on each record every visitor can read every lead anyone
> has ever extracted. The general reasoning is in
> [Why these pieces belong in any real application](#why-these-pieces-belong-in-any-real-application).
> Everything in the original six stages works unchanged with auth disabled.

Sign-in is [Clerk](https://clerk.com), integrated **without React and without a
build step** — Clerk publishes `@clerk/clerk-js`, a plain browser bundle that
exposes a global `Clerk` object.

### The rule this is built around

> The API verifies the token itself. It never trusts a header because the
> frontend promises to have set one.

An API that believes `X-User-Id` because its own frontend sets it is not
authenticated — it is authenticated only to people who use the frontend, and
anyone with curl can send any header they like. So the browser sends Clerk's
signed session JWT and `app/auth.py` checks that signature against Clerk's
published public keys.

How the check works:

1. Clerk signs each session token with a private key only Clerk holds.
2. The matching **public** key is published at `<issuer>/.well-known/jwks.json`.
3. We fetch it (cached by `PyJWKClient`, refetched on an unknown key id, which
   makes Clerk's key rotation transparent) and verify the signature.
4. We also check `exp` and `iss` — the latter so a token minted by some *other*
   Clerk tenant an attacker controls is rejected.

### The secret key is not used

Verification needs public keys only. `CLERK_SECRET_KEY` appears nowhere in this
codebase and is never read. The process holds no credential that could act on
the Clerk account.

### Verified behaviour

| Request | Result |
|---|---|
| No `Authorization` header | `401 UNAUTHENTICATED` |
| Malformed token | `401` — invalid header padding |
| Self-signed HS256 token | `401` — no matching signing key |
| **RS256 token carrying Clerk's real key id, signed with a different key** | **`401` — signature verification failed** |
| Job id belonging to another user | `404`, not `403` |

That last row is deliberate. A `403` would confirm the id exists, letting an
attacker enumerate valid job ids. `404` tells them nothing they did not already
know, and is the same response they would get for a nonexistent job.

### TLS, and a bug worth knowing about

Python does **not** use the operating system's certificate store. A python.org
install on macOS ships with no CA bundle until you run
`Install Certificates.command`, and minimal Linux containers often have none.

The symptom is badly misleading: fetching the JWKS fails with
`CERTIFICATE_VERIFY_FAILED`, which surfaces as "could not resolve signing key"
— so *every* token appears invalid, including genuine ones, and it reads like
an auth bug rather than a TLS one. `app/auth.py` therefore builds its SSL
context explicitly from `certifi`.

What it does **not** do is disable verification. Turning certificate checking
off to make the error go away would let anyone who can intercept the connection
serve their own JWKS — their own public keys — and every token they forged
would verify. That converts the whole file from a security control into
decoration.

### Data is scoped per user

Jobs carry a `user_id`. Every read checks ownership, and `list_jobs` /
`all_leads` filter by it. In the in-memory store that is a field comparison; in
a database it becomes `WHERE user_id = ?` — the same shape, which is the point
of the storage seam.

### Running without Clerk

Leave `CLERK_ISSUER` and `CLERK_PUBLISHABLE_KEY` blank and auth disables
itself: `require_user` returns a fixed local user and every route works
unauthenticated. This keeps `curl localhost:8000` usable for development and
keeps the project runnable by someone who has no Clerk account.

---

## Design

The interface uses the **Industry** design system from the sibling
`trao-interview-kit` project, ported from Tailwind v4 `@theme` variables to
plain CSS custom properties. Same tokens, same rules, no build step:

- three surfaces (`--paper`, `--surface`, plus tints) rather than one
- one steel accent ramp on a shared lightness scale
- one red (`--alarm`), spent on failure and deletion only
- cards are **tinted fills with no border** — borders were doing two jobs, and
  "this is an object" moved to the fill
- Barlow Condensed 600 for titles, Barlow 400/500 for everything read
- four radius steps tied to *kinds of object*: control 10, card 14,
  **composer 22**, pill 999

The composer is deliberately rounder than a card because it is the one thing on
the page you put something into, and focus rings the whole composer rather than
the control inside it — a square outline drawn tight around a borderless input
inside a 22px card is the one thing that makes the card look like a mistake.

The page is a **conversation**: each upload becomes a user turn with
thumbnails, and the assistant replies with a shimmering status line, a progress
meter, the leads table, and download buttons. The API underneath is unchanged
— the chat is how results are *presented*, not a different protocol.

---

## Why these pieces belong in any real application

Several parts of this project are not specific to business cards. They are the
things that separate a demo from something you could put in front of users, and
each one exists because of a failure that shows up the first time real people
use real data. This section explains the general principle behind each, so the
reasoning transfers to the next application rather than staying here.

### Authentication is about data, not about logins

It is tempting to treat sign-in as a feature you add when you are ready to
charge money. It is not. The moment an application holds data that belongs to
*someone*, three separate problems appear at once, and auth is the only thing
that solves any of them:

1. **Isolation.** Without a user on the request, every record belongs to
   everybody. Whoever opens the page sees whatever the last person uploaded.
   That is not a privacy setting you can add later — it changes the shape of
   every query, because "list the jobs" becomes "list *this user's* jobs".
   Retrofitting a `user_id` onto a schema that never had one is one of the
   more painful migrations there is.
2. **Accountability.** When something goes wrong — data deleted, a bad export,
   an abusive upload — "who did this?" has no answer unless requests carry an
   identity. Logs of anonymous actions tell you what happened and never who.
3. **Limits that mean anything.** Rate limits, quotas and fair-use rules all
   need a subject. Limiting by IP address is a poor substitute: it punishes
   everyone behind one office NAT and is trivially evaded by anyone else.

**This application specifically handles personal data.** A business card is a
named person's job title, employer, phone number and email address — that is
personally identifiable information under GDPR, India's DPDP Act and similar
regimes. Storing PII in a system where any visitor can read any record is not
merely untidy; it is the kind of thing that becomes a reportable incident.
Scoping every row to an owner is the minimum bar, and it is why `user_id` is a
field on `Job` rather than an afterthought.

The related principle is where the check lives:

> Authorisation is enforced on the server, or it is not enforced.

`static/auth.js` decides what the user *sees*. `app/auth.py` decides what the
user may *do*. Deleting the frontend file entirely would not grant anyone a
single extra byte of access. Any check that lives only in the browser — a
hidden button, a filtered list, a header the frontend promises to set — is a
suggestion, because the browser is under the user's control and `curl` is not
under yours.

### Long work needs a job, not a long request

HTTP requests are not a place to keep work. Proxies, load balancers and
browsers all enforce their own timeouts (60 seconds is the common default in
all three), and none of them know or care that your task is legitimately slow.
A request that runs longer than the shortest timeout in the chain does not just
appear to fail — it **loses the completed work**, because that work only ever
existed in the dying request's memory.

The general rule:

> If a task can outlive a request, give it an id and let the client ask about
> it.

That single change buys several things at once: the user gets a progress
indicator instead of a spinner, a dropped connection costs nothing, work
survives a page refresh, and the server can decide *when* to do the work rather
than being forced to do it now. It applies to anything slow — video encoding,
report generation, bulk imports, sending 10,000 emails — not just model
inference.

It does not require infrastructure. This project's queue is an `asyncio` task
and a dictionary: no Redis, no Celery, no SQS, no added cost. Reaching for a
message broker on day one is a common over-correction; the shape matters more
than the machinery, and the shape is what lets you swap the machinery in later.

### Backpressure is not optimisation, it is survival

Any endpoint that accepts input from outside must decide, *before* spending
resources, whether it is willing to. The cost of an input is often wildly
disproportionate to its size — the examples in this codebase being a 6 MB JPEG
that becomes 100 MB of RAM when decoded, and a 136 KB PNG that declares itself
to be 144 million pixels.

The pattern that generalises:

- **Bound every unbounded thing.** Request size, item count, concurrency,
  retention. Anything a caller controls, you cap.
- **Reject early, before the expensive step.** Checking size *while streaming*
  means an oversized upload is cut off after a megabyte instead of being
  received in full and then refused.
- **Bound resources by concurrency, not by input volume.** Peak memory here is
  `MAX_CONCURRENCY × ~100 MB` whether you upload 5 files or 5,000. That is the
  difference between a server that degrades and one that dies.
- **Validate on the server even when you validate in the browser.** The client
  check is for the user's benefit — instant feedback, no wasted upload. The
  server check is the actual rule. Both is not duplication; only the first is
  negligence.

### Failure must be per-item, not per-batch

When a system processes many things, one bad item must not destroy the run.
This is why every failure in this pipeline becomes a typed row rather than an
exception: a batch of 50 cards where 3 are unreadable returns 50 rows, 3 of
them flagged with a reason.

The generalisation is that **errors are data**. They have a type (`input_error`
vs `parse_error` vs `model_error`), a message aimed at whoever can act on it,
and a place in the output. That typing is not bureaucracy — it tells the user
what to *do*: re-shoot the photo, retry later, or contact someone. "Something
went wrong" tells them nothing and wastes a support conversation.

The corollary is to **persist progress incrementally**. Results are recorded as
each card completes, not in one write at the end, so a crash at item 49 keeps
the first 48.

### Untrusted input includes your own model's output

Input validation is usually framed around what users type. Model output
deserves exactly the same suspicion, for two different reasons:

- **Structurally**, a model is a probabilistic text generator, not an API. It
  will eventually return markdown fences, prose, a number where you asked for a
  string, or a truncated object — so the parser must treat malformed output as
  an expected case rather than an exception.
- **Securely**, model output is user input laundered through a model. Text
  printed on an uploaded card reaches your page having passed through nothing
  that sanitises it. Rendering it with `innerHTML` is the same vulnerability as
  rendering a user's comment with `innerHTML`, just with an extra step in
  between that makes it easier to forget.

The rule that covers both: **treat any value you did not compute yourself as
hostile until you have constrained it.**

### Determinism belongs in code, not in prompts

Anything with exactly one correct answer should be computed, not generated.
Phone formatting, date parsing, currency rounding, sorting, deduplication — a
model can often do these, and will do them slightly differently each time.

Pushing them into code after the model gives you three things a prompt cannot:
the same answer every run, a stack trace when it is wrong, and a unit test that
pins the behaviour. Use the model for the part that genuinely needs judgement —
here, *finding* the phone number on a cluttered card — and let deterministic
code handle everything downstream of that.

### Configuration is environment, never code

Every value that differs between your laptop, a colleague's machine and
production belongs in the environment: endpoints, credentials, limits,
timeouts. The test is simple — **if changing where the app points requires
editing a file that gets committed, it is hardcoded.**

Here that principle is what makes the migration from local Ollama to a
llama.cpp instance on AWS a two-variable change rather than a code change. The
same discipline keeps secrets out of version control, because a value that
lives in the environment cannot be accidentally committed.

### Seams are cheaper before you need them than after

`store.py` defines an interface and one implementation. Today that interface
looks like indirection with no payoff — there is exactly one store, and a
plain dictionary would be shorter.

The payoff is not today. It is that swapping in Postgres touches one file,
because no route and no worker ever learned that storage was a dictionary. Had
the routes read a module-level `JOBS = {}` directly, that knowledge would be
spread across the codebase and the swap would mean touching everything that
reads it.

The judgement call is **where** to put a seam, because a codebase that is all
interfaces is worse than one with none. A useful test: put a seam where the
implementation is *expected to change* (storage, the model backend, the queue)
and not where it is not. Both seams in this project sit on a boundary the brief
explicitly said would move.

### Health checks answer one question quickly

`/health` deliberately does not call the model, and `/api/model-check` does.
The general principle is that a liveness endpoint is polled by machines every
few seconds and must answer in milliseconds. Making it verify its dependencies
means a slow dependency makes your healthy process *look* dead — and your
orchestrator will dutifully kill and restart a container that was working fine,
turning a downstream slowdown into a restart loop.

Check dependencies on a separate, slower endpoint that humans call
deliberately.

---

## AI usage, and what was rejected

This project was built with Claude. That is only useful information if it comes
with the corrections, so this is a record of where the AI's proposals were
wrong, overruled, or disproved by measurement — not a list of what it produced.

### Direction changes made by the human

| Proposal | Outcome |
|---|---|
| Deploy the app to **Azure Container Apps** with an Ollama sidecar | **Overruled.** Colocated on the existing EC2 box instead. Better on every axis: Ollama stays on `127.0.0.1` and is unreachable from the internet, SQLite gets a real local filesystem instead of SMB (where its locking is unreliable), no card image crosses the public network, and the app is ~0.05% of the CPU cost of a card so it costs nothing to host beside the model |
| Serve the **frontend from Vercel** | **Raised by the human, argued against, dropped.** `GET /` is not static — it strips the sign-in markup server-side based on `AUTH_MODE` — so splitting it would move that decision back into the browser, which this codebase deliberately moved out of it, and would add CORS for no gain |
| **Port-scan** the model host to discover its port | **Stopped by the human.** Replaced by asking directly |
| Ask deployment questions through a **structured form** | **Rejected.** Plain numbered questions instead |
| Run `git init` + `echo "# card-reader" >> README.md` as given | **Declined by the AI.** The repository already had eight commits, and that `echo` would have appended a stray heading to this file |
| Model described throughout as **2B** | **Corrected by the human** to Qwen2.5-VL-**3B** |
| **`MODEL_API_KEY`** proposed to secure the Ollama endpoint | **Wrong, withdrawn.** Ollama has no built-in authentication and ignores the header. Network-level restriction is the only real control |
| Claimed **port 80 was blocked** by the security group | **Wrong, corrected by the human.** Inferred from a listening-port list rather than tested; a single connect showed a 212 ms refusal, not a timeout |
| Evaluate **quantization variants and moondream** (A4) | **Descoped by the human.** Each is a multi-gigabyte download plus a full pass at ~170 s per card; a result that is not measured properly is worse than an absent one. Recorded as the named next experiment instead |

### Where the measurement overruled the plan

The resolution sweep (A2) was specified as *"expected to be the biggest win."*
It was not. Prompt tokens are flat at 1,341 from 256 px to 1024 px, because
Ollama resizes to a fixed grid before the vision encoder. The client-side
resize is inert on this runtime.

That reframed the crop work (A3) rather than invalidating it: if the token
count is fixed, the only remaining lever is *what those tokens contain*, so
cropping the desk away became an accuracy change rather than a latency one.

### Mistakes the AI made that were caught by tooling, not by review

* **A benchmark that would have lied.** The first cache check demanded
  `cached_tokens == 0` and aborted a valid run — 299 tokens were the system
  prompt. Left unexamined in the other direction, the same harness would have
  averaged genuine 22 s cache hits into a median and reported a 7.8× speedup.
* **A crop that silently destroyed data.** Card detection clipped the city off
  a creased card; the model then returned six of seven fields and nothing
  looked wrong. Found by looking at the cropped image, not by reading the code.
* **Fifteen minutes of benchmark data lost.** Python block-buffers stdout when
  redirected; the risk was noticed, deprioritised, and then cost every
  measurement taken before the run was stopped.
* **Two CI failures from linting individual files** instead of the whole tree
  the way CI does — including one "fix" that did not fix the problem.
* **`--require-hashes=false`** in the Dockerfile: not a real pip flag. Unreachable
  locally because the development machine had no Docker daemon; CI caught it.
* **`pytest` vs `python -m pytest`**: 73 tests passed locally and every module
  failed to import in CI, because `-m` silently puts the working directory on
  `sys.path` and the bare entry point does not.

---

## Testing without a model

`tools/stub_model_server.py` implements the same `/v1/chat/completions`
contract and can reproduce each misbehaviour on demand — which is how the
failure table in §4 was verified.

```bash
STUB_MODE=fenced   ./.venv/bin/uvicorn tools.stub_model_server:app --port 11434
```

| `STUB_MODE` | Simulates |
|---|---|
| `clean` *(default)* | Well-formed JSON |
| `mixed` | A rotating mix of good and bad replies, with varied people |
| `fenced` | JSON wrapped in markdown fences |
| `prose` | JSON buried in conversational text |
| `weird_types` | `phone` as an int, `location` as a list, `"N/A"` strings |
| `malformed` | Trailing comma and Python `None` |
| `truncated` | Reply cut off mid-object |
| `empty` | Empty string |
| `refusal` | "I'm sorry, I can't read this image." |
| `http500` | Upstream error |
| `slow` | Never replies — exercises the timeout path |

`STUB_DELAY=1.5` adds artificial latency so the job queue's progress behaviour
is observable.

```bash
./.venv/bin/python tools/make_test_card.py   # regenerate sample cards
```
