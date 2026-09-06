# Learner Memory & Profile

learn-os sub-repo owning learner evidence, memory cards and the learner profile.

Three layers: an immutable **raw evidence archive** (Supabase Storage), a unified
**memory card** index (Qdrant + Postgres ledger), and a derived **learner
profile** (PostgreSQL). L2 and L3 are fully rebuildable from L1.

Start with [docs/architecture/](docs/architecture/README.md).
