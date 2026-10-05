"""Integration tests against a running HTTP service and its real PostgreSQL DB.

Run with ADMIN_TOKEN=... TEST_BASE_URL=http://localhost:8000 pytest.
Each test creates uniquely named shows and users; existing records are untouched.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections import Counter

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def body(response: httpx.Response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def assert_reconciled(show: dict) -> dict:
    counts = show["counts"]
    seats = show["seats"]
    total = show["total_seats"]
    actual = Counter(seat["status"] for seat in seats)
    assert set(actual) <= {"available", "held", "confirmed"}
    assert len(seats) == len({seat["seat_number"] for seat in seats}) == total
    for status in ("available", "held", "confirmed"):
        assert type(counts[status]) is int and counts[status] >= 0
        assert counts[status] == actual[status]
    assert sum(counts[status] for status in ("available", "held", "confirmed")) == total
    assert counts.get("total_seats", total) == total
    return counts


class API:
    def __init__(self, client: httpx.AsyncClient, admin_token: str):
        self.client = client
        self.admin = auth(admin_token)
        self.prefix = f"test-{uuid.uuid4().hex}"

    async def user(self, label: str) -> tuple[str, str]:
        user_id = f"{self.prefix}-{label}"
        token = body(
            await self.client.post("/auth/token", headers=self.admin, json={"user_id": user_id})
        )
        assert token["token_type"] == "bearer"
        return user_id, token["access_token"]

    async def show(self, seats: list[str], limit: int | None = None) -> dict:
        payload = {"name": self.prefix, "seats": seats, "price_paise": 25_001}
        if limit is not None:
            payload["per_user_limit"] = limit
        result = body(await self.client.post("/shows", headers=self.admin, json=payload), 201)
        assert result["price_paise"] == 25_001
        assert result["per_user_limit"] == (4 if limit is None else limit)
        assert assert_reconciled(result)["available"] == len(seats)
        return result

    async def reserve(
        self,
        show: dict,
        token: str,
        seats: list[str],
        key: str | None = None,
        extra: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        payload = {"seats": seats, "idempotency_key": key or uuid.uuid4().hex}
        payload.update(extra or {})
        return await self.client.post(
            f"/shows/{show['id']}/reserve", headers=auth(token) | (headers or {}), json=payload
        )

    async def state(self, show: dict) -> dict:
        result = body(await self.client.get(f"/shows/{show['id']}"))
        assert_reconciled(result)
        return result

    async def cancel(self, reservation: dict, token: str) -> httpx.Response:
        return await self.client.post(
            f"/reservations/{reservation['reservation_id']}/cancel", headers=auth(token)
        )


@pytest_asyncio.fixture
async def api():
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        pytest.fail("Set ADMIN_TOKEN and start the service before running HTTP integration tests")
    base_url = os.environ.get("TEST_BASE_URL", os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    limits = httpx.Limits(max_connections=128, max_keepalive_connections=128)
    async with httpx.AsyncClient(base_url=base_url, timeout=60, limits=limits) as client:
        yield API(client, admin_token)


async def test_reviewer_logs_require_admin_and_correlate_requests(api):
    body(await api.client.get("/logs"), 401)
    _, user_token = await api.user("log-reader")
    body(await api.client.get("/logs", headers=auth(user_token)), 403)
    request_id = f"reviewer-{uuid.uuid4().hex}"
    response = await api.client.get("/health/live", headers={"X-Request-ID": request_id})
    assert response.status_code == 200
    logs = await api.client.get(
        "/logs", headers=api.admin, params={"request_id": request_id, "limit": 10}
    )
    entries = body(logs)["entries"]
    assert len(entries) == 1
    assert entries[0]["request_id"] == request_id
    assert entries[0]["status"] == 200
    assert entries[0]["path"] == "/health/live"
    assert logs.headers["Cache-Control"] == "no-store"
    assert "Authorization" not in logs.text and user_token not in logs.text
    body(await api.client.get("/logs?limit=501", headers=api.admin), 422)


async def test_health_and_prometheus_metrics_are_public(api):
    body(await api.client.get("/health/live"))
    body(await api.client.get("/health/ready"))
    response = await api.client.get("/metrics")
    assert response.status_code == 200
    assert "# TYPE" in response.text
    assert "reservations_confirmed_total" in response.text
    assert "reservations_declined_total" in response.text


async def test_admin_routes_and_booking_require_authentication(api):
    _, token = await api.user("ordinary")
    payload = {"name": api.prefix, "seats": ["A1"], "price_paise": 100}
    for headers in ({}, auth("invalid"), auth(token)):
        response = await api.client.post("/shows", headers=headers, json=payload)
        assert response.status_code in {401, 403}
        response = await api.client.post(
            "/auth/token", headers=headers, json={"user_id": "impersonated"}
        )
        assert response.status_code in {401, 403}
    show = await api.show(["A1"])
    response = await api.client.post(
        f"/shows/{show['id']}/reserve", json={"seats": ["A1"], "idempotency_key": "no-auth"}
    )
    assert response.status_code in {401, 403}
    assert (await api.state(show))["counts"]["confirmed"] == 0


async def test_hot_seat_has_exactly_one_winner(api):
    show = await api.show(["A12"])
    users = await asyncio.gather(*(api.user(str(index)) for index in range(32)))
    responses = await asyncio.gather(
        *(
            api.reserve(show, users[index % len(users)][1], ["A12"], f"request-{index}")
            for index in range(128)
        )
    )
    assert Counter(response.status_code for response in responses) == {201: 1, 409: 127}
    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"]["code"] == "seat_taken"
    winner = next(response.json() for response in responses if response.status_code == 201)
    assert winner["seats"] == ["A12"] and winner["amount_paise"] == 25_001
    assert type(winner["amount_paise"]) is int
    assert (await api.state(show))["counts"]["confirmed"] == 1


async def test_parallel_identical_keys_return_one_original_reservation(api):
    user_id, token = await api.user("retry")
    show = await api.show(["A1", "A2"])
    responses = await asyncio.gather(
        *(api.reserve(show, token, ["A1"], "one-key") for _ in range(40))
    )
    assert Counter(response.status_code for response in responses) == {201: 1, 200: 39}
    original = next(response.json() for response in responses if response.status_code == 201)
    assert original["user_id"] == user_id
    for response in responses:
        assert response.json() == original
        if response.status_code == 200:
            assert response.headers["Idempotent-Replayed"] == "true"
    assert (await api.state(show))["counts"]["confirmed"] == 1


async def test_parallel_same_key_different_seats_rejects_changed_request(api):
    _, token = await api.user("same-key")
    show = await api.show(["A1", "A2"])
    responses = await asyncio.gather(
        api.reserve(show, token, ["A1"], "shared"),
        api.reserve(show, token, ["A2"], "shared"),
    )
    assert Counter(response.status_code for response in responses) == {201: 1, 409: 1}
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert (await api.state(show))["counts"]["confirmed"] == 1


async def test_idempotency_key_is_scoped_to_user(api):
    (_, token_a), (_, token_b) = await asyncio.gather(api.user("a"), api.user("b"))
    show = await api.show(["A1", "A2"])
    responses = await asyncio.gather(
        api.reserve(show, token_a, ["A1"], "same-text"),
        api.reserve(show, token_b, ["A2"], "same-text"),
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["reservation_id"] != responses[1].json()["reservation_id"]


async def test_per_user_limit_survives_parallel_different_seats(api):
    _, token = await api.user("quota")
    show = await api.show([f"A{i}" for i in range(10)])
    responses = await asyncio.gather(*(api.reserve(show, token, [f"A{i}"]) for i in range(10)))
    assert Counter(response.status_code for response in responses) == {201: 4, 409: 6}
    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"]["code"] == "per_user_limit"
    assert (await api.state(show))["counts"]["confirmed"] == 4


async def test_partial_request_is_all_or_nothing_and_decline_replays(api):
    (_, token_a), (_, token_b) = await asyncio.gather(api.user("a"), api.user("b"))
    show = await api.show(["A1", "A2"])
    first = body(await api.reserve(show, token_a, ["A1"]), 201)
    declined_response = await api.reserve(show, token_b, ["A1", "A2"], "declined-pair")
    declined = body(declined_response, 409)
    assert declined["error"]["code"] == "seat_taken"
    state = await api.state(show)
    assert {seat["seat_number"]: seat["status"] for seat in state["seats"]} == {
        "A1": "confirmed",
        "A2": "available",
    }
    body(await api.cancel(first, token_a))
    replay_response = await api.reserve(show, token_b, ["A1", "A2"], "declined-pair")
    replay = body(replay_response, 409)
    assert replay["error"] == declined["error"]
    assert replay_response.headers["Idempotent-Replayed"] == "true"
    assert (await api.state(show))["counts"]["confirmed"] == 0
    booking = body(await api.reserve(show, token_b, ["A1", "A2"], "fresh-pair"), 201)
    assert booking["amount_paise"] == 50_002


async def test_overlapping_multi_seat_requests_commit_only_whole_reservation(api):
    users = await asyncio.gather(api.user("a"), api.user("b"))
    show = await api.show(["A1", "A2", "A3"])
    responses = await asyncio.gather(
        api.reserve(show, users[0][1], ["A1", "A2"]),
        api.reserve(show, users[1][1], ["A2", "A3"]),
    )
    assert Counter(response.status_code for response in responses) == {201: 1, 409: 1}
    winner = next(response.json() for response in responses if response.status_code == 201)
    state = await api.state(show)
    confirmed = {seat["seat_number"] for seat in state["seats"] if seat["status"] == "confirmed"}
    assert confirmed == set(winner["seats"])
    assert state["counts"]["confirmed"] == 2


async def test_owner_cancel_rebook_and_stale_cancel_cannot_release_new_booking(api):
    (_, token_a), (_, token_b) = await asyncio.gather(api.user("a"), api.user("b"))
    show = await api.show(["A1", "A2"], limit=1)
    first = body(await api.reserve(show, token_a, ["A1"], "original"), 201)
    body(await api.cancel(first, token_b), 403)
    assert (await api.state(show))["counts"]["confirmed"] == 1
    assert body(await api.cancel(first, token_a))["status"] == "cancelled"
    assert (await api.state(show))["counts"]["available"] == 2
    second = body(await api.reserve(show, token_b, ["A1"]), 201)
    body(await api.cancel(first, token_a))
    assert body(await api.reserve(show, token_a, ["A1"], "original")) == first
    body(await api.reserve(show, token_a, ["A1"]), 409)
    # Cancellation returns quota to the previous owner, independently of the new owner.
    body(await api.reserve(show, token_a, ["A2"]), 201)
    assert (await api.state(show))["counts"]["confirmed"] == 2
    assert first["reservation_id"] != second["reservation_id"]


async def test_identity_is_derived_from_token(api):
    (user_a, token_a), (user_b, token_b) = await asyncio.gather(api.user("a"), api.user("b"))
    show = await api.show(["A1"])
    response = await api.reserve(show, token_a, ["A1"], extra={"user_id": user_b})
    if response.status_code == 422:
        assert (await api.state(show))["counts"]["confirmed"] == 0
        response = await api.reserve(show, token_a, ["A1"])
    reservation = body(response, 201)
    assert reservation["user_id"] == user_a
    body(await api.cancel(reservation, token_b), 403)


async def test_header_idempotency_key_replays_and_correlation_id_is_returned(api):
    _, token = await api.user("header")
    show = await api.show(["A1"])
    headers = auth(token) | {"Idempotency-Key": "header-key", "X-Request-ID": api.prefix}
    first = await api.client.post(
        f"/shows/{show['id']}/reserve", headers=headers, json={"seats": ["A1"]}
    )
    reservation = body(first, 201)
    assert first.headers["X-Request-ID"] == api.prefix
    second = await api.client.post(
        f"/shows/{show['id']}/reserve", headers=headers, json={"seats": ["A1"]}
    )
    assert body(second) == reservation
    assert second.headers["Idempotent-Replayed"] == "true"


async def test_metrics_seat_gauges_match_api_after_booking_and_cancellation(api):
    _, token = await api.user("metrics")
    show = await api.show(["A1", "A2", "A3"])
    reservation = body(await api.reserve(show, token, ["A1", "A2"]), 201)

    async def assert_gauges():
        state = await api.state(show)
        metrics = await api.client.get("/metrics")
        assert metrics.status_code == 200
        for status in ("available", "held", "confirmed"):
            pattern = (
                rf'^seats_{status}\{{[^}}]*show_id="{re.escape(show["id"])}"'
                rf"[^}}]*\}}\s+([0-9.eE+-]+)"
            )
            value = re.search(pattern, metrics.text, re.MULTILINE)
            assert value is not None, f"missing seats_{status} gauge for {show['id']}"
            assert float(value.group(1)) == state["counts"][status]

    await assert_gauges()
    body(await api.cancel(reservation, token))
    await assert_gauges()


async def test_state_reconciles_while_reservations_are_in_flight(api):
    _, token = await api.user("snapshots")
    show = await api.show([f"A{i}" for i in range(40)], limit=40)
    done = asyncio.Event()
    snapshots = []

    async def observe():
        while not done.is_set():
            snapshots.append(await api.state(show))
            await asyncio.sleep(0.01)

    observer = asyncio.create_task(observe())
    try:
        responses = await asyncio.gather(*(api.reserve(show, token, [f"A{i}"]) for i in range(40)))
    finally:
        done.set()
        await observer
    assert all(response.status_code == 201 for response in responses)
    assert snapshots
    assert (await api.state(show))["counts"]["confirmed"] == 40


@pytest.mark.parametrize("seats", [[], ["A1", "A1"], ["UNKNOWN"]])
async def test_invalid_seat_request_never_changes_state(api, seats):
    _, token = await api.user("invalid")
    show = await api.show(["A1"])
    response = await api.reserve(show, token, seats)
    assert response.status_code in {400, 404, 422}
    assert (await api.state(show))["counts"]["available"] == 1


@pytest.mark.parametrize("price", [25_000.5, -1, "25000", True])
async def test_money_must_be_nonnegative_integer_paise(api, price):
    response = await api.client.post(
        "/shows",
        headers=api.admin,
        json={"name": api.prefix, "seats": ["A1"], "price_paise": price},
    )
    assert response.status_code == 422


async def test_same_key_across_shows_is_rejected(api):
    _, token = await api.user("cross-show")
    first, second = await asyncio.gather(api.show(["A1"]), api.show(["A1"]))
    original = body(await api.reserve(first, token, ["A1"], "shared"), 201)
    changed = body(await api.reserve(second, token, ["A1"], "shared"), 409)
    assert changed["error"]["code"] == "idempotency_conflict"
    assert body(await api.reserve(first, token, ["A1"], "shared")) == original
    assert (await api.state(second))["counts"]["available"] == 1


async def test_reverse_seat_order_replays_same_reservation(api):
    _, token = await api.user("seat-order")
    show = await api.show(["A1", "A2"])
    replies = await asyncio.gather(
        api.reserve(show, token, ["A1", "A2"], "shared"),
        api.reserve(show, token, ["A2", "A1"], "shared"),
    )
    assert Counter(r.status_code for r in replies) == {201: 1, 200: 1}
    assert replies[0].json() == replies[1].json()


async def test_parallel_cancel_and_reserve_preserves_quota(api):
    _, token = await api.user("cancel-race")
    show = await api.show([f"A{i}" for i in range(12)])
    original = body(await api.reserve(show, token, ["A0", "A1", "A2", "A3"]), 201)
    replies = await asyncio.gather(
        *(api.cancel(original, token) for _ in range(20)),
        *(api.reserve(show, token, [f"A{i}"]) for i in range(4, 12)),
    )
    assert all(r.status_code == 200 for r in replies[:20])
    assert all(r.status_code in {201, 409} for r in replies[20:])
    successful = [r for r in replies[20:] if r.status_code == 201]
    counts = (await api.state(show))["counts"]
    assert counts["confirmed"] == len(successful) <= 4
    # Fill the remaining quota after the race; repeated cancellations must not refund it twice.
    for i in range(12):
        response = await api.reserve(show, token, [f"A{i}"])
        assert response.status_code in {201, 409}
    assert (await api.state(show))["counts"]["confirmed"] == 4


async def test_tampered_user_token_cannot_impersonate(api):
    import base64
    import json

    _, token = await api.user("signed")
    parts = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    claims["sub"] = "someone-else"
    parts[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    show = await api.show(["A1"])
    response = await api.reserve(show, ".".join(parts), ["A1"])
    body(response, 401)
    assert (await api.state(show))["counts"]["available"] == 1


async def test_metrics_count_reservations_once_and_report_declines(api):
    from prometheus_client.parser import text_string_to_metric_families

    async def snapshot():
        response = await api.client.get("/metrics")
        assert response.status_code == 200
        return {
            (sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for family in text_string_to_metric_families(response.text)
            for sample in family.samples
        }

    (_, token_a), (_, token_b) = await asyncio.gather(api.user("a"), api.user("b"))
    show = await api.show(["A1", "A2", "A3"], limit=2)
    before = await snapshot()
    reservation = body(await api.reserve(show, token_a, ["A1", "A2"], "original"), 201)
    body(await api.reserve(show, token_a, ["A1", "A2"], "original"))
    body(await api.reserve(show, token_a, ["A3"], "over-limit"), 409)
    body(await api.reserve(show, token_b, ["A1"], "taken"), 409)
    body(await api.reserve(show, token_a, ["A3"], "original"), 409)
    body(await api.cancel(reservation, token_a))
    body(await api.cancel(reservation, token_a))
    after = await snapshot()
    for name in (
        "reservations_confirmed_total",
        "reservations_cancelled_total",
        "reservations_replayed_total",
    ):
        assert after[(name, ())] - before[(name, ())] == 1
    for reason in ("seat_taken", "per_user_limit", "idempotent_replay", "idempotency_conflict"):
        key = ("reservations_declined_total", (("reason", reason),))
        assert after[key] - before[key] == 1
