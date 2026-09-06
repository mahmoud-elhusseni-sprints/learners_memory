# Repository structure

```
learner-memory/
├── docs/
│   ├── skills_taxonomy_framework.md      # source of truth for the 34 subskills
│   └── architecture/                     # this design
├── alembic/{env.py,versions/}
├── deploy/
│   ├── Dockerfile                        # one image, five commands
│   ├── prometheus/prometheus.yml
│   └── grafana/{provisioning,dashboards}
├── docker-compose.yml                    # api · workers · beat · pg · redis ·
│                                         # qdrant · supabase storage · prom · grafana
├── scripts/{seed_taxonomy.py,bootstrap.py}
├── src/learner_memory/
│   ├── main.py                     # app factory + lifespan (loads extractors,
│   │                               #   ensures Qdrant collection and bucket)
│   ├── api/
│   │   ├── deps.py                 # AuthContext, session, repositories
│   │   └── v1/
│   │       ├── learners.py         # register (caller-supplied id) + lookup
│   │       ├── ingest.py           # POST /ingest/{source_type}
│   │       ├── memory.py           # search / get / correct / retract cards
│   │       ├── profile.py          # profile read + human-authored writes
│   │       ├── evidence.py         # raw document access, signed URLs
│   │       └── admin.py            # backfill, reindex, replay, dead letters
│   ├── core/
│   │   ├── config.py               # one Settings, one cached factory
│   │   ├── logging.py              # structlog + correlation id
│   │   ├── security.py             # JWT/JWKS verification
│   │   └── taxonomy.py             # parses the markdown → 34 frozen skills
│   ├── db/
│   │   ├── base.py                 # DeclarativeBase + UUIDPk/Timestamps/OrgScoped
│   │   ├── session.py              # engine, sessionmaker, unit_of_work
│   │   ├── models/{learner.py,raw.py}
│   │   └── repositories/{base.py,learner.py,card.py,document.py}
│   ├── schemas/
│   │   ├── memory_card.py          # THE unified card envelope
│   │   ├── learner.py              # register / ref / response DTOs
│   │   ├── ingest.py · profile.py · events.py
│   ├── extractors/
│   │   ├── base.py                 # BaseExtractor — the whole pipeline, once
│   │   ├── registry.py             # @register + load_extractors()
│   │   ├── chunking.py             # Whole / Transcript / Section strategies
│   │   ├── prompt_loader.py        # prompts/<source>/<version>.{system,user}.j2
│   │   ├── prompts/                # _shared/ + per-source overrides
│   │   └── sources/                # ONE FILE PER SOURCE — nothing else to edit
│   │       ├── task_review.py
│   │       └── meeting_transcript.py
│   ├── llm/client.py               # LiteLLM proxy adapter + Langfuse traces
│   ├── vector/qdrant.py            # collection, payload indexes, retrieval
│   ├── storage/{base.py,supabase.py,paths.py}   # single bucket, deterministic keys
│   ├── profile/{synthesizer.py,aggregation.py,career.py,prompts/}
│   ├── workers/
│   │   ├── celery_app.py           # queues, routes, small beat schedule
│   │   └── tasks/{base.py,ingest.py,profile.py,maintenance.py}
│   ├── integrations/               # outbound events to learn-os
│   └── utils/
└── tests/{unit,integration,fixtures}/
```

## Layering rule

`api` and `workers` are both thin entrypoints. They call services (`extractors`,
`profile`, `db.repositories`) and never touch a driver directly. Drivers (`db`,
`vector`, `storage`, `llm`) never import from `api`/`workers`. That is what makes
a use case callable from an HTTP handler, a worker or a script with identical
behaviour — and testable without a live Qdrant.

```
api ─┐
     ├─► extractors / profile / repositories ─► db · vector · storage · llm ─► core
workers ─┘
```

## Design patterns, and what each one buys

| pattern | where | what it prevents |
| --- | --- | --- |
| **Registry + self-registering plugin** | `extractors/registry.py` (`@register`, `load_extractors()`) | a new source touching the API, workers, Qdrant or the synthesizer — see below |
| **Template Method** | `BaseExtractor.run()` | six copies of parse→chunk→prompt→validate→stamp drifting apart |
| **Strategy** | `extractors/chunking.py` | chunking logic duplicated per source |
| **Port/Adapter** | `storage/base.py`, `llm/client.py`, `vector/qdrant.py` | vendor lock-in; swapping Supabase for S3, or the proxy for another, is one new adapter |
| **Repository + Unit of Work** | `db/repositories/`, `db/session.py` | a query that forgets `organization_id`; half-committed transactions |
| **Factory (cached)** | `get_settings`, `get_storage`, `get_card_index`, `get_llm_client` | scattered client construction and connection churn |
| **Outbox / reconciliation** | ledger write → Qdrant → `vector_synced_at`, swept hourly | silent evidence loss on our only dual write |
| **Idempotency ledger** | `workers/tasks/base.py` (`IdempotentTask`, `claim`) | duplicated work and duplicated cards on retry or replay |
| **Read model** | `learner_profile.snapshot` | the profile endpoint fanning out to Qdrant on every request |

### Adding a source costs exactly one file

```python
# src/learner_memory/extractors/sources/podcast.py
@register
class PodcastExtractor(BaseExtractor):
    source_type = SourceType.PODCAST      # + one enum member
    version = "podcast@1.0"
    payload_model = PodcastPayload        # source-specific payload, declared here
    chunker = TranscriptChunker()         # reuse an existing strategy

    def parse(self, data): ...            # only if the bytes aren't already text
```

Plus a prompt template if the shared one doesn't fit
(`prompts/podcast/v1.user.j2`). Nothing else changes: `load_extractors()` imports
the package at startup, the decorator registers the class, and the ingest
endpoint, the worker, the card schema, the Qdrant payload and the profile
synthesizer are all untouched — they only ever see the unified card envelope.
