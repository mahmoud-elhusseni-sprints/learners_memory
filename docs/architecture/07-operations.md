# Configuration, deployment & operations

## 7.1 Stack

| concern | choice |
| --- | --- |
| API | FastAPI + uvicorn (async) |
| ORM / migrations | SQLAlchemy 2.0 async + **Alembic** |
| Relational | PostgreSQL 15+ (Supabase-hosted or managed) |
| Vector | Qdrant (server mode, named vectors, payload indexes) |
| Object storage | Supabase Storage (private buckets, signed URLs) |
| Queue | Celery 5 + Redis (broker & results), Celery Beat |
| LLM | Litellm via `llm/client.py` (structured outputs) |
| Embeddings | pluggable provider behind `llm/embeddings.py` |
| Config | pydantic-settings, 12-factor, one `Settings` |
| Observability | structlog (JSON) + OpenTelemetry traces + Prometheus |
| Tests | pytest, testcontainers (pg + qdrant + redis), VCR for LLM |

## 7.2 Processes

```
api       : uvicorn learner_memory.main:app --host 0.0.0.0 --port 8000
extract   : celery -A learner_memory.workers.celery_app worker -Q ingest,extract -c 8
profile   : celery -A learner_memory.workers.celery_app worker -Q profile -c 4
maint     : celery -A learner_memory.workers.celery_app worker -Q maintenance -c 2
beat      : celery -A learner_memory.workers.celery_app beat        # exactly one
```

One image, five commands. `deploy/docker-compose.yml` brings up the full local
stack (pg, redis, qdrant, minio-as-storage-stub, api, workers, beat).

## 7.3 Environment

```
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
QDRANT_URL= / QDRANT_API_KEY= / QDRANT_COLLECTION_ALIAS=learner_memory_v1
SUPABASE_URL= / SUPABASE_SERVICE_KEY= / STORAGE_BUCKET=learner-evidence
LITELLM_BASE_URL= / LITELLM_API_KEY= / LLM_MODEL=claude-sonnet-5
LLM_MAX_CONCURRENCY=8 / EMBEDDING_MODEL= / EMBEDDING_DIM=1024
LANGFUSE_HOST= / LANGFUSE_PUBLIC_KEY= / LANGFUSE_SECRET_KEY= / LANGFUSE_ENABLED=true
JWT_ISSUER= / JWT_JWKS_URL=
TAXONOMY_VERSION=1 / EXTRACTOR_VERSION_OVERRIDE=
ONPREM_MIRROR_PATH=/mnt/archive
PROFILE_DEBOUNCE_SECONDS=300 / EVIDENCE_WINDOW_MONTHS=18 / DECAY_HALF_LIFE_DAYS=120
```

## 7.4 Migrations & seeds

- `alembic upgrade head` on deploy (init container / release phase).
- Qdrant collections and the storage bucket are **not** in Alembic —
  `scripts/bootstrap.py` (also run in the API lifespan) creates the collection,
  its payload indexes and the bucket, idempotently.
- `scripts/seed_taxonomy.py` parses `docs/skills_taxonomy_framework.md` into
  `skill_catalog` (34 subskills × 6 level descriptors + best-evidence hints).
  The markdown table stays the human-editable source of truth; a taxonomy change
  bumps `TAXONOMY_VERSION` and schedules a full resynthesis.

## 7.5 What we watch

- Pipeline: ingest rate, extraction p95 latency, cards/document, failure and
  quarantine rate, dead-letter depth, queue lag per queue.
- Quality: cards with no valid contribution (extractor drift), % skills with
  `confidence < 0.4`, level-change volatility per skill, dedupe merge rate.
- Cost: LLM tokens and spend per document and per profile recompute, budget
  guard that trips the `extract` queue rather than overrunning silently.
- Integrity: ledger↔Qdrant drift count, archive checksum failures, profile
  snapshots older than the newest contributing card.

## 7.6 Privacy & compliance

- PII lives in `raw_document` blobs and `learner_personal_data`; card `content`
  is scrubbed at extraction time and `pii_level` is recorded.
- Evidence reads are audited (`who, what, when`), signed URLs are short-lived.
- **Right to erasure:** `POST /v1/admin/learners/{id}/forget` tombstones cards,
  deletes Qdrant points, purges profile rows and blobs, and leaves a
  non-reversible audit stub. Because L2/L3 are derived, this is a complete
  deletion, not a best-effort one.
- Per-org data residency is expressible via bucket + Qdrant cluster routing in
  `Settings`, since org id is on every path and payload.

## 7.7 Build order (suggested)

1. Skeleton, config, Postgres models, Alembic, taxonomy seed, health checks.
2. Ingest API + Supabase archive + `raw_document` ledger (no AI yet) — this
   alone makes the archive requirement real and everything else replayable.
3. Card schema + one extractor (`task_review`, most structured) + Qdrant index
   + retrieval API.
4. Profile synthesizer for general skills + snapshot + read API.
5. Celery beat schedules, decay, reconciliation, backfill tooling.
6. Remaining extractors (meeting, chat, assessment, CV), career/journey logic,
   outbound events, on-prem mirror.

## 7.8 LLM access: LiteLLM proxy + Langfuse

All model traffic — completions and embeddings — leaves through the **LiteLLM
proxy**. `llm/client.py` is the only module that imports `litellm`; extractors
and the synthesizer depend on `LLMClient`, so the proxy address, the model or the
tracer can change without touching a prompt or a pipeline.

Why the proxy is the only egress: keys stay in the proxy (never in this repo's
env beyond a single proxy token), model choice, fallbacks, rate limits and spend
caps are configured centrally, and swapping a model is a proxy change and a
`LLM_MODEL` value — not a deploy of this service.

**Langfuse** is wired as a litellm success/failure callback (`_configure_litellm`),
so every generation is traced without a decorator on each call site. Each call
carries a `TraceContext`:

| field | value | why |
| --- | --- | --- |
| `trace_id` / `session_id` | `document_id` | every LLM call for one document groups into one trace — chunk by chunk |
| `user_id` | `learner_id` | per-learner cost and quality views |
| `generation_name` | `extract.meeting_transcript`, `embed.cards` | compare prompt performance per source |
| `metadata` | `organization_id`, `extractor_version`, `prompt_version`, `chunk_anchor` | a bad card in Qdrant leads straight to the exact generation that produced it |

That last row is the point: card → `extractor_version` + `prompt_version` +
`source_document_id` → Langfuse trace → the exact prompt, response and cost.
Prompt regressions are diagnosable after the fact instead of reproducible only by
guesswork.

## 7.9 Local stack (`docker compose up`)

| service | port | what it is |
| --- | --- | --- |
| `api` | 8000 | FastAPI, hot-reload, `/metrics` |
| `worker-extract` / `worker-profile` / `worker-maintenance` | — | Celery workers, one per queue group |
| `beat` | — | scheduler, exactly one replica |
| `postgres` | 5433 → 5432 | profile + ledger |
| `redis` | 6380 → 6379 | broker |
| `prometheus` | 9092 → 9090 | scrapes api + celery-exporter |
| `celery-exporter` | 9808 | queue depth, task rates, runtimes |
| `grafana` | 3002 → 3000 | provisioned datasource + pipeline dashboard (admin/admin) |

Host ports are remapped where the default collides with another stack on the
same machine (langfuse holds 3000/6379/9090, lc-postgres holds 5432). Inside
the compose network every service still listens on its standard port.

Qdrant (managed cluster), Supabase Storage (hosted project), LiteLLM and
Langfuse are **not** in the compose file — all shared or remote services,
pointed at through `.env`. The compose stack pins only
`DATABASE_URL` and `REDIS_URL`; the rest of the URLs come from your `.env` as
written. Run `make init` for first-time setup
(compose up → alembic upgrade → seed taxonomy → bootstrap Qdrant/bucket).
