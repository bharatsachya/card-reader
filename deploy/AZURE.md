# Azure Container Apps — evaluated and rejected

> **This is not how the app is deployed.** It runs on a single EC2 box with the
> model on `127.0.0.1` — see [../README.md](../README.md#deployment) and
> [deploy.sh](deploy.sh). This document is kept because the reasons Container
> Apps was rejected are specific and worth having written down, not because it
> describes anything that was built.

Container Apps with an Ollama sidecar works, but **three of its defaults
silently break this app**, and each has to be turned off deliberately. Two of
the three are why colocating on one box won instead:

- scale-to-zero terminates a replica running an 85-minute batch, because the
  work generates no HTTP traffic to keep it alive;
- SQLite on the Azure Files (SMB) mount is a documented corruption risk, since
  SMB emulates POSIX advisory locks incompletely;
- the writable filesystem is ephemeral, so both the database and the retained
  images must live on a mount.

On one EC2 instance none of those apply: the process is always up, the database
is on a real local filesystem, and nothing is ephemeral. The rest of this
document is the detail, retained for the record.

---

## 1. Scale-to-zero will kill jobs mid-batch

Container Apps scales replicas on **HTTP traffic**. A running extraction is a
detached `asyncio` task that generates none: it is calling the model, not
serving requests. So the platform sees an idle replica and terminates it.

At ~170 s/card a 30-card batch runs for **85 minutes**. If the user closes the
browser tab — no more polling, no more HTTP — the replica scales in and the job
dies. The leads already extracted survive (they are committed per card) and
startup reconciliation flips the row to `failed: interrupted by restart`, so it
degrades honestly rather than hanging. But the work is gone.

```bash
az containerapp update -n card-reader -g <rg> --min-replicas 1 --max-replicas 1
```

`--min-replicas 1` is what keeps the worker alive. Read on for why max is also 1.

## 2. More than one replica corrupts the database

`--max-replicas 1` is not a performance choice, it is a correctness one, for
two independent reasons:

**SQLite on Azure Files.** The volume mount is SMB. SQLite's locking depends on
POSIX advisory locks, which SMB emulates incompletely — this is the documented
worst case for SQLite, and the failure mode is a corrupted database, not an
error message. WAL mode makes it worse, not better, because the `-wal` and
`-shm` files need shared-memory semantics that network filesystems do not
provide.

**Jobs are process-local.** A job's worker is an asyncio task inside the
replica that accepted the upload. Replica B can now *read* the job (that was
the point of persistence) but cannot run it, resume it, or cancel it.

If you genuinely need several replicas, the fix is the seam in `store.py`:
write `PostgresLeadStore(LeadStore)` against Azure Database for PostgreSQL and
add one branch to `build_store()`. Nothing else changes. Until then, one.

Also set, for the same reason:

```
RECLAIM_STALE_JOBS=false    # only if you ever run >1 replica
```

With one replica leave it `true` — it is what stops a killed job polling forever.

## 3. The writable filesystem is ephemeral

Anything outside a mounted volume is destroyed on every restart and every
revision. Both the database and the retained card images must be on the mount:

```
DB_PATH=/app/data/leads.db
IMAGE_DIR=/app/data/images
```

Mount an Azure Files share at `/app/data`. Note the container runs as **uid
10001**, so the share must be mounted with matching `uid`/`gid` options or the
app cannot write — which surfaces as the famously unhelpful
`unable to open database file`.

---

## Ollama as a sidecar

Container Apps supports multiple containers in one app, sharing `localhost`:

```
MODEL_URL=http://localhost:11434/v1/chat/completions
```

Two things to plan for:

- **Model weights are ~3 GB and the container filesystem is ephemeral.** Every
  cold start re-pulls the model. Mount a volume at `/root/.ollama`, or accept
  a multi-minute first request after each revision.
- **Sizing.** Qwen2.5-VL-3B needs ~4 GB resident. Give the app container 1 GB
  (peak is `MAX_CONCURRENCY × ~100 MB` plus overhead) and the sidecar the rest.

Container Apps caps a replica at 4 vCPU / 8 GB, which is your ceiling for a
CPU-only deployment. This is the constraint that eventually pushes the model
onto a GPU endpoint — at which point `MODEL_URL` and `MODEL_NAME` are the only
two things that change, and `IMAGE_RETENTION_DAYS` needs revisiting because the
disk ceiling scales with throughput.

---

## Health probes

`/health` answers in ~1 ms and never runs inference — point the liveness probe
at it. It reports model reachability from a cached TCP connect, so it tells you
`MODEL_URL` is wrong without ever blocking on the model.

Do **not** point a probe at `/api/model-check`: that one runs a real inference
and can take 170 seconds, so the platform would kill a healthy container for
failing a check it could never pass.

---

## Deploy

```bash
RG=card-reader-rg
ACR=cardreaderacr

az acr build --registry $ACR --image card-reader:$(git rev-parse --short HEAD) .

az containerapp update \
  -n card-reader -g $RG \
  --image $ACR.azurecr.io/card-reader:$(git rev-parse --short HEAD) \
  --min-replicas 1 --max-replicas 1
```

Tag with the commit sha rather than `latest`: `latest` makes "which code is
running?" unanswerable, and makes rollback a rebuild instead of a redeploy.

Set secrets as secret env vars, never in the image:

```bash
az containerapp secret set -n card-reader -g $RG \
  --secrets clerk-pk=pk_live_xxx
az containerapp update -n card-reader -g $RG \
  --set-env-vars AUTH_MODE=clerk \
                 CLERK_PUBLISHABLE_KEY=secretref:clerk-pk \
                 CLERK_ISSUER=https://your-instance.clerk.accounts.dev
```

## Ingress timeout

Container Apps' ingress caps a request at 240 s. Uploads return `202` in
milliseconds and progress is polled, so nothing here is at risk — but it is
exactly why the job queue exists, and why `/api/extract` (synchronous, one
card, ~170 s) is close to that limit and is a debugging tool rather than the
path the UI uses.
