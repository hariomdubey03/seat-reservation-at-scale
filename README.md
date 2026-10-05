# Seat Reservation at Scale

A JSON API built with Python 3.12, FastAPI, and PostgreSQL 17. PostgreSQL owns booking, quota, and idempotency decisions across all application processes and machines. Amounts are integers in paise.

The service implements immediate confirmation, owner-only cancellation, and all-or-nothing multi-seat reservations. A new booking returns `201`; replaying a successful idempotent request returns `200` with the original reservation and `Idempotent-Replayed: true`. Conflicting bookings return `409`.

## Live service and reviewer files

- **API:** https://seats.algocrafter.in
- **Interactive documentation:** https://seats.algocrafter.in/docs
- **Readiness:** https://seats.algocrafter.in/health/ready
- **Metrics:** https://seats.algocrafter.in/metrics
- **Logs:** `GET /logs` with the privately supplied admin bearer token.
- [Reviewer walkthrough](docs/REVIEWER_GUIDE.md), [flow diagrams](docs/ARCHITECTURE.md), and [operator runbook](docs/OPERATIONS.md).
- [Postman collection](postman/seat-reservation.postman_collection.json) and [live environment](postman/live.postman_environment.json). Set the empty `admin_token` value privately, then run in order.

The VM deployment uses `deploy/compose.vm.yaml`, a dedicated PostgreSQL volume,
resource limits, and the existing reverse proxy with a separate hostname. Live
credentials and scratch notes are excluded from the public submission.

## Verified locally — 5 October 2026

[evidence/validation.json](evidence/validation.json) records the tested source commit and results:

- 26 HTTP integration tests passed, including concurrent booking, quota, cancellation, and authentication cases.
- A fresh Git clone built with Docker and passed all 26 tests against its own PostgreSQL database.
- 20,000 requests with 20,000 concurrent client tasks completed in 86.492 seconds: 8 new reservations, 19,989 seat-taken responses, 3 successful replays, **zero 5xx and zero transport failures**. One additional reservation was created before the storm to exercise replay.
- Exactly one winner for each of 8 hot seats; 71 reconciliation samples; metrics matched final state.
- The high-load p95 client latency was 65.3 seconds; sampled peak API memory was 341.5 MiB. These are local measurements, not a hosted performance promise.
- Two API containers shared idempotency and quotas correctly. Database outage returned readiness `503` and liveness `200`; restart preserved bookings and replay results.
- The read-only PostgreSQL audit passed for ownership, quotas, seat counts, money, and completed idempotency records.

The client used 500 authenticated identities and a 240-second timeout. See the [final show state](evidence/show-state.json), [metrics snapshot](evidence/metrics-snapshot.prom), and [correlated log sample](evidence/request-logs.sample.jsonl). The complete 20,000-entry local request log is in ignored `artifacts/burst-request-logs.jsonl`. These are the original local results; hosted results are recorded separately in `evidence/hosted-validation.json`.

## Run from a clean checkout

Requirements: Docker Engine with Docker Compose v2. Python 3.12 is needed only for running the tests or burst client on the host.

```sh
docker compose up --build -d --wait
curl --fail http://127.0.0.1:8000/health/ready
```

The API is at `http://127.0.0.1:8000`, with interactive API documentation at `/docs`. Compose creates a persistent PostgreSQL volume, waits for the database, runs migrations, and starts the API as a non-root container user. `docker compose down` preserves bookings; deleting the named volume deletes them.

Compose defaults are **local development credentials** and bind both ports to localhost. Copy `.env.example` to `.env` to override them. Public deployments must use separate random values for `AUTH_SECRET` (at least 32 characters) and `ADMIN_TOKEN` (at least 24 characters). Generate each using `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`.

### Run the API directly

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
set -a
. ./.env
set +a
docker compose up -d --wait db
python -m app.migrate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`DATABASE_URL` targets PostgreSQL on `127.0.0.1:5433` for host development. Compose sets the API container's connection URL to the internal `db:5432` address. `DB_POOL_SIZE` defaults to 10 connections **per application process**; size the total across replicas below the database connection budget. Each API process admits at most 256 active mutation requests, queuing the rest before body parsing; reads and health checks bypass this admission queue. This queue controls memory use, while PostgreSQL still decides ownership. `DB_POOL_TIMEOUT` is the maximum pool wait in seconds (default 180). User tokens expire after `TOKEN_TTL_SECONDS` (default 86400). `LOG_LEVEL` defaults to `INFO`; `PORT` defaults to `8000`.

## API walkthrough

These examples use the local Compose credentials. Replace them for a hosted service. Keep the admin token private; it can create shows and issue user tokens.

```sh
BASE_URL=http://127.0.0.1:8000
ADMIN_TOKEN=local-admin-token-change-before-deployment

# Admin provisions a token for the evaluator's user.
TOKEN=$(curl --fail --silent "$BASE_URL/auth/token" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"alice"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

SHOW_ID=$(curl --fail --silent "$BASE_URL/shows" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"friday-night","seats":["A1","A2","A12","A13"],"price_paise":25000,"per_user_limit":4}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

curl -i "$BASE_URL/shows/$SHOW_ID/reserve" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: alice-first-booking' \
  -d '{"seats":["A12"]}'
```

The reservation body contains `reservation_id`, `show_id`, `user_id`, `seats`, `amount_paise`, and `status`. Save `reservation_id` to cancel:

```sh
RESERVATION_ID=replace-with-reservation-id
curl --fail --silent -X POST "$BASE_URL/reservations/$RESERVATION_ID/cancel" \
  -H "Authorization: Bearer $TOKEN"
curl --fail --silent "$BASE_URL/shows/$SHOW_ID"
```

The endpoints are:

- `POST /auth/token`: admin bearer token; body `{"user_id":"alice"}`; returns a signed user token. Token issuance is deliberately admin-only so an unauthenticated caller cannot impersonate another user.
- `POST /shows`: admin bearer token; name, unique seat names, nonnegative integer `price_paise`, optional positive `per_user_limit` (default 4).
- `GET /shows/{id}`: show details, per-seat states, and `counts` with `available`, `held`, `confirmed`, and `total_seats`.
- `POST /shows/{id}/reserve`: user bearer token; `seats` plus an idempotency key in the body or `Idempotency-Key` header. Conflicting header/body keys are rejected. Identity always comes from the verified token.
- `POST /reservations/{id}/cancel`: only the reservation owner can cancel; repeated cancellation succeeds without changing another booking.
- `GET /health/live`: process liveness.
- `GET /health/ready`: an actual database check; fails with `503` when the dependency is unavailable.
- `GET /metrics`: Prometheus text exposition.

### Booking rules

Requests are all-or-nothing: if any requested seat is unavailable, no seat in that request is booked. Duplicate seats in a request are invalid. Seat identifiers are case-sensitive, up to 32 characters, and use letters, digits, underscores, or hyphens with an alphanumeric first character. Seat order does not change the meaning of an idempotent request. Unknown body fields, including a spoofed `user_id`, are rejected.

Idempotency keys are scoped to the authenticated user and operation. Reusing a key for another show or another seat set returns `409`. The response is saved in the same transaction as the booking; retries after a lost response or an application restart return the saved result. After cancellation, a retry of the original booking request still replays that original result and does not book the seat again. Use a fresh key for a new booking attempt.

Domain declines after claiming a valid key are also saved, including seat-taken, quota, and unknown-seat outcomes. A retry returns the original decline even if conditions subsequently change. Validation and authentication failures before a claim do not consume a key.

Cancellation restores both the seats and the user's quota. This implementation has no temporary holds or automatic expiry, so `held` is always zero. Cancellation checks the reservation currently owning each seat before releasing it. There is no payment gateway: `amount_paise` is the stored reservation amount, calculated once from the show's price. External payment authorization and refunds are outside this API's contract.

Errors use `{"error":{"code":"...","message":"..."},"request_id":"..."}`. A normal seat conflict, quota decline, or idempotency mismatch is a domain response, not a server error. An unavailable database produces a truthful `503`; no booking is acknowledged from an uncommitted local cache.

## Concurrency checks and burst

Install the client/test dependencies inside a virtual environment, and start the service first:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
make test
make burst
```

`make test` uses `TEST_BASE_URL` through the Makefile's `BASE_URL` and admin credentials. To call pytest directly, set `TEST_BASE_URL` (or `BASE_URL`) and `ADMIN_TOKEN`.

Against a deployed service:

```sh
make burst BASE_URL=https://your-service.onrender.com \
  ADMIN_TOKEN="$ADMIN_TOKEN" REQUESTS=20000 CONCURRENCY=500
```

Equivalent without Make:

```sh
python scripts/burst.py https://your-service.onrender.com \
  --admin-token "$ADMIN_TOKEN" --requests 20000 --concurrency 500
```

The client creates fresh shows and users. Its main storm sends distinct keys against hot seats (80%) and retries a prebooked reservation (20%). It samples the show invariant during the storm and checks final reconciliation. Additional scenarios cover the concurrent per-user limit, conflicting key reuse, all-or-nothing booking, cancellation ownership, stale cancellation after rebooking, retries after cancellation, and spoofed identity.

After progress messages, the client prints a JSON summary with HTTP outcomes, declines by reason, any 5xx or transport failures, and each hot seat's `201` count. Unexpected behavior produces a nonzero exit code. `--users` defaults to 500, `--hot-seats` to 8, and `--timeout` to 60 seconds. `REQUESTS` is the request count for the main storm; `CONCURRENCY` bounds simultaneous client requests. A 20,000-request run at concurrency 500 is not evidence of 20,000 simultaneous connections. Provision and measure the deployment at the evaluator's actual load before claiming that capacity.

Record a reproducible run with the URL, Git commit, request count, concurrency, and output:

```sh
mkdir -p artifacts
make burst BASE_URL="$BASE_URL" ADMIN_TOKEN="$ADMIN_TOKEN" \
  REQUESTS=20000 CONCURRENCY=500 | tee artifacts/burst.txt
```

The CI workflow builds the Docker image and runs integration, burst, and operational checks.
For local multi-container and recovery checks, run:

```sh
make check-operations
make audit
```

`check-operations` starts a temporary second API container on localhost:8001, tests shared
idempotency and quotas, stops and starts this project's database, then restarts its API.
It preserves the database volume and removes its temporary replica. Run it only against
the local Compose stack, while no other load test is running. `audit` checks committed
seat ownership, quota, amounts, and idempotency consistency directly in PostgreSQL.

To attempt the assignment's full simultaneous load, increase concurrency explicitly:

```sh
python scripts/burst.py "$BASE_URL" --admin-token "$ADMIN_TOKEN" \
  --requests 20000 --concurrency 20000 --timeout 240
```

The burst client uses aiohttp connection pooling with a configured concurrency bound;
its connection-pool behavior follows the [aiohttp client documentation](https://docs.aiohttp.org/en/stable/client_advanced.html#limiting-connection-pool-size).
Both client and server resources affect this measurement. A large task count does not
mean all TCP connections arrive within the same millisecond.

## Metrics and logs

Scrape `$BASE_URL/metrics`. Business metrics read committed PostgreSQL records, so they survive application restarts and are consistent across application replicas:

- `reservations_confirmed_total`: historical successful reservations, including those subsequently cancelled. A replay never adds another confirmation.
- `reservations_cancelled_total`: historical cancellations, counted once per reservation.
- `reservations_replayed_total`: all replay requests, including replays of saved declines.
- `reservations_declined_total{reason="..."}`: request outcomes including seat conflicts, quota declines, and idempotency conflicts. The required `idempotent_replay` reason records a duplicate attempt; successful replay responses still use `200`.
- `seats_available{show_id="..."}`, `seats_held{show_id="..."}`, and `seats_confirmed{show_id="..."}`: current seat counts from a consistent snapshot.

The confirmation counter counts **reservations**, while the gauges count **seats**; a two-seat reservation adds one confirmation and two confirmed seats. For each show, `available + held + confirmed == total_seats`. Each API response and metrics scrape has its own snapshot; two separate reads during active booking need not have identical counts.

Structured JSON logs are written to stdout with a correlation/request ID. Inspect the response's `X-Request-ID`, and use it to find the corresponding log. Locally:

```sh
docker compose logs --follow api
docker compose logs --no-color api > artifacts/live-logs.txt
```

For the hosted service, use its provider log viewer. If those logs cannot be public, record the log viewer while the burst runs and include the recording with the submission. Do not publish bearer tokens or environment secrets.

## Alternative deployment: Render

`render.yaml` is a Render Blueprint for a Docker web service and managed PostgreSQL in the same region. Both plans are explicitly free. The Docker command runs migrations before starting the server, which also supports cold starts; a failed migration prevents an unhealthy instance from accepting traffic. A paid deployment can move migration execution to a release/pre-deploy step. Render's [Blueprint reference](https://render.com/docs/blueprint-spec) documents the configuration; its [deployment documentation](https://render.com/docs/deploys#pre-deploy-command) describes the paid pre-deploy option.

1. Push this directory as the root of a public Git repository, preserving its incremental commits.
2. In Render, create a Blueprint from that repository and supply a newly generated `ADMIN_TOKEN`. `AUTH_SECRET` is generated by the platform and the database URL is wired automatically.
3. Wait for `/health/ready` to return `200`. The API and database must use the same region; the database is configured for internal network access.
4. Run the tests and burst against the assigned public URL, inspect `/metrics`, restart the web service, and confirm that previous show/reservation state remains.
5. Add the actual repository URL, live URL, measured burst output, and log access or recording to the submission.

Free-tier capacity is not a 20,000-client performance guarantee. Free Render databases also expire after 30 days; select a suitable plan or arrange a deployment lifetime that covers evaluation. See [Render's current free-tier limits](https://render.com/docs/free).

The live submission uses the VM deployment described in [the operator runbook](docs/OPERATIONS.md). The Render blueprint remains an optional alternative.

See [WRITEUP.md](WRITEUP.md) for the transaction and failure model, design principles, and operational alerts.
