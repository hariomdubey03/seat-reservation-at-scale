# Reviewer guide

## Start here

Base URL: **https://seats.algocrafter.in**

- Interactive API: https://seats.algocrafter.in/docs
- OpenAPI schema: https://seats.algocrafter.in/openapi.json
- Liveness: https://seats.algocrafter.in/health/live
- Database readiness: https://seats.algocrafter.in/health/ready
- Prometheus metrics: https://seats.algocrafter.in/metrics
- Recent request logs: `GET /logs`, using the separately supplied admin bearer token.

The admin token is supplied privately with the submission. It is deliberately absent
from Git, Postman exports, screenshots, and this document. Admin access creates shows
and provisions test-user tokens. Reservation identity always comes from the user JWT.

## Postman walkthrough

Import both files from `postman/`:

1. `seat-reservation.postman_collection.json`
2. `live.postman_environment.json`

Select the **Seat Reservation — Live** environment and set `admin_token` to the
privately supplied value. Run the collection in order, using Collection Runner or
individual requests. Creating a show generates a unique run identifier and saves
`show_id`; token requests save Alice's and Bob's credentials; booking saves the
reservation ID. No manual ID copying is required.

The walkthrough checks readiness, creation, integer pricing, a successful booking,
an identical retry, a changed-body retry, seat conflicts, all-or-nothing requests,
the per-user limit, spoofed identity, cancellation permissions, release/rebooking,
stale cancellation, final reconciliation, metrics, and request-ID log lookup.

Expected `409`, `403`, and `422` responses are successful negative test cases.
Run from the create-show step again for fresh state. The final demo keeps Bob's
booking on A1, so cancellation/rebooking is inspectable. It does not delete data.
To run locally, set `base_url` to `http://127.0.0.1:8000` and use the local admin token.

## Booking rules

- A show seat has exactly one current state and reservation owner.
- Reservations confirm immediately. There is no temporary hold or expiry timer;
  `held` is always zero in this release.
- Multiple seats are all-or-nothing. One unavailable seat declines the entire request.
- The default limit is four active seats per user per show. Cancelling restores quota.
- New confirmation: `201`. Successful replay: `200` with `Idempotent-Replayed: true`.
- The same key is scoped to the token's user and operation. Reusing it with another
  show or seat set returns `409`. Seat ordering does not change the request identity.
- A saved decline also replays. Use a new key to make a new attempt after cancellation.
- Replaying a successful request after cancellation returns the original response,
  without booking again. Read the show for its current inventory.
- Only the owner can cancel. Repeating cancellation cannot release someone else's rebooking.
- Prices and computed amounts are integer paise. This exercise records amounts;
  it does not integrate an external payment provider.

## Run the concurrency scenario

For the public HTTPS endpoint, use the HTTP/2 runner (Go 1.24 or newer, standard
library only). It submits 20,000 concurrent client tasks while multiplexing HTTP
streams over TLS connections, and prints the actual protocol and connections used:

```sh
export BASE_URL=https://seats.algocrafter.in
read -rs ADMIN_TOKEN
export ADMIN_TOKEN
go run scripts/burst_http2.go -url "$BASE_URL" -requests 20000 -concurrency 20000
```

It checks hot seats, identical retries, changed-body retries, parallel quota, ownership,
all-or-nothing selection, cancellation/rebooking, spoofing, reconciliation, and metrics.
The Python runner is also available for HTTP/1.1 comparisons and local testing:

Install Python 3.12 and `requirements-dev.txt`, then provide the admin token in the
environment (avoid committing it or putting it into a shell history entry):

```sh
export BASE_URL=https://seats.algocrafter.in
read -rs ADMIN_TOKEN
export ADMIN_TOKEN
python scripts/burst.py "$BASE_URL" --requests 20000 --concurrency 500 --timeout 240
```

The Python runner supports `--concurrency 20000`, but opening 20,000 independent
TLS connections requires a large load-generator memory budget. Use the HTTP/2
command above for the public 20,000-task scenario. The script creates
fresh shows and test identities, storms a hot seat, mixes further requests and
replays, samples the reconciliation invariant, checks metrics, and prints all HTTP
outcomes plus latency percentiles. It does not retry away network failures or 5xx.
Concurrency denotes client tasks, not an assurance that all connections arrive at
the same instant. Use `evidence/` for measured results and their environment/limits.

## Observe a request

Send `X-Request-ID: reviewer-example-1` with a request. The same value is returned
in the response headers. Then query:

```sh
curl -fsS "$BASE_URL/logs?request_id=reviewer-example-1&limit=10" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

For live polling, request `/logs?limit=100` every few seconds. Entries include UTC
timestamp, request ID, method, path, HTTP status, outcome, duration, and queue wait.
Request bodies, authorization headers, and signing secrets are never recorded.
This endpoint exposes the newest 2,000 completion records from the current API
process (up to 500 per response); it clears on restart and is not an audit archive.
Rotated JSON stdout logs are retained by Docker. See the operator runbook for access.

Metrics are derived from committed database records and survive restarts. Confirmation
and cancellation counters count reservations; seat gauges count seats. An idempotent
replay never adds a confirmation. `idempotent_replay` is included among the assignment's
decline reasons even when its HTTP response is a successful replay. Show state and a
metrics scrape use separate snapshots and may differ if bookings occur between reads.

## Implementation map

- `app/main.py`: HTTP routes, authorization dependencies, errors, health and observability.
- `app/domain.py`: booking commands and repository contract.
- `app/repository.py`: PostgreSQL transaction boundaries and lock ordering.
- `app/schema.sql`: relational constraints, seat ownership, quota, saved outcomes.
- `app/auth.py`: admin validation and signed user tokens.
- `app/middleware.py`: bounded admission and correlated completion logs.
- `scripts/burst.py`: live concurrency scenario; `tests/test_api.py`: HTTP integration checks.
- `WRITEUP.md`: concurrency and failure reasoning; `docs/ARCHITECTURE.md`: flow diagrams.
- `docs/OPERATIONS.md`: deployment, restart, backup, rollback and isolation.

## Submission checklist

The exercise requests a public Git repository with incremental history, the live URL,
burst instructions, metrics/log access, and `WRITEUP.md`. Share only the application,
dependencies, deployment files, tests, Postman files, diagrams/docs and sanitized evidence.
Local scratch notes, editor skills, credentials, virtual environments and raw private
artifacts are excluded from the submission export.
