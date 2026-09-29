# Task review ingest — caller contract

For the learn-os service that posts a learner's reviewed task submission to the
learner-memory service: the task that was set, the learner's own work (as a list
of URLs), and the grader's written report on it. This document is the shape of
what you send us.

There is no score, verdict or rubric in this contract — the review is a narrative
report only.

**Endpoint:** `POST /v1/ingest/task_review`

This is a **single-learner** source: one submission by one learner.

---

## 1. Request

### Headers

```
Content-Type: application/json
Authorization: Bearer <jwt>       # required; must carry scope memory:write
X-Organization-Id: <uuid>         # ONLY when the service runs with AUTH_DISABLED=true
```

### Envelope

| field | type | required | notes |
|---|---|---|---|
| `learner_id` | uuid | **yes** | the learn-os learner uuid. There is no secondary key — carry it. |
| `occurred_at` | ISO-8601 datetime | **yes** | when the submission was **reviewed**, not when you send it. Drives recency decay. |
| `payload` | object | **yes** | the task, submission and review (§2). Inline JSON only. |
| `external_id` | string | no | your own id for this review. Used for deduplication — send it. |
| `metadata` | object | no | passed through. `task_title`, `verdict` and `iteration` are read if the payload omits them. |
| `program_id`, `cohort_id` | uuid | no | |

```jsonc
{
  "learner_id": "22222222-2222-2222-2222-222222222222",
  "occurred_at": "2026-03-01T10:00:00Z",
  "external_id": "task-42-iter-2",
  "payload": { /* §2 */ }
}
```

---

## 2. The `payload`

The payload has three parts — the task, the submission, and the review. You may
send them as **nested objects** (`task`, `submission`, `review`) or **flat** in
one object. Both parse; a top-level key wins over the same key inside a section.

```jsonc
{
  "task": {
    "headline": "Build a rate limiter",
    "task_number": 42,
    "workstream": "backend",
    "description": "Implement a token-bucket rate limiter with tests.",
    "technologies": ["concurrency", "api design"],
    "functional_requirements": ["Reject requests over the configured rate"],
    "non_functional_requirements": ["Thread-safe under concurrent callers"],
    "acceptance_checks": ["Refill path has a passing test"],
    "definition_of_done": ["Code reviewed", "Tests green"],
    "deliverables": {
      "format": "pull_request",
      "expected_repo_paths": ["src/rate_limiter.py"]
    },
    "dependencies": {
      "depends_on_previous_tasks": [41],
      "integration_notes": "Builds on the token-bucket task."
    }
  },
  "submission": ["https://git.example/pr/9"],
  "review": {
    "report": "Approach is sound but there are no tests for the refill path.",
    "reviewer_id": "rev-7",
    "iteration": 2
  }
}
```

### Fields

Every field is optional **except a non-empty `submission` and a non-empty
`report`** — without the learner's work there is nothing to point at, and without
the grader's report there is no account of quality to read. A payload missing
either one is rejected. Aliases are accepted because the producer is outside our
release cycle.

| field | aliases | why it matters |
|---|---|---|
| `headline` | `title`, `name`, `task_title` | recorded on every card |
| `task_number` | | recorded on the card payload |
| `workstream` | | recorded on the card payload |
| `description` | `prompt`, `brief`, `instructions` | what was asked — context for the submission |
| `technologies` | `topics`, `tags`, `skills` | recorded on the card payload |
| `functional_requirements` | | context for the submission |
| `non_functional_requirements` | | context for the submission |
| `acceptance_checks` | | context for the submission |
| `definition_of_done` | | context for the submission |
| `deliverables` | | object: `format`, `max_pages`, `required_sections`, `expected_effort_hours`, `expected_repo_paths` |
| `dependencies` | | object: `depends_on_previous_tasks`, `integration_notes` |
| `assets` | | list of asset objects, passed through |
| `review_notes` | | free-form notes on the task itself |
| `submission` | `submission_urls`, `urls`, `links`, `repo_urls` | **a list of URLs to the learner's work** (repo, PR, deployed app, ...). A single string is also accepted and wrapped in a list. |
| `report` | `review_report`, `grader_report`, `feedback`, `comment`, `notes` | the grader's written report — **the only account of quality here.** There is no score, verdict or rubric; send the actual prose. |
| `reviewer_id` | `grader_id`, `reviewer` | recorded on the card payload |
| `iteration` | `attempt`, `revision` | rework across iterations is evidence about follow-through |

Omit a field rather than sending `null` or `""` — both are treated as absent.

> **Do not send the reviewer's or learner's name or email.** The learner is
> identified by `learner_id`; card content is kept free of identifying details.

Send the **full list of submission URLs** and the grader's **actual report** —
the agent reads the report's concrete observations, since the submission itself is
only links, not text it can read directly.

---

## 3. Response

**`202 Accepted`** — the document is archived and extraction is queued.

```jsonc
{ "document_id": "8a5ab4cc-629b-4968-a63d-9d5e1299448b",
  "source_type": "task_review",
  "status": "received",
  "duplicate": false }
```

| status | meaning |
|---|---|
| `received` | queued for extraction |
| `pending_identity` | archived, but that `learner_id` is not registered yet. **No cards are produced, and nothing picks the document up later** — see §5. |

**Other codes**

| code | cause |
|---|---|
| `400` | unknown source type |
| `401` / `403` | missing/invalid bearer token, or it lacks `memory:write` |
| `422` | envelope failed validation (missing `learner_id`, `occurred_at` or `payload`) |

Extraction is **asynchronous** — a `202` does not mean cards exist yet. Poll
`GET /v1/ingest/documents/{document_id}` for `status` and `error`. A payload with
an empty submission, or no grader report, fails extraction with a clear error
rather than producing nothing.

---

## 4. Deduplication and re-sending

- Identical bytes are deduplicated by content hash — a retry is free and safe.
- `external_id` deduplicates per `(source_type, external_id)`, so send it. For a
  re-reviewed submission, include the iteration in the id (e.g. `task-42-iter-2`)
  so a new iteration is not mistaken for a duplicate of the first.
- To force re-extraction of a document we already hold, use
  `POST /v1/ingest/documents/{document_id}/reprocess` rather than re-posting.

---

## 5. Known gaps

- **A document posted before the learner is registered produces nothing.** It is
  archived at `pending_identity`, but there is no automatic backfill and no manual
  recovery today: the unregistered `learner_id` is not kept as the document's
  owner, so `POST /v1/ingest/documents/{document_id}/reprocess` parks it again,
  and a byte-identical re-post is deduplicated back to the same parked document.
  **Register the learner before you post.** If you need a resolver that picks
  parked documents up on registration, say so.

---

## 6. Checklist before you send

- [ ] `learner_id` is the learn-os uuid and that learner is **already registered** (§5)
- [ ] `occurred_at` is when the submission was reviewed
- [ ] `external_id` is set, stable, and distinguishes iterations
- [ ] `submission` is a non-empty list of URLs to the learner's actual work
- [ ] `report` carries the grader's actual written report, in full
- [ ] no reviewer or learner name, email or other identifying detail anywhere
