#!/usr/bin/env python
"""One-shot bootstrap: Qdrant collection + payload indexes, storage bucket."""
from __future__ import annotations

import asyncio

from learner_memory.storage.supabase import get_storage
from learner_memory.vector.qdrant import get_card_index


async def main() -> None:
    await get_card_index().ensure_collection()
    get_storage().ensure_bucket()
    print("bootstrap complete")


if __name__ == "__main__":
    asyncio.run(main())
