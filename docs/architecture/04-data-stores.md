# Data stores

Three stores, three jobs. The rule of thumb: **Postgres is the ledger, Qdrant is
the index, Storage is the archive.** Nothing is only in Qdrant.

## 4.1 Supabase Storage — the raw archive (L1)

**One private bucket, one object per uploaded file.** No derived copies, no
sidecar files — all metadata lives in `raw_document`, so the object is exactly
the bytes we received.

```
bucket: learner-evidence
key:    org/{organization_id}/{source_type}/{YYYY}/{MM}/{DD}/{document_id}.{ext}
```

The key is deterministic (`storage/paths.py`), so re-uploading the same document
overwrites the same object rather than accumulating duplicates, and the prefix
tree stays walkable per org, per source, per day without touching the database.

**On-prem retrievability:** `scripts/replay_archive.py --org --from --to --out
/mnt/nas` streams objects into a local tree with a manifest of
`(document_id, key, sha256)` taken from `raw_document`. Because L2 and L3 are
derived, that tree plus a database dump is a complete, replayable backup.

## 4.2 PostgreSQL — ledger + profile (L3)

SQLAlchemy 2.0 async + Alembic. Every table carries `organization_id`,
`created_at`, `updated_at`. Row-level tenancy enforced in repositories (and
optionally Postgres RLS when Supabase-hosted).

### Raw / ledger
```sql
raw_document(
  id uuid pk, organization_id uuid, learner_id uuid null, source_type text,
  external_id text, content_sha256 text, storage_key text,
  mime_type text, size_bytes bigint,
  occurred_at timestamptz, received_at timestamptz,
  status text,     -- received|extracting|extracted|failed|pending_identity|quarantined
  extractor_version text null, error jsonb null, metadata jsonb,
  unique(organization_id, source_type, external_id),
  unique(organization_id, content_sha256)
);
memory_card(                            -- ledger mirror of every Qdrant point
  id uuid pk, organization_id uuid, learner_id uuid,
  source_document_id uuid references raw_document,
  source_type text, card_type text, title text, content text,
  observed_at timestamptz, confidence real, salience real,
  status text,                          -- extracted|indexed|superseded|retracted|expired
  superseded_by uuid null, vector_synced_at timestamptz null,
  extractor_version text, prompt_version text, model text,
  contributions jsonb, payload jsonb, tags text[]
);
card_contribution(                      -- flattened for cheap "what feeds X" queries
  card_id uuid, target text, key text, level_signal smallint null,
  weight real, direction text, primary key(card_id, target, key)
);
```

### Profile
```sql
learner(                                -- id is SUPPLIED BY THE CALLER, stored verbatim
  id uuid pk, organization_id uuid, external_user_id text, display_name text,
  status text, program_id uuid, cohort_id uuid, registered_at timestamptz,
  metadata jsonb,
  unique(organization_id, external_user_id));

learner_personal_data(                  -- PII island, separately encrypted/auditable
  learner_id uuid pk, email citext, phone text, location jsonb,
  contact jsonb, education jsonb, experience jsonb,
  languages jsonb, demographics jsonb, source_of_truth jsonb);

learning_preference(learner_id uuid pk, preferences jsonb, updated_by text);

skill_catalog(                          -- seeded from docs/skills_taxonomy_framework.md
  id uuid pk, kind text,                -- general|technical
  category text, slug text unique, name text,
  level_descriptors jsonb,              -- L1..L6 text
  best_evidence text[], taxonomy_version text, active bool);

skill_assessment(                       -- append-only, one row per recompute
  id uuid pk, organization_id uuid, learner_id uuid, skill_id uuid,
  level smallint,                       -- 1..6
  confidence real, evidence_card_ids uuid[], rationale text,
  method text,                          -- llm_synthesis|assessment_score|self_report|mentor
  synthesizer_version text, taxonomy_version text,
  computed_at timestamptz, superseded bool default false);

learner_technical_skill(                -- learner-declared + evidence-confirmed
  id uuid pk, learner_id uuid, skill_id uuid null, label text,
  declared_level smallint null, assessed_level smallint null,
  confidence real, evidence_card_ids uuid[], last_evidence_at timestamptz);

career_goal(
  id uuid pk, learner_id uuid, title text, target_role text,
  target_date date null, motivation text, status text, details jsonb);

learning_journey(
  id uuid pk, learner_id uuid, career_goal_id uuid, name text,
  status text, progress numeric, plan jsonb);

journey_step(
  id uuid pk, journey_id uuid, ord int, title text, kind text,
  status text,                          -- planned|in_progress|done|skipped
  completed_at timestamptz null, evidence_card_ids uuid[], details jsonb);

learner_profile(                         -- denormalized read model, 1 row/learner
  learner_id uuid pk, organization_id uuid,
  snapshot jsonb,                        -- full profile as served by the API
  profile_version int, computed_at timestamptz, stale_dimensions text[]);
```

`jsonb` columns (`details`, `plan`, `preferences`, `snapshot`) are the escape
hatch for future dynamicity, exactly as asked — structured where we query,
JSON where we evolve. Add a GIN index only on the ones actually filtered.

### Jobs
```sql
job_run(id uuid pk, task text, idempotency_key text unique, status text,
        args jsonb, attempts int, started_at, finished_at, error jsonb);
dead_letter(id uuid pk, task text, args jsonb, error jsonb, created_at, replayed_at);
```

## 4.3 Qdrant — memory card index (L2)

```
collection: learner_memory_v1        (QDRANT_COLLECTION; re-embedding builds
                                      learner_memory_v2 and swaps the setting)
vectors:    { "content": 1024d cosine }
point id:   the card UUID (same as the ledger) — makes reconciliation trivial
```

Payload (mirrors the card envelope; everything we filter on gets a payload index):

```json
{
  "organization_id": "...", "learner_id": "...", "program_id": "...",
  "source_type": "meeting_transcript", "source_document_id": "...",
  "card_type": "skill_evidence", "status": "indexed",
  "observed_at": 1757116800, "confidence": 0.82, "salience": 0.7,
  "contribution_keys": ["general_skill:critical_thinking",
                        "general_skill:influence"],
  "tags": ["design-review","python"], "title": "...", "content": "...",
  "schema_version": "1.0", "extractor_version": "meeting@2.1"
}
```

Indexed payload fields: `organization_id`, `learner_id`, `source_type`,
`card_type`, `status`, `contribution_keys` (keyword), `observed_at` (integer
range), `tags`.

`contribution_keys` is the flattened `target:key` list — it turns "give me
every card feeding *critical thinking* for this learner in the last 180 days"
into a single filtered search with no join.

Two access patterns, both served here:
- **Directed retrieval** (synthesizer): filter-only scroll, no query vector,
  ordered by `observed_at` — deterministic and cheap.
- **Semantic retrieval** (API, coaching agents): embed the query, filter by org +
  learner, hybrid rerank on `salience × recency`.

### Consistency between Postgres and Qdrant
Qdrant is not transactional with Postgres, so we use the outbox pattern:
write the ledger row in the same transaction as the document status, then upsert
to Qdrant, then stamp `vector_synced_at`. A `reconcile_vectors` maintenance task
sweeps rows where `status='extracted' AND vector_synced_at IS NULL` (or
`updated_at > vector_synced_at`) and re-upserts. Orphan points in Qdrant with no
ledger row are deleted. Postgres always wins.
