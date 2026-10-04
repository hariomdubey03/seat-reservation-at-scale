CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    installed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shows (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    price_paise bigint NOT NULL CHECK (price_paise BETWEEN 0 AND 1000000000000),
    per_user_limit integer NOT NULL CHECK (per_user_limit BETWEEN 1 AND 100),
    total_seats integer NOT NULL CHECK (total_seats BETWEEN 1 AND 10000),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reservations (
    id uuid PRIMARY KEY,
    show_id uuid NOT NULL REFERENCES shows(id),
    user_id text NOT NULL,
    seats text[] NOT NULL CHECK (cardinality(seats) > 0),
    amount_paise bigint NOT NULL CHECK (amount_paise >= 0),
    status text NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    cancelled_at timestamptz,
    UNIQUE (show_id, id)
);
CREATE INDEX IF NOT EXISTS reservations_user_show ON reservations (show_id, user_id);

CREATE TABLE IF NOT EXISTS seats (
    show_id uuid NOT NULL REFERENCES shows(id),
    seat_number text NOT NULL,
    status text NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'confirmed')),
    reservation_id uuid,
    PRIMARY KEY (show_id, seat_number),
    FOREIGN KEY (show_id, reservation_id) REFERENCES reservations (show_id, id),
    CHECK ((status = 'available' AND reservation_id IS NULL)
        OR (status = 'confirmed' AND reservation_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS seats_reservation ON seats (reservation_id)
    WHERE reservation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS user_show_quota (
    show_id uuid NOT NULL REFERENCES shows(id),
    user_id text NOT NULL,
    active_seats integer NOT NULL DEFAULT 0 CHECK (active_seats BETWEEN 0 AND 100),
    PRIMARY KEY (show_id, user_id)
);

CREATE TABLE IF NOT EXISTS idempotency_requests (
    user_id text NOT NULL,
    operation text NOT NULL,
    key text NOT NULL,
    request_body jsonb NOT NULL,
    response_status integer,
    response_body jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, operation, key),
    CHECK ((response_status IS NULL) = (response_body IS NULL))
);

-- Append-only outcome records avoid one globally contended metrics counter row.
CREATE TABLE IF NOT EXISTS request_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS request_events_reason ON request_events (reason);

INSERT INTO schema_migrations (version) VALUES (1) ON CONFLICT DO NOTHING;
