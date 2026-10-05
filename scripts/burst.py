#!/usr/bin/env python3
"""Exercise a running reservation API, including a bounded 20,000-request burst."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import aiohttp
from multidict import CIMultiDictProxy


@dataclass(frozen=True)
class BufferedResponse:
    """Retain a small response after its connection is returned to the client pool."""

    status_code: int
    text: str
    headers: CIMultiDictProxy
    method: str
    path: str

    def json(self) -> dict:
        return json.loads(self.text)


class BurstClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def request(self, method: str, path: str, **kwargs) -> BufferedResponse:
        async with self.session.request(method, path, allow_redirects=False, **kwargs) as response:
            return BufferedResponse(
                response.status,
                await response.text(),
                response.headers,
                method,
                path,
            )

    async def get(self, path: str, **kwargs) -> BufferedResponse:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> BufferedResponse:
        return await self.request("POST", path, **kwargs)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="For example http://localhost:8000")
    parser.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN"))
    parser.add_argument("--requests", type=positive_int, default=20_000)
    parser.add_argument("--concurrency", type=positive_int, default=500)
    parser.add_argument("--users", type=positive_int, default=500)
    parser.add_argument("--hot-seats", type=positive_int, default=8)
    parser.add_argument("--timeout", type=positive_int, default=60)
    args = parser.parse_args()
    if not args.admin_token:
        parser.error("set ADMIN_TOKEN or pass --admin-token")
    if args.requests < args.hot_seats * 2:
        parser.error("--requests must be at least twice --hot-seats")
    if args.users < 2:
        parser.error("--users must be at least 2 for ownership checks")
    if args.hot_seats > 99:
        parser.error("--hot-seats must be at most 99")
    return args


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def checked_json(response: BufferedResponse, expected: int) -> dict:
    if response.status_code != expected:
        raise AssertionError(
            f"{response.method} {response.path}: expected "
            f"{expected}, got {response.status_code}: {response.text[:300]}"
        )
    return response.json()


def reconcile(show: dict) -> dict[str, int]:
    """Check counts against the actual seat list, not just against each other."""
    seats = show["seats"]
    counts = show["counts"]
    total = show["total_seats"]
    if len(seats) != total or len({seat["seat_number"] for seat in seats}) != total:
        raise AssertionError("seat list has a missing or duplicate seat")
    actual = Counter(seat["status"] for seat in seats)
    if actual.keys() - {"available", "held", "confirmed"}:
        raise AssertionError(f"unknown seat state: {dict(actual)}")
    for state in ("available", "held", "confirmed"):
        if type(counts[state]) is not int or counts[state] < 0:
            raise AssertionError(f"invalid {state} count: {counts[state]}")
        if actual[state] != counts[state]:
            raise AssertionError(f"seat list and {state} count disagree")
    if sum(counts[state] for state in ("available", "held", "confirmed")) != total:
        raise AssertionError("available + held + confirmed differs from total_seats")
    if counts.get("total_seats", total) != total:
        raise AssertionError("total_seats fields disagree")
    return {
        "available": counts["available"],
        "held": counts["held"],
        "confirmed": counts["confirmed"],
        "total_seats": total,
    }


@dataclass
class Results:
    statuses: Counter = field(default_factory=Counter)
    outcomes: Counter = field(
        default_factory=lambda: Counter(
            {
                "confirmed": 0,
                "seat_taken": 0,
                "per_user_limit": 0,
                "idempotent_replay": 0,
                "5xx": 0,
                "transport_failure": 0,
            }
        )
    )
    winners: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    errors: list[str] = field(default_factory=list)
    error_count: int = 0
    snapshots: int = 0
    latencies: list[float] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.error_count += 1
        if len(self.errors) < 20:
            self.errors.append(message)


class Exercise:
    def __init__(self, client: BurstClient, args: argparse.Namespace, load_client: BurstClient):
        self.client = client
        self.load_client = load_client
        self.args = args
        self.prefix = f"burst-{uuid.uuid4().hex[:12]}"
        self.tokens: list[str] = []
        self.results = Results()

    async def setup(self) -> None:
        checked_json(await self.client.get("/health/ready"), 200)
        semaphore = asyncio.Semaphore(min(20, self.args.concurrency))

        async def mint(index: int) -> str:
            async with semaphore:
                response = await self.client.post(
                    "/auth/token",
                    headers=bearer(self.args.admin_token),
                    json={"user_id": f"{self.prefix}-user-{index}"},
                )
                return checked_json(response, 200)["access_token"]

        self.tokens = list(await asyncio.gather(*(mint(i) for i in range(self.args.users))))

    async def create_show(self, label: str, seats: list[str], limit: int = 4) -> dict:
        response = await self.client.post(
            "/shows",
            headers=bearer(self.args.admin_token),
            json={
                "name": f"{self.prefix}-{label}",
                "seats": seats,
                "price_paise": 25_000,
                "per_user_limit": limit,
            },
        )
        show = checked_json(response, 201)
        counts = reconcile(show)
        if counts["available"] != len(seats):
            raise AssertionError("new show contains unavailable seats")
        return show

    async def reserve(
        self,
        show_id: str,
        seats: list[str],
        key: str,
        user: int = 0,
        extra: dict | None = None,
        client: BurstClient | None = None,
    ) -> BufferedResponse:
        payload = {"seats": seats, "idempotency_key": key}
        payload.update(extra or {})
        return await (client or self.client).post(
            f"/shows/{show_id}/reserve",
            headers=bearer(self.tokens[user]),
            json=payload,
        )

    async def state(self, show_id: str) -> dict:
        return checked_json(await self.client.get(f"/shows/{show_id}"), 200)

    async def metrics_reconciliation(self, show_id: str) -> dict:
        state = reconcile(await self.state(show_id))
        response = await self.client.get("/metrics")
        if response.status_code != 200 or "# TYPE" not in response.text:
            raise AssertionError("Prometheus endpoint is not healthy")
        for status in ("available", "held", "confirmed"):
            pattern = (
                rf'^seats_{status}\{{[^}}]*show_id="{re.escape(show_id)}"'
                rf"[^}}]*\}}\s+([0-9.eE+-]+)"
            )
            match = re.search(pattern, response.text, re.MULTILINE)
            if match is None or float(match.group(1)) != state[status]:
                raise AssertionError(f"seats_{status} metric differs from API state")
        return state

    async def storm(self) -> dict:
        hot_seats = [f"HOT{i + 1}" for i in range(self.args.hot_seats)]
        show = await self.create_show(
            "storm", hot_seats + ["REPLAY", "UNSOLD"], limit=len(hot_seats) + 1
        )
        show_id = show["id"]
        replay = checked_json(await self.reserve(show_id, ["REPLAY"], "original"), 201)
        completed = asyncio.Event()

        async def sample() -> None:
            while not completed.is_set():
                try:
                    reconcile(await self.state(show_id))
                    self.results.snapshots += 1
                except (
                    aiohttp.ClientError,
                    TimeoutError,
                    AssertionError,
                    KeyError,
                    ValueError,
                    TypeError,
                ) as error:
                    self.results.fail(f"during-burst reconciliation: {error}")
                try:
                    await asyncio.wait_for(completed.wait(), timeout=0.1)
                except TimeoutError:
                    pass

        indices = iter(range(self.args.requests))
        # The first worker wave targets one seat; remaining calls mix hot seats and retries.
        first_wave = min(self.args.concurrency, self.args.requests - 2 * len(hot_seats))

        async def worker() -> None:
            for index in indices:
                mixed_index = index - first_wave
                is_replay = mixed_index >= 0 and mixed_index % 5 == 4
                # Skipping each fifth index must not skip one of the hot seats.
                hot_index = max(0, mixed_index - mixed_index // 5)
                seat = "REPLAY" if is_replay else hot_seats[hot_index % len(hot_seats)]
                user = 0 if is_replay else index % len(self.tokens)
                key = "original" if is_replay else f"storm-{index}"
                request_started = time.perf_counter()
                try:
                    response = await self.reserve(
                        show_id, [seat], key, user, client=self.load_client
                    )
                except (aiohttp.ClientError, TimeoutError) as error:
                    self.results.outcomes["transport_failure"] += 1
                    self.results.fail(f"request {index}: {type(error).__name__}: {error}")
                    continue
                self.results.latencies.append((time.perf_counter() - request_started) * 1000)
                self.results.statuses[str(response.status_code)] += 1
                if response.status_code >= 500:
                    self.results.outcomes["5xx"] += 1
                    self.results.fail(f"request {index}: server error {response.status_code}")
                    continue
                try:
                    body = response.json()
                    if response.status_code == 201 and not is_replay:
                        self.results.outcomes["confirmed"] += 1
                        self.results.winners[seat].append(body["reservation_id"])
                        expected_user = f"{self.prefix}-user-{user}"
                        if (
                            body["user_id"] != expected_user
                            or body["show_id"] != show_id
                            or body["seats"] != [seat]
                            or body["amount_paise"] != 25_000
                            or type(body["amount_paise"]) is not int
                            or body["status"] != "confirmed"
                        ):
                            self.results.fail(f"request {index}: wrong reservation data")
                    elif response.status_code == 200 and is_replay:
                        self.results.outcomes["idempotent_replay"] += 1
                        if body != replay or response.headers.get("Idempotent-Replayed") != "true":
                            self.results.fail(
                                f"request {index}: replay changed the original result"
                            )
                    elif response.status_code == 409 and not is_replay:
                        reason = body["error"]["code"]
                        self.results.outcomes[reason] += 1
                        if reason != "seat_taken":
                            self.results.fail(f"request {index}: unexpected decline {reason}")
                    else:
                        self.results.outcomes["unexpected_response"] += 1
                        self.results.fail(
                            f"request {index}: unexpected HTTP {response.status_code}"
                        )
                except (ValueError, KeyError, TypeError) as error:
                    self.results.outcomes["invalid_response"] += 1
                    self.results.fail(f"request {index}: malformed response: {error}")

        sampler = asyncio.create_task(sample())
        started = time.perf_counter()
        try:
            await asyncio.gather(
                *(worker() for _ in range(min(self.args.concurrency, self.args.requests)))
            )
        finally:
            completed.set()
            await sampler
        elapsed = time.perf_counter() - started
        final = reconcile(await self.state(show_id))
        for seat in hot_seats:
            if len(self.results.winners[seat]) != 1:
                self.results.fail(
                    f"{seat}: expected exactly one 201, got {len(self.results.winners[seat])}"
                )
        if final["confirmed"] != len(hot_seats) + 1 or final["available"] != 1:
            self.results.fail(f"unexpected final storm state: {final}")
        await self.metrics_reconciliation(show_id)
        latencies = sorted(self.results.latencies)
        percentiles = (
            {
                f"p{percentile}": round(
                    latencies[min(len(latencies) - 1, int(len(latencies) * percentile / 100))], 2
                )
                for percentile in (50, 95, 99)
            }
            if latencies
            else {}
        )
        return {
            "show_id": show_id,
            "requests": self.args.requests,
            "concurrency": self.args.concurrency,
            "seconds": round(elapsed, 3),
            "requests_per_second": round(self.args.requests / elapsed, 2),
            "http_response_latency_ms": percentiles,
            "http_statuses": dict(self.results.statuses),
            "outcomes": dict(self.results.outcomes),
            "hot_seat_201_counts": {seat: len(self.results.winners[seat]) for seat in hot_seats},
            "invariant_snapshots_during_burst": self.results.snapshots,
            "final_reconciliation": final,
            "metrics_match_final_state": True,
        }

    async def scenarios(self) -> dict:
        """Check rules that a hot-seat storm by itself cannot demonstrate."""
        report = {}
        quota = await self.create_show("quota", [f"Q{i}" for i in range(10)])
        responses = await asyncio.gather(
            *(self.reserve(quota["id"], [f"Q{i}"], f"quota-{i}") for i in range(10))
        )
        statuses = Counter(response.status_code for response in responses)
        assert statuses == {201: 4, 409: 6}, f"quota outcomes: {dict(statuses)}"
        for response in responses:
            if response.status_code == 409:
                assert response.json()["error"]["code"] == "per_user_limit"
        report["per_user_limit"] = reconcile(await self.state(quota["id"]))
        assert report["per_user_limit"]["confirmed"] == 4

        idem = await self.create_show("key-conflict", ["I1", "I2"])
        races = await asyncio.gather(
            *(self.reserve(idem["id"], [seat], "same-key") for seat in ("I1", "I2"))
        )
        assert Counter(r.status_code for r in races) == {201: 1, 409: 1}
        assert (
            next(r for r in races if r.status_code == 409).json()["error"]["code"]
            == "idempotency_conflict"
        )
        winner = next(r.json() for r in races if r.status_code == 201)
        replay = checked_json(await self.reserve(idem["id"], winner["seats"], "same-key"), 200)
        assert replay == winner
        report["same_key_different_seats"] = "one reservation; changed request rejected"

        atomic = await self.create_show("atomic-cancel", ["A1", "A2", "A3"])
        original = checked_json(await self.reserve(atomic["id"], ["A1"], "first"), 201)
        declined = checked_json(await self.reserve(atomic["id"], ["A1", "A2"], "pair", 1), 409)
        assert declined["error"]["code"] == "seat_taken"
        assert reconcile(await self.state(atomic["id"]))["confirmed"] == 1
        checked_json(await self.reserve(atomic["id"], ["A2"], "free-half", 1), 201)
        reservation_id = original["reservation_id"]
        checked_json(
            await self.client.post(
                f"/reservations/{reservation_id}/cancel", headers=bearer(self.tokens[1])
            ),
            403,
        )
        cancelled = checked_json(
            await self.client.post(
                f"/reservations/{reservation_id}/cancel", headers=bearer(self.tokens[0])
            ),
            200,
        )
        assert cancelled["status"] == "cancelled"
        new_booking = checked_json(await self.reserve(atomic["id"], ["A1"], "new-owner", 1), 201)
        checked_json(
            await self.client.post(
                f"/reservations/{reservation_id}/cancel", headers=bearer(self.tokens[0])
            ),
            200,
        )
        old_replay = checked_json(await self.reserve(atomic["id"], ["A1"], "first"), 200)
        assert old_replay == original
        checked_json(await self.reserve(atomic["id"], ["A1"], "third-booking"), 409)
        final = reconcile(await self.state(atomic["id"]))
        assert final["confirmed"] == 2 and new_booking["reservation_id"] != reservation_id
        report["all_or_nothing_and_cancellation"] = final

        spoof = await self.reserve(
            atomic["id"], ["A3"], "spoof", 0, extra={"user_id": f"{self.prefix}-user-1"}
        )
        if spoof.status_code == 201:
            assert spoof.json()["user_id"] == f"{self.prefix}-user-0"
        else:
            checked_json(spoof, 422)
        report["token_identity"] = "spoofed identity rejected or ignored"
        report["metrics_after_cancel_and_rebook"] = await self.metrics_reconciliation(atomic["id"])
        return report


async def run(args: argparse.Namespace) -> int:
    # Control requests use fresh, small pools instead of inheriting thousands of
    # idle storm connections that the server may already have closed.
    async with (
        aiohttp.ClientSession(
            base_url=args.base_url.rstrip("/"),
            connector=aiohttp.TCPConnector(limit=32, keepalive_timeout=2),
            timeout=aiohttp.ClientTimeout(total=args.timeout),
        ) as control,
        aiohttp.ClientSession(
            base_url=args.base_url.rstrip("/"),
            connector=aiohttp.TCPConnector(limit=args.concurrency, keepalive_timeout=2),
            timeout=aiohttp.ClientTimeout(total=args.timeout),
        ) as load,
    ):
        exercise = Exercise(BurstClient(control), args, BurstClient(load))
        print(f"Preparing {args.users} authenticated users for {exercise.prefix}...", flush=True)
        await exercise.setup()
        print(
            f"Sending {args.requests} reservations with {args.concurrency} workers...", flush=True
        )
        report = await exercise.storm()
        try:
            report["additional_scenarios"] = await exercise.scenarios()
        except (
            AssertionError,
            aiohttp.ClientError,
            TimeoutError,
            KeyError,
            ValueError,
            TypeError,
        ) as error:
            exercise.results.fail(f"additional scenarios: {type(error).__name__}: {error}")
        report["passed"] = exercise.results.error_count == 0
        report["error_count"] = exercise.results.error_count
        report["errors_first_20"] = exercise.results.errors
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(arguments())))
    except (
        AssertionError,
        aiohttp.ClientError,
        TimeoutError,
        KeyError,
        ValueError,
        TypeError,
    ) as error:
        print(
            json.dumps({"passed": False, "error": f"{type(error).__name__}: {error}"}),
            file=sys.stderr,
        )
        sys.exit(1)
