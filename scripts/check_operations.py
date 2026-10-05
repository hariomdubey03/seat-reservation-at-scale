#!/usr/bin/env python3
"""Validate the LOCAL Compose stack: two replicas, DB outage, restart persistence.

This intentionally restarts this project's api/db containers. It never deletes volumes.
Run with ADMIN_TOKEN set and the normal Compose stack already running on port 8000.
"""

import asyncio
import json
import os
import subprocess
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    result = subprocess.run(["docker", *args], cwd=ROOT, check=True, capture_output=True, text=True)
    return result.stdout.strip()


async def ready(client):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if (await client.get("/health/ready", timeout=4)).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    raise AssertionError("Service did not become ready within 90 seconds")


def checked(response, expected=200):
    assert response.status_code == expected, response.text
    return response.json()


async def run():
    admin = {"Authorization": "Bearer " + os.environ["ADMIN_TOKEN"]}
    base_url = os.getenv("BASE_URL", "http://127.0.0.1:8000")
    if httpx.URL(base_url).host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Operations checks only support the local Compose stack")
    replica_port = int(os.getenv("REPLICA_PORT", "8001"))
    replica_name = "reservation-check-" + uuid.uuid4().hex[:12]
    replica_created = False
    report = {}
    async with (
        httpx.AsyncClient(base_url=base_url, timeout=60) as first,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{replica_port}", timeout=60) as second,
    ):
        await ready(first)
        uid = "operations-" + uuid.uuid4().hex
        token = checked(await first.post("/auth/token", headers=admin, json={"user_id": uid}))
        headers = {"Authorization": "Bearer " + token["access_token"]}
        show = checked(
            await first.post(
                "/shows",
                headers=admin,
                json={
                    "name": uid,
                    "seats": [f"A{i}" for i in range(12)],
                    "price_paise": 12345,
                },
            ),
            201,
        )
        path = f"/shows/{show['id']}/reserve"

        async def reserve(client, seats, key):
            return await client.post(
                path, headers=headers, json={"seats": seats, "idempotency_key": key}
            )

        try:
            print("Starting a second API container against the same PostgreSQL...", flush=True)
            await asyncio.to_thread(
                docker,
                "compose",
                "run",
                "--detach",
                "--no-deps",
                "--publish",
                f"127.0.0.1:{replica_port}:8000",
                "--name",
                replica_name,
                "api",
            )
            replica_created = True
            await ready(second)
            replies = await asyncio.gather(
                *(reserve(first if i % 2 else second, ["A0"], "same-key") for i in range(80))
            )
            assert Counter(r.status_code for r in replies) == {201: 1, 200: 79}
            original = next(r.json() for r in replies if r.status_code == 201)
            assert all(r.json() == original for r in replies)
            report["two_replicas_same_key"] = {"created": 1, "replayed": 79}

            replies = await asyncio.gather(
                *(reserve(first if i % 2 else second, ["A1"], f"hot-{i}") for i in range(80))
            )
            assert Counter(r.status_code for r in replies) == {201: 1, 409: 79}
            assert all(
                r.json()["error"]["code"] == "seat_taken" for r in replies if r.status_code == 409
            )
            report["two_replicas_hot_seat"] = {"created": 1, "declined": 79}

            replies = await asyncio.gather(
                *(
                    reserve(first if i % 2 else second, [f"A{i}"], f"quota-{i}")
                    for i in range(2, 12)
                )
            )
            assert Counter(r.status_code for r in replies) == {201: 2, 409: 8}
            assert all(
                r.json()["error"]["code"] == "per_user_limit"
                for r in replies
                if r.status_code == 409
            )
            state = checked(await first.get(f"/shows/{show['id']}"))
            assert state["counts"]["confirmed"] == 4
            assert checked(await second.get(f"/shows/{show['id']}")) == state
            report["two_replicas_user_limit"] = state["counts"]

            print(
                "Stopping the database; liveness must stay 200 and readiness become 503...",
                flush=True,
            )
            await asyncio.to_thread(docker, "compose", "stop", "db")
            try:
                checked(await first.get("/health/live"))
                checked(await first.get("/health/ready", timeout=5), 503)
                report["database_down"] = {"liveness": 200, "readiness": 503}
            finally:
                await asyncio.to_thread(docker, "compose", "start", "db")
            await ready(first)
            await ready(second)
            print(
                "Restarting the API and checking durable state and original replay...", flush=True
            )
            await asyncio.to_thread(docker, "compose", "restart", "api")
            await ready(first)
            assert checked(await first.get(f"/shows/{show['id']}")) == state
            assert checked(await reserve(first, ["A0"], "same-key")) == original
            report["restart_preserves_state_and_idempotency"] = True
        finally:
            if replica_created:
                await asyncio.to_thread(docker, "rm", "--force", replica_name)
    report["passed"] = True
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(run())
