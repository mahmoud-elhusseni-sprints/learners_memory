# Learner Memory & Profile Service — System Design

Sub-repo of **learn-os**. Owns everything about *what we know about a learner*:
ingesting raw evidence, distilling it into memory cards, and maintaining a living
learner profile.

## 1. Purpose & boundaries

| In scope | Out of scope |
| --- | --- | 
| Archiving raw learner evidence (immutable) | Producing the evidence (LMS, meetings, ATS) |
| Source-specific extraction agents → memory cards | Curriculum / content generation |
| Unified memory card store (Qdrant) + retrieval API | Recommendation UI |
| Learner profile (skills, personal data, career goal) | Auth / identity provider (consumed, not owned) |
| Scheduled + event-driven profile recomputation | Reporting dashboards (they read our API) |

The service is **write-heavy from events, read-heavy from other learn-os services**.
It never lets a caller write a profile field that is derived — derived fields are
only produced by the profile synthesizer.

## 2. The three layers

```
┌──────────────────────────────────────────────────────────────────────────┐
│  L1  RAW EVIDENCE LAYER      (Supabase Storage, ONE bucket + PG index)   │
│  One object per uploaded file. Replayable, on-prem retrievable.          │
│  meeting transcripts · task reviews · chat logs · assessments · CV · ... │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  extraction agents (1 per source type)
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  L2  MEMORY CARD LAYER           (Qdrant + Postgres card ledger)         │
│  One unified card schema. Common envelope + source-specific payload.     │
│  Every card is *directed*: it carries the skills/profile fields it feeds.│
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  profile synthesizer (per-dimension)
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  L3  LEARNER PROFILE LAYER       (PostgreSQL, relational + JSONB)        │
│  Personal data · general skills (34 subskills) · technical skills ·      │
│  career goal + learning journey + completed steps.                      │
└──────────────────────────────────────────────────────────────────────────┘
```

Key invariant: **L1 is the source of truth.** L2 and L3 are both derivable —
if we lose Qdrant or the profile tables we can rebuild them by replaying the
archive. That makes prompt changes, taxonomy changes and model upgrades safe:
bump a version, re-run the backfill.

## 3. Component map

```
                    ┌───────────────────────────────────────┐
   producers ──────►│  Ingestion API (FastAPI)              │
  (learn-os svcs,   │  POST /v1/ingest/{source}             │
   webhooks,        │  - validate, dedupe (idempotency key) │
   batch uploads)   │  - stream blob → Supabase Storage     │
                    │  - INSERT raw_document (Postgres)     │
                    │  - enqueue extract job                │
                    └──────────────┬────────────────────────┘
                                   │ Redis (Celery broker)
      ┌────────────────────────────┼──────────────────────────────┐
      ▼                            ▼                              ▼
┌──────────────┐        ┌─────────────────────┐       ┌──────────────────────┐
│ queue:extract│        │ queue:profile       │       │ queue:maintenance    │
│ extraction   │        │ profile synthesis   │       │ reindex, backfill,    │
│ agents       │        │ per learner/dim     │       │ decay, GC, exports    │
└──────┬───────┘        └──────────┬──────────┘       └──────────┬───────────┘
       │                           │                             │
       ▼                           ▼                             ▼
  Qdrant + card ledger        Postgres profile              Qdrant / Storage
       │                           │
       └────────────► Read API (FastAPI) ◄────────────────── other learn-os services
                      /v1/profile, /v1/memory/search, /v1/evidence
```

Processes to deploy: `api` (N replicas), `worker-extract`, `worker-profile`,
`worker-maintenance`, `beat` (scheduler, exactly 1). Same image, different command.

## 4. Data flow, end to end

0. **Register.** `POST /v1/learners` with the learner id issued upstream. We
   store it verbatim as the primary key — ids stay identical across learn-os.
1. **Ingest.** Producer calls `POST /v1/ingest/{source_type}` with metadata +
   payload (inline JSON, or a pre-signed upload for large files). API computes
   `content_sha256`, writes the file to the single bucket at a deterministic key,
   inserts `raw_document` (status `received`), and returns `202 + document_id`.
   Duplicate `(organization_id, source_type, external_id, content_sha256)` →
   returns the existing document, no re-work.
2. **Extract.** `extract_document(document_id)` loads the file, runs the
   source's agent through the LiteLLM proxy with structured output (traced to
   Langfuse under the document id), returning 0..N cards in the unified schema. Cards are written **transactionally**: Postgres ledger row
   first, then Qdrant upsert with the same UUID, then ledger row marked `indexed`.
   Emits `cards_written` event → enqueues profile recompute for the touched
   learner + the profile dimensions the cards target.
3. **Synthesize.** `recompute_profile(learner_id, dimensions)` is debounced.
   For each targeted dimension it pulls the relevant cards (filtered + recency
   weighted), asks the profile agent for a level + rationale + evidence card ids,
   and writes a new `skill_assessment` row (append-only, versioned) plus updates
   the `learner_profile` snapshot.
4. **Schedule.** Beat runs three jobs only: drain stale profile dimensions,
   retry failed documents, reconcile the ledger against Qdrant. Everything else
   (decay, expiry, re-embedding, resynthesis) is an on-demand admin task.
5. **Serve.** Read API returns the profile snapshot (cheap, Postgres) and, on
   request, the evidence chain: profile field → contributing card ids → raw
   document → signed URL to the original artifact.

## 5. Cross-cutting decisions

- **Idempotency everywhere.** Every job key is derived from content, not time.
  Re-running a task is a no-op or an in-place upsert.
- **Versioned derivation.** Cards carry `extractor_version` + `prompt_version` +
  `model`; profile assessments carry `synthesizer_version` + `taxonomy_version`.
  Backfills target a version, so partial re-processing is always resumable.
- **Multi-tenant by construction.** `organization_id` is on every row, every
  Qdrant payload, and every storage path; repositories require it, the API derives
  it from the token, never from the request body.
- **PII is contained.** Raw artifacts and personal data are the only places PII
  lives. Memory card `content` is written to be evidence-bearing but not
  identity-bearing; card text is what gets embedded and sent to the LLM.
- **Everything derived is explainable.** No profile number exists without the
  card ids that produced it — and each card carries the prompt/extractor version
  and document id that lead back to its Langfuse trace.
- **One egress for models.** Every completion and embedding goes through the
  LiteLLM proxy; no vendor SDK is imported outside `llm/client.py`.
