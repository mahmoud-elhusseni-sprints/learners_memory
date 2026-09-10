# API surface & communication

FastAPI, `/v1`, async throughout. Auth: service-to-service JWT (learn-os issuer)
carrying `organization_id`, `subject`, `scopes`. `organization_id` is **always**
taken from the token, never the body.

## 6.1 Endpoints

### Learners (scope `profile:write` / `profile:read`)
```
POST /v1/learners                        # register — CALLER SUPPLIES THE ID
GET  /v1/learners/{learner_id}
```
```jsonc
// POST /v1/learners
{ "id": "0f7c...-uuid-issued-by-learn-os",   // stored verbatim as the PK
  "display_name": "…", "program_id": "…", "cohort_id": "…", "metadata": {} }
```
The learner id is **never generated here**. The upstream learn-os service issues
it and we persist it as the primary key, so every service refers to a learner by
the same uuid — no local id, no mapping table, no drift. Registration is
idempotent: re-posting the same id refreshes the mutable fields and returns
`200` with `created: false` instead of erroring, so a retried call is safe.
Registration also creates the empty personal-data and profile rows, so reads
never have to special-case a learner who has been registered but not yet
processed.

Ingest identifies the learner by that same `learner_id` — it is the only
learner key in the system, so producers must carry the learn-os uuid. Evidence
for a learner who is not yet registered is archived and parked at
`pending_identity` rather than rejected.

### Ingestion (scope `memory:write`)
```
POST /v1/ingest/{source_type}            # inline payload  → 202 {document_id, status}
POST /v1/ingest/{source_type}/upload-url # → presigned Supabase URL + document_id
POST /v1/ingest/{source_type}/complete   # confirm upload, enqueue pipeline
GET  /v1/ingest/documents/{id}           # status, error, produced card ids
POST /v1/ingest/documents/{id}/reprocess # force re-extraction
```
#### Coderbyte assessments

`POST /v1/ingest/coderbyte_assessment` takes the JSON a learn-os reader service
pulls from Coderbyte — we never call Coderbyte ourselves. `payload` carries the
assessment; `questions` may sit beside `assessment` or inside it.

```jsonc
{ "learner_id": "…", "occurred_at": "2026-03-01T10:00:00Z",
  "external_id": "cb-run-001",
  "metadata": { "assessment_name": "…", "role_target": "…" },
  "payload": {
    "assessment": { "id": "…", "name": "…", "score": 72, "max_score": 100,
                    "percentile": 64, "duration_seconds": 2700 },
    "questions": [ { "id": "q1", "type": "coding|mcq|open_ended",
                     "title": "…", "prompt": "…", "topics": ["…"],
                     "language": "python",              // coding
                     "answer": "…",                     // the learner's own work
                     "options": [{"id": "A", "text": "…", "selected": true},
                                 {"id": "B", "text": "…", "is_correct": true}],
                     "rubric":  [{"criterion": "clarity", "score": 3, "max_score": 5}],
                     "feedback": "…",                   // grader, open questions
                     "correct": true, "score": 10, "max_score": 10,
                     "test_cases": {"passed": 8, "total": 8},
                     "attempts": 1, "time_spent_seconds": 240 } ] } }
```

Every field is optional except a non-empty question list, and the common
alternative spellings are accepted (`response`/`submission` for `answer`,
`choices` for `options`, `items` for `questions`, …) — the reader service sits
outside our release cycle. Question `type` is normalised onto `coding`,
`multiple_choice` and `free_response`, and inferred from the evidence when the
label is absent.

The three shapes are not interchangeable evidence, and each is rendered with the
context that makes it readable: options are listed with the learner's pick and
the correct answer marked (which distractor was chosen is the whole signal on a
wrong MCQ), open answers carry the grader's rubric and feedback, coding answers
carry the submitted code and per-test results. **Do not send candidate name or
email** — they are dropped at parse time, but should not be transmitted.

Headers: `Idempotency-Key` (optional; content hash used otherwise).
Body envelope is identical across sources — `learner_id`, `occurred_at`,
`external_id`, `metadata`, `payload|file` — so producers integrate once.

### Memory (scope `memory:read`)
```
GET  /v1/learners/{id}/memory            # filter: source_type, card_type,
                                         #  contribution_key, from, to, cursor
POST /v1/memory/search                   # {query, learner_id?, filters, top_k}
GET  /v1/memory/cards/{card_id}
POST /v1/memory/cards/{card_id}/retract  # tombstone + trigger recompute
POST /v1/memory/cards/{card_id}/correct  # supersede with a human-authored card
```

### Profile (scope `profile:read` / `profile:write`)
```
GET  /v1/learners/{id}/profile           # ?include=skills,personal,career,journey
GET  /v1/learners/{id}/profile/skills    # 34 general subskills + technical
GET  /v1/learners/{id}/profile/skills/{slug}/evidence   # → cards → documents
PATCH/v1/learners/{id}/profile/personal  # human-authored, pins source_of_truth
PUT  /v1/learners/{id}/career-goal
PATCH/v1/learners/{id}/journey/steps/{step_id}
POST /v1/learners/{id}/profile/recompute # 202, force synthesis
GET  /v1/learners/{id}/profile/history   # assessment timeline per skill
```

### Evidence (scope `evidence:read`, audited)
```
GET  /v1/evidence/documents/{id}         # metadata
GET  /v1/evidence/documents/{id}/content # short-lived signed URL
POST /v1/evidence/export                 # async archive bundle for a learner/org
```

### Admin (scope `admin`)
```
POST /v1/admin/backfill        POST /v1/admin/reindex
GET  /v1/admin/jobs            POST /v1/admin/dead-letter/{id}/replay
GET  /healthz  /readyz  /metrics
```

## 6.2 Profile response shape

```jsonc
{
  "learner_id": "...", "profile_version": 42, "computed_at": "...",
  "personal": { "email": "...", "phone": "...", "location": {...},
                "education": [...], "experience": [...],
                "learning_preferences": {...},
                "source_of_truth": {"email": "human", "education": "cv"} },
  "skills": {
    "general": [
      { "slug": "critical_thinking", "category": "Cognitive Skills",
        "level": 4, "level_label": "Adaptive", "confidence": 0.71,
        "last_evidence_at": "2026-08-30T...", "trend": "+1 in 90d",
        "evidence_card_ids": ["...", "..."], "rationale": "..." }
      /* … 34 entries, always all 34; unassessed → level:null, confidence:0 */
    ],
    "technical": [
      { "label": "python", "declared_level": 4, "assessed_level": 3,
        "confidence": 0.65, "last_evidence_at": "..." }
    ]
  },
  "career": {
    "goal": {"target_role": "ML Engineer", "target_date": "2027-06-01", ...},
    "journey": {"name": "...", "progress": 0.38,
                "steps": [{"title": "...", "status": "done",
                           "completed_at": "...", "evidence_card_ids": [...]}]}
  }
}
```

Serving this is a single Postgres read of `learner_profile.snapshot` — Qdrant is
never on the read path for profile fetches, only for search and synthesis.

## 6.3 Communication with the rest of learn-os

**Inbound**, two supported modes:
1. *Push* — producers call the ingest API (preferred; simplest to reason about).
2. *Subscribe* — a consumer in `integrations/` reads the learn-os event
   bus/webhooks and calls the same internal ingest service. The HTTP handler is
   a thin wrapper over that service, so both paths share validation and dedupe.

**Outbound**, fire-and-forget events (published from the profile worker, retried
from the outbox):
- `learner.profile.updated` `{learner_id, profile_version, changed_dimensions[]}`
- `learner.skill.level_changed` `{learner_id, slug, from, to, confidence}`
- `learner.journey.step_completed`
- `learner.document.extraction_failed` (ops/alerting)

Consumers (recommendation, curriculum, coaching agents) then pull the full
profile from `GET /v1/learners/{id}/profile` — thin events, fat API. That keeps
the event contract stable while the profile shape evolves.
