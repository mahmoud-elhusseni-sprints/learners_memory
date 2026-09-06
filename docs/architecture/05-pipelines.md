# Pipelines, queues & scheduling

## 5.1 Runtime

- **Broker/result backend:** Redis. **Orchestrator:** Celery 5 + Celery Beat
  (`django-celery-beat`-style DB schedule not needed; a static beat schedule in
  code + a `scheduled_job` table for per-org overrides).
- Queues are separated so a burst of transcript extraction can never starve
  profile updates or the maintenance sweeps.

| queue | worker | concurrency | character |
| --- | --- | --- | --- |
| `ingest` | `worker-extract` | high (I/O) | download, normalize, chunk |
| `extract` | `worker-extract` | LLM-rate-limited | agent calls, embeddings |
| `profile` | `worker-profile` | moderate | synthesis, per-learner lock |
| `maintenance` | `worker-maintenance` | low | sweeps, backfills, exports |

All tasks: `acks_late=True`, `reject_on_worker_lost=True`, bounded retries with
exponential backoff + jitter, `max_retries` then → `dead_letter` table (never a
silent drop). Every task takes an `idempotency_key` and short-circuits if
`job_run` already holds a `succeeded` row for it.

## 5.2 Extraction pipeline

Parsing happens inside the extractor (`BaseExtractor.parse`), so there is no
separate normalization stage and no second copy of the file in storage.

```
extract.extract_document(document_id)
   ├─ pick extractor from registry[source_type]   (registry is self-populating)
   ├─ parse bytes → canonical text
   ├─ chunk (source-aware: speaker turns / rubric items / CV sections)
   ├─ per chunk → structured LLM call → list[MemoryCardDraft]
   ├─ post-process: taxonomy validation, dedupe vs existing cards
   │                (cosine > 0.93 + same contribution key → merge, bump salience),
   │                PII scrub on `content`
   ├─ embed batch → Qdrant upsert (point id = card id)
   ├─ write ledger rows + card_contribution rows (one transaction)
   └─ emit cards_written → profile.schedule_recompute(learner_id, dims)
```

Failure modes, handled explicitly:
- **Unparseable blob** → `status=quarantined`, alert, no retry storm.
- **LLM schema violation** → one repair round-trip, then partial accept (valid
  cards kept, invalid chunk recorded in `error`), document marked
  `extracted_partial` so a backfill can revisit just that chunk.
- **Unknown learner** → document parked at `pending_identity`; it is picked up
  once that `learner_id` is registered (`POST /v1/learners`), which re-enqueues
  extraction.

## 5.3 Profile synthesis pipeline

Event-driven **and** scheduled — the two paths run the same code.

```
profile.schedule_recompute(learner_id, dimensions[])
   └─ marks learner_profile.stale_dimensions, debounces 5 min per learner
      (a coalescing key in Redis; a meeting producing 20 cards causes 1 recompute)

profile.recompute_profile(learner_id, dimensions=None)
   ├─ advisory lock on learner_id (no concurrent writers)
   ├─ for each stale dimension:
   │     cards = qdrant.filter(learner, contribution_keys=dim,
   │                           status=indexed, observed_at > now-18mo)
   │     weighted by  salience × confidence × recency_decay(half_life=120d)
   │     → synthesizer LLM call → {level 1..6, confidence, rationale,
   │                               evidence_card_ids}
   │     → guard: level can rise at most 1 per recompute unless a
   │       high-salience assessment card justifies more (anti-flip-flop)
   │     → INSERT skill_assessment (old row superseded=true)
   ├─ career: recompute journey step statuses from milestone cards
   ├─ personal data: apply `fact` cards through source-precedence rules
   │   (self_report > cv > inferred; human edits always win and are pinned)
   ├─ rebuild learner_profile.snapshot, bump profile_version
   └─ emit profile.updated → integrations (learn-os event bus / webhook)
```

Human-authored values are never overwritten: `learner_personal_data.source_of_truth`
records, per field, whether the current value is `human`, `cv`, or `inferred`,
and the synthesizer only writes fields it outranks.

## 5.4 Beat schedule

Kept deliberately small. Every periodic job costs attention when it misfires, so
only the three the system cannot run correctly without are scheduled; the rest
are admin endpoints, run when there is a reason.

| task | cadence | why it must be periodic |
| --- | --- | --- |
| `profile.refresh_stale_profiles` | every 15 min | drains `stale_dimensions` — the debounce that turns a 20-card meeting into one recompute, and the safety net for recomputes a crashed worker dropped |
| `maintenance.retry_failed_documents` | every 30 min | transient LLM/storage failures are the common case; without this they need a human |
| `maintenance.reconcile_vectors` | hourly | the only guard on our single dual write (Postgres → Qdrant). A crash between the two silently removes evidence from every future recompute |

Run on demand instead of on a timer (`/v1/admin/*`):
confidence decay, card expiry, full resynthesis after a taxonomy or prompt
change, re-embedding, on-prem archive mirroring, export cleanup. Each becomes a
beat entry only when there is production data showing it needs one.

## 5.5 Backfill & replay (the reason L1 exists)

`admin.replay(scope)` enqueues onto `maintenance`:
- `--source meeting_transcript --extractor-version '<2.1'` → re-extract only
  documents processed by an older extractor; new cards supersede old ones.
- `--reembed --collection learner_memory_v2` → rebuild a new Qdrant collection
  from the ledger, verify counts, then point `QDRANT_COLLECTION` at it.
- `--rebuild-profiles --taxonomy-version 2` → resynthesize everything after a
  taxonomy change.

All three are resumable, rate-limited, and report progress in `job_run`.
