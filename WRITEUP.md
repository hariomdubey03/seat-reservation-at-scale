# Design and operational notes

## Atomic decision

PostgreSQL is the sole authority for seats, reservation records, each user's show quota, and idempotency results. The API holds no correctness-critical Python locks or local booking state, so the same decisions apply across threads, processes, and VMs.

One reservation transaction claims its idempotency key, locks the user's quota row for that show, and locks every requested seat row in sorted seat order with `SELECT ... FOR UPDATE`. It validates the quota and availability while those locks are held, creates the reservation, updates the seats and quota, and saves the response before commit. Another transaction touching any of those rows must wait for the first transaction's outcome. Only committed success is returned to the caller.

The seat table's primary key is `(show_id, seat_number)`: there is one current state and owner for each show seat. All-or-nothing means a conflict on any requested seat rejects the whole selection. Sorting seat locks prevents requests for `[A1,A2]` and `[A2,A1]` from creating a circular wait. Acquiring the show/user quota lock before seat locks also makes concurrent requests for different seats by one user respect the same limit.

SQL constraints and transaction boundaries enforce integrity; HTTP handlers do not perform an unprotected availability read followed by a separate write. Default PostgreSQL durability records the transaction before acknowledging success. Database connection loss is not interpreted as “seat taken.”

## Idempotency

The unique identity is `(user_id, operation, idempotency_key)`. `INSERT ... ON CONFLICT DO NOTHING` claims the key; a competing insert with the same identity waits for the earlier transaction to commit or roll back. The winner saves the canonical request (show ID and sorted seat set) and original result in the booking transaction. After the insert finishes, a duplicate request reads that result and compares the canonical request. A different show or seat set returns `409`.

A new successful booking returns `201`, while a successful replay returns the stored reservation with `200` and `Idempotent-Replayed: true`. This makes the hot-seat “exactly one 201” requirement compatible with retries. A process crash before commit leaves no committed reservation/key result; a crash after commit but before the HTTP reply is resolved by retrying the same key. Cancellation does not delete that key, so an old retry cannot resurrect the booking.

Domain declines after a key claim are committed as saved outcomes too. Retrying such a key returns its original decline; a later attempt after availability changes requires a new key. Validation and authentication failures before the claim do not consume keys.

The guarantee is one committed reservation for an accepted key, not exactly-once network delivery. The service does not call a payment provider. All money is calculated and stored in integer paise; any future external charge needs its own provider idempotency key and a durable workflow.

## Cancellation and consistency

Reservations confirm immediately. The chosen release model is explicit owner-only cancellation, not expiring holds. `held` therefore remains zero. Cancellation locks the user's quota, then the reservation, then its seat rows in deterministic order. It releases only seats currently owned by that reservation, changes the reservation status, and restores quota in one transaction. Repeating cancellation changes nothing further, including after another user rebooks those seats.

Show state and counts come from one consistent snapshot, so available, held, and confirmed always sum to the show's total within that response. Metrics use committed database state too. Application restarts preserve booking history in PostgreSQL.

During a partition or database outage, the service fails closed with an unavailable response. It prioritizes consistent ownership over accepting bookings without the authority. This is distinct from an ordinary domain decline, which stays `409`. No architecture can truthfully guarantee both continued booking and strict single ownership when it loses contact with its sole authority. Zero 5xx during the evaluator's normal burst remains a performance target to verify with measured load, connection limits, and sufficient hosting capacity.

## Structure and principles

The project uses an HTTP boundary for parsing, authentication, and response mapping; a service boundary for booking rules; and a PostgreSQL repository for SQL and transaction ownership. Dependencies are supplied explicitly. A small repository interface keeps the service independent of connection management without abstracting away the database guarantees it relies on.

Single responsibility means authentication, transport validation, booking rules, and persistence have clear owners. DRY means shared validation, error formatting, and idempotency comparison have one implementation. Composition and narrow interfaces provide the useful parts of OOP and dependency inversion. There is no class hierarchy for screens, air conditioning, or payment integrations absent from the exercise. New abstractions should solve a concrete second use case. The project skill captures these checks before implementation and review.

## Observability and operations

Business counters are derived from durable records rather than resettable per-worker memory. Confirmations and cancellations count reservation events; seat gauges measure the current inventory. Replay attempts are visible separately and do not increment confirmations. An `idempotent_replay` reason in the required declined metric describes a duplicate request, even when the saved reservation is returned successfully.

At 2am I would investigate:

- Any reconciliation failure, duplicate active ownership, or user quota mismatch immediately.
- Readiness failures, sustained HTTP 5xx, pool exhaustion, growing lock waits, and increasing reservation latency.
- A rise in key/payload conflicts or authentication failures, which can indicate a broken caller or misuse.
- Database storage exhaustion, missed backups, and approaching provider limits or expiration.

Expected seat-taken responses during a hot-seat storm are healthy contention outcomes. Alert thresholds for latency and error rates need a measured baseline. This submission exposes metrics and structured logs; a hosted Prometheus/Grafana installation and paging integration are follow-up operational work. Capturing output from the real public burst and its matching logs is part of deployment evidence.

The app uses a bounded asynchronous connection pool instead of one database connection per HTTP request. Keep transactions short and avoid network calls while holding row locks. Increasing API replicas also increases total pool connections, so database capacity must be budgeted across the fleet. Heavy contention for a single seat necessarily serializes the decision about that seat.

## Next work

First, publish and record the live deployment and observed results for the requested load. Then tune from measurements: connection budgeting, indexed query plans, lock wait duration, client admission control, and database sizing. Introduce a real identity provider, backups and restore drills, CI deployment checks, a versioned migration policy, and retention for idempotency and request outcomes. Scale metrics collection before request-event history makes full-history aggregation expensive. An external payment workflow would need an outbox and provider idempotency; expiring holds would need explicit ownership/version checks. Neither is required by the chosen cancellation model.
