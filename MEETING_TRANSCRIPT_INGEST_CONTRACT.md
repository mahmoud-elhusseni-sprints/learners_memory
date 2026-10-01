# Meeting transcript ingest — caller contract

For the learn-os service that reads diarized meeting transcripts and posts them to
the learner-memory service. One transcript covers **several learners at once**;
we extract memory cards for each of them in a single pass. This document is the
shape of what you send us.

**Endpoint:** `POST /v1/ingest/meeting_transcript`

Any meeting works — sprint planning, standup, retro, follow-up, technical
discussion, and so on. The `meeting_type` is context you pass through, not a fixed
list we enforce.

---

## 1. What makes this source different

Unlike an assessment, a meeting has **no single owner**. So:

- You do **not** send `learner_id` on the envelope. You send a **participant
  roster** in `metadata` instead (§3).
- Each produced card is attributed to exactly one learner, by matching the
  diarized speaker label to the roster. A speaker who is not in the roster — a
  mentor, a facilitator, or a learner you did not list — gets **no cards**.
- The document is stored with no owner; the roster travels in its metadata.

---

## 2. Request

### Headers

```
Content-Type: application/json
Authorization: Bearer <jwt>       # required; must carry scope memory:write
X-Organization-Id: <uuid>         # ONLY when the service runs with AUTH_DISABLED=true
```

### Envelope

| field | type | required | notes |
|---|---|---|---|
| `occurred_at` | ISO-8601 datetime | **yes** | when the meeting **happened**, not when you send it. Drives recency decay. |
| `payload` | object or string | **yes** | the transcript (§4). Inline only. |
| `metadata.participants` | object | **yes** | speaker-label → learner uuid map (§3). Non-empty. |
| `metadata.meeting_type` | string | no | e.g. `standup`, `retro`, `sprint_planning`. Recorded on every card. |
| `metadata.title` | string | no | recorded on every card. |
| `metadata.meeting_id` | string | no | your id for the meeting. Recorded on every card. |
| `learner_id` | uuid | no | **omit it.** A meeting has no single owner; sending one is ignored for attribution. |
| `external_id` | string | no | your own id for this transcript. Used for deduplication — send it. |
| `program_id`, `cohort_id` | uuid | no | |

```jsonc
{
  "occurred_at": "2026-03-01T09:00:00Z",
  "external_id": "meeting-2026-03-01-standup",
  "metadata": {
    "meeting_type": "standup",
    "title": "Daily standup — Team Phoenix",
    "meeting_id": "mtg-4471",
    "participants": {
      "SPEAKER_01": "22222222-2222-2222-2222-222222222222",
      "SPEAKER_02": "33333333-3333-3333-3333-333333333333"
    }
  },
  "payload": { /* §4 */ }
}
```

---

## 3. The participant roster

`metadata.participants` maps a **diarized speaker label** to a **learn-os learner
uuid**:

```jsonc
"participants": {
  "SPEAKER_01": "<learner-uuid>",
  "SPEAKER_02": "<learner-uuid>"
}
```

- The **keys must match the speaker labels in the transcript exactly** (§4). If
  your diarizer emits `SPEAKER_01`, the roster key is `SPEAKER_01`.
- **List only the learners you want cards for.** Omit mentors, facilitators,
  managers, and guests — their turns are still read as context (they shape what a
  learner's reply means) but no card is ever attributed to them.
- We filter the roster to learners **registered** in your organization. A speaker
  whose uuid is not registered is dropped, and that learner gets no cards this run
  (see §6). A malformed uuid is dropped without failing the request.

> **Do not send participant names or emails.** Learners are identified by the
> roster; card content is kept free of identifying details. Names in the roster
> serve no purpose and should not be transmitted.

---

## 4. The `payload` — the transcript

Two shapes parse:

### 4.1 Diarized JSON (preferred)

```jsonc
{
  "segments": [
    { "speaker": "SPEAKER_01", "text": "I finished the auth migration and unblocked the review." },
    { "speaker": "SPEAKER_02", "text": "I'm blocked on the flaky integration test." },
    { "speaker": "MENTOR",     "text": "Let's pair on it after standup." }
  ]
}
```

| field | aliases | notes |
|---|---|---|
| `segments` | `transcript` | array of turns, in spoken order |
| `speaker` | — | the diarization label; must match a roster key to be attributed |
| `text` | — | what was said. Empty turns are skipped. |

Send the turns **in order** and keep the speaker labels **stable and consistent**
across the whole transcript — attribution and the deterministic card ids both
depend on them.

### 4.2 Plain text

If you send a string instead of JSON, it is used as-is. Put one turn per line as
`SPEAKER: text` so speakers can still be told apart:

```
SPEAKER_01: I finished the auth migration and unblocked the review.
SPEAKER_02: I'm blocked on the flaky integration test.
```

---

## 5. Response

**`202 Accepted`** — the transcript is archived and extraction is queued.

```jsonc
{ "document_id": "8a5ab4cc-629b-4968-a63d-9d5e1299448b",
  "source_type": "meeting_transcript",
  "status": "received",
  "duplicate": false }
```

| status | meaning |
|---|---|
| `received` | queued for extraction — at least one participant is a registered learner |
| `pending_identity` | archived, but **none** of the roster's learners are registered yet. Nothing is lost; re-post once they are registered (see §6). |

**Other codes**

| code | cause |
|---|---|
| `400` | unknown source type |
| `401` | missing or invalid bearer token |
| `403` | token lacks `memory:write` |
| `422` | `metadata.participants` missing or empty, or the envelope failed validation (missing `occurred_at` or `payload`) |

Extraction is **asynchronous** — a `202` does not mean cards exist yet. Poll
`GET /v1/ingest/documents/{document_id}` for `status` and `error`.

---

## 6. Known gaps

- **Unregistered participants get nothing.** If a learner in the roster is not yet
  registered when you post, they are dropped and no cards are produced for them —
  even though cards *are* produced for the registered participants in the same
  meeting. To pick them up later, register the learner and
  `POST /v1/ingest/documents/{document_id}/reprocess`. There is no automatic
  backfill for individual participants yet; if you need one, say so.
- **Attribution rests on diarization quality.** Cards are only as well-attributed
  as your speaker labels. If two learners are merged under one label, their
  evidence will be merged too.

---

## 7. Deduplication and re-sending

- Identical bytes are deduplicated by content hash — a retry is free and safe.
- `external_id` deduplicates per `(source_type, external_id)`, so send it.
- **Keep speaker labels stable across re-sends.** Card ids are derived from the
  document, the turn range and the resolved learner, so stable labels keep a
  reprocess idempotent instead of producing near-duplicate cards.
- To force re-extraction of a transcript we already hold, use
  `POST /v1/ingest/documents/{document_id}/reprocess` rather than re-posting.

---

## 8. Checklist before you send

- [ ] `occurred_at` is when the meeting happened
- [ ] `metadata.participants` maps each learner's speaker label to their learn-os uuid
- [ ] roster keys match the transcript's speaker labels exactly
- [ ] mentors/facilitators/guests are **omitted** from the roster
- [ ] `external_id` is set and stable for this transcript
- [ ] speaker labels are stable and consistent throughout the transcript
- [ ] no participant name, email or other identifying detail anywhere
