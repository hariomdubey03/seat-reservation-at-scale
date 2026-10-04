"""Initialize the schema under a transaction-scoped advisory migration lock."""

import asyncio
import os
from pathlib import Path

import psycopg


async def migrate() -> None:
    schema = Path(__file__).with_name("schema.sql").read_text()
    for attempt in range(30):
        try:
            async with await psycopg.AsyncConnection.connect(
                os.environ["DATABASE_URL"], connect_timeout=3
            ) as conn:
                await conn.execute("SELECT pg_advisory_xact_lock(73915301)")
                await conn.execute(schema)
            print("Schema version 1 ready", flush=True)
            return
        except psycopg.OperationalError:
            if attempt == 29:
                raise
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(migrate())
