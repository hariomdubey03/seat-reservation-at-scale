-- Read-only audit of committed state. Any invariant violation fails psql.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
DO $$
BEGIN
    IF EXISTS (
        SELECT s.id FROM shows s LEFT JOIN seats t ON t.show_id = s.id
        GROUP BY s.id HAVING count(t.seat_number) <> s.total_seats
    ) THEN RAISE EXCEPTION 'show seat count mismatch'; END IF;

    IF EXISTS (
        SELECT r.show_id, wanted.seat_number
        FROM reservations r CROSS JOIN LATERAL unnest(r.seats) wanted(seat_number)
        WHERE r.status = 'confirmed'
        GROUP BY r.show_id, wanted.seat_number HAVING count(*) > 1
    ) THEN RAISE EXCEPTION 'duplicate active reservation for a seat'; END IF;

    IF EXISTS (
        SELECT 1 FROM reservations r
        CROSS JOIN LATERAL unnest(r.seats) wanted(seat_number)
        LEFT JOIN seats s ON s.show_id = r.show_id AND s.seat_number = wanted.seat_number
        WHERE r.status = 'confirmed'
          AND (s.reservation_id IS DISTINCT FROM r.id OR s.status <> 'confirmed')
    ) THEN RAISE EXCEPTION 'confirmed reservation missing its seats'; END IF;

    IF EXISTS (
        SELECT 1 FROM seats s JOIN reservations r ON r.id = s.reservation_id
        WHERE s.status = 'confirmed'
          AND (r.status <> 'confirmed' OR NOT (s.seat_number = ANY(r.seats)))
    ) THEN RAISE EXCEPTION 'seat points to invalid reservation owner'; END IF;

    IF EXISTS (
        WITH actual AS (
            SELECT r.show_id, r.user_id, sum(cardinality(r.seats)) AS active_seats
            FROM reservations r WHERE r.status = 'confirmed' GROUP BY r.show_id, r.user_id
        )
        SELECT 1 FROM actual a FULL JOIN user_show_quota q
        ON a.show_id = q.show_id AND a.user_id = q.user_id
        WHERE coalesce(a.active_seats, 0) <> coalesce(q.active_seats, 0)
    ) THEN RAISE EXCEPTION 'quota does not match reservations'; END IF;

    IF EXISTS (
        SELECT 1 FROM user_show_quota q JOIN shows s ON s.id = q.show_id
        WHERE q.active_seats > s.per_user_limit OR q.active_seats < 0
    ) THEN RAISE EXCEPTION 'user quota exceeded'; END IF;

    IF EXISTS (
        SELECT 1 FROM reservations r JOIN shows s ON s.id = r.show_id
        WHERE r.amount_paise <> cardinality(r.seats)::bigint * s.price_paise
    ) THEN RAISE EXCEPTION 'incorrect reservation amount'; END IF;

    IF EXISTS (
        SELECT 1 FROM idempotency_requests WHERE response_status IS NULL OR response_body IS NULL
    ) THEN RAISE EXCEPTION 'incomplete committed idempotency result'; END IF;
END $$;
SELECT 'all committed invariants hold' AS audit_result,
       (SELECT count(*) FROM shows) AS shows,
       (SELECT count(*) FROM reservations) AS historical_reservations,
       (SELECT count(*) FROM idempotency_requests) AS idempotency_records;
COMMIT;
