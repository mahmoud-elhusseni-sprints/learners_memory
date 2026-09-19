# The unified memory card

Every source produces the *same* envelope. Only `payload` varies by source.
This is the contract that lets one Qdrant collection, one retrieval API and one
profile synthesizer serve six-plus wildly different evidence types.

## 3.1 Schema

```python
class MemoryCard(BaseModel):
    # ---- identity ----
    id: UUID                        # deterministic: uuid5(ns, doc_id + chunk anchor
                                    # + title) — re-extraction upserts, never dupes
    schema_version: str             # "1.0"

    # ---- tenancy & subject ----
    organization_id: UUID
    learner_id: UUID
    program_id: UUID | None
    cohort_id: UUID | None

    # ---- provenance (the chain back to L1) ----
    source_type: SourceType         # meeting_transcript | task_review | chat | ...
    source_document_id: UUID        # FK → raw_document
    source_ref: SourceRef           # {locator, span, timestamp} inside the doc
    observed_at: datetime           # when the behaviour happened (not ingest time)
    ingested_at: datetime
    extractor_version: str
    prompt_version: str
    model: str

    # ---- the insight ----
    card_type: CardType             # observation | skill_evidence | preference |
                                    # goal | fact | milestone | risk
    title: str                      # <= 120 chars, human scannable
    content: str                    # 1-3 sentences, self-contained, THIS is embedded
    evidence_quote: str | None      # verbatim snippet supporting the claim
    sentiment: Literal["positive","neutral","negative"] | None

    # ---- direction: where this card contributes ----
    contributions: list[Contribution]

    # ---- quality signals ----
    confidence: float               # 0..1, extractor's own certainty
    salience: float                 # 0..1, how much this should move the needle
    validity: Validity              # {valid_from, valid_until|None, superseded_by}

    # ---- source-specific ----
    payload: dict                   # validated against the source's payload model
    tags: list[str]                 # free-form: topics, tech, project names
    pii_level: Literal["none","low","high"]


class Contribution(BaseModel):
    target: Literal["general_skill", "technical_skill",
                    "personal_data", "learning_preference",
                    "career_goal", "learning_journey", "journey_step"]
    key: str                        # "critical_thinking" | "python" | "email" ...
    level_signal: int | None        # 1..6 on the taxonomy scale, if assessable
    weight: float                   # 0..1 contribution strength
    direction: Literal["supports","contradicts"] = "supports"
```

`contributions` is the "directed" part of the request: a card about a learner
challenging an assumption in a design review carries
`[{general_skill, critical_thinking, L4, 0.8}, {general_skill, influence, L3, 0.4}]`,
so the synthesizer only re-runs the two dimensions that actually changed.

Profile synthesis is incremental. Each flattened `card_contribution` stores the
`memory_card.updated_at` version last supplied to the profile agent. A contribution
is fresh when that marker is missing or older than the card, so the same card can be
consumed independently by each `target:key` without being synthesized twice.

`key` for `general_skill` is validated against `core.taxonomy` (34 frozen slugs).
Anything the extractor invents that isn't in the taxonomy is dropped into
`tags` instead of silently becoming a fake skill.

Other targets use stable keys: a normalized technical-skill label, a personal
data field, a learning-preference JSON path, a career/journey field, or a stable
journey-step id/slug. This makes stale-dimension scheduling and patch validation
deterministic across extractors.

## 3.2 Per-source payloads

| source_type | payload fields | typical card types |
| --- | --- | --- |
| `meeting_transcript` | `meeting_id, title, role_in_meeting, speaker_id, turn_range, participants, duration_s` | observation, skill_evidence, milestone |
| `task_review` | `task_id, reviewer_id, rubric_scores{}, verdict, iteration, repo_url, diff_stats` | skill_evidence, risk |
| `chat` | `channel_id, thread_id, message_ids[], counterparties[]` | observation, preference |
| `assessment` | `assessment_id, item_results[], score, max_score, duration_s, attempt` | skill_evidence |
| `cv` | `section, employer, role, period, institution, degree, extracted_skills[]` | fact, skill_evidence |
| `self_report` | `form_id, question, answer_raw` | preference, goal, fact |
| `mentor_feedback` | `mentor_id, session_id, rating, focus_areas[]` | observation, skill_evidence |

Adding a source is **one file** in `extractors/sources/`: a payload model, a
class with `@register`, and a prompt template if the shared one doesn't fit. No
registry edit, no schema migration, no change to Qdrant, the ingest API or the
synthesizer — they only ever see the envelope. See
[02-repo-structure](02-repo-structure.md#adding-a-source-costs-exactly-one-file).

## 3.3 Card lifecycle

`extracted` → `indexed` → (`superseded` | `retracted` | `expired`)

Cards are **append-only**. A correction (human or re-extraction) writes a new
card and sets `superseded_by` on the old one; the old card stays retrievable for
audit but is excluded from synthesis by a Qdrant filter on `status`. Retraction
(e.g. GDPR, or a mis-attributed speaker) tombstones the card and must trigger the
separate full-rebuild path; the incremental agent only applies fresh active evidence.
