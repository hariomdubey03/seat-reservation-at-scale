"""PostgreSQL is the authority for reservations across processes and machines."""

from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.domain import DomainError, Outcome, ReserveCommand


def reservation_body(row: dict) -> dict:
    return {
        "reservation_id": str(row["id"]),
        "show_id": str(row["show_id"]),
        "user_id": row["user_id"],
        "seats": row["seats"],
        "amount_paise": row["amount_paise"],
        "status": row["status"],
    }


class PostgresReservationStore:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def create_show(self, name: str, seats: list[str], price: int, limit: int) -> dict:
        show_id = uuid4()
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats)
                   VALUES (%s, %s, %s, %s, %s)""",
                (show_id, name, price, limit, len(seats)),
            )
            await conn.execute(
                "INSERT INTO seats (show_id, seat_number) SELECT %s, unnest(%s::text[])",
                (show_id, seats),
            )
            result = await self._get_show(conn, show_id)
        return result

    async def get_show(self, show_id: UUID) -> dict:
        async with self.pool.connection() as conn:
            return await self._get_show(conn, show_id)

    @staticmethod
    async def _get_show(conn: AsyncConnection, show_id: UUID) -> dict:
        # One statement means counts and seat details see precisely the same snapshot.
        cursor = await conn.execute(
            """SELECT s.id, s.name, s.price_paise, s.per_user_limit, s.total_seats,
                      jsonb_agg(jsonb_build_object('seat_number', t.seat_number,
                                'status', t.status) ORDER BY t.seat_number) AS seats,
                      count(*) FILTER (WHERE t.status = 'available') AS available,
                      count(*) FILTER (WHERE t.status = 'confirmed') AS confirmed
                 FROM shows s JOIN seats t ON t.show_id = s.id
                WHERE s.id = %s GROUP BY s.id""",
            (show_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise DomainError(404, "show_not_found", "Show does not exist")
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "price_paise": row["price_paise"],
            "per_user_limit": row["per_user_limit"],
            "total_seats": row["total_seats"],
            "seats": row["seats"],
            "counts": {
                "available": row["available"],
                "held": 0,
                "confirmed": row["confirmed"],
                "total_seats": row["total_seats"],
            },
        }

    async def reserve(self, command: ReserveCommand) -> Outcome:
        async with self.pool.connection() as conn, conn.transaction():
            claimed = await conn.execute(
                """INSERT INTO idempotency_requests (user_id, operation, key, request_body)
                   VALUES (%s, 'reserve', %s, %s) ON CONFLICT DO NOTHING RETURNING key""",
                (command.user_id, command.key, Jsonb(command.request_body())),
            )
            if await claimed.fetchone() is None:
                # ON CONFLICT waits for the competing transaction. At READ COMMITTED,
                # this next statement sees its completed result (including failures).
                return await self._replay(conn, command)

            try:
                result = await self._book(conn, command)
            except DomainError as error:
                # Domain declines commit their result; they never perform seat/quota writes.
                await self._event(conn, error.code)
                result = Outcome(error.status_code, error.as_body())

            await conn.execute(
                """UPDATE idempotency_requests SET response_status = %s, response_body = %s
                    WHERE user_id = %s AND operation = 'reserve' AND key = %s""",
                (result.status_code, Jsonb(result.body), command.user_id, command.key),
            )
        # This line is reached only after a successful commit.
        return result

    async def _replay(self, conn: AsyncConnection, command: ReserveCommand) -> Outcome:
        cursor = await conn.execute(
            """SELECT request_body, response_status, response_body FROM idempotency_requests
                WHERE user_id = %s AND operation = 'reserve' AND key = %s""",
            (command.user_id, command.key),
        )
        previous = await cursor.fetchone()
        if previous is None or previous["response_status"] is None:
            raise RuntimeError("Incomplete committed idempotency record")
        if previous["request_body"] != command.request_body():
            await self._event(conn, "idempotency_conflict")
            error = DomainError(409, "idempotency_conflict", "Key was used for a different request")
            return Outcome(409, error.as_body())
        await self._event(conn, "idempotent_replay")
        status = 200 if previous["response_status"] == 201 else previous["response_status"]
        return Outcome(status, previous["response_body"], replayed=True)

    async def _book(self, conn: AsyncConnection, command: ReserveCommand) -> Outcome:
        cursor = await conn.execute("SELECT * FROM shows WHERE id = %s", (command.show_id,))
        show = await cursor.fetchone()
        if show is None:
            raise DomainError(404, "show_not_found", "Show does not exist")

        await conn.execute(
            """INSERT INTO user_show_quota (show_id, user_id) VALUES (%s, %s)
               ON CONFLICT DO NOTHING""",
            (command.show_id, command.user_id),
        )
        cursor = await conn.execute(
            """SELECT active_seats FROM user_show_quota
               WHERE show_id = %s AND user_id = %s FOR UPDATE""",
            (command.show_id, command.user_id),
        )
        quota = await cursor.fetchone()
        if quota["active_seats"] + len(command.seats) > show["per_user_limit"]:
            raise DomainError(409, "per_user_limit", "Per-user seat limit would be exceeded")

        cursor = await conn.execute(
            """SELECT seat_number, status FROM seats
               WHERE show_id = %s AND seat_number = ANY(%s)
               ORDER BY seat_number FOR UPDATE""",
            (command.show_id, list(command.seats)),
        )
        seats = await cursor.fetchall()
        if len(seats) != len(command.seats):
            raise DomainError(404, "seat_not_found", "One or more seats do not exist")
        if any(seat["status"] != "available" for seat in seats):
            raise DomainError(409, "seat_taken", "One or more requested seats are already taken")

        reservation_id = uuid4()
        amount = show["price_paise"] * len(command.seats)
        cursor = await conn.execute(
            """INSERT INTO reservations (id, show_id, user_id, seats, amount_paise, status)
               VALUES (%s, %s, %s, %s, %s, 'confirmed') RETURNING *""",
            (reservation_id, command.show_id, command.user_id, list(command.seats), amount),
        )
        booking = await cursor.fetchone()
        cursor = await conn.execute(
            """UPDATE seats SET status = 'confirmed', reservation_id = %s
               WHERE show_id = %s AND seat_number = ANY(%s) AND status = 'available'""",
            (reservation_id, command.show_id, list(command.seats)),
        )
        if cursor.rowcount != len(command.seats):
            raise RuntimeError("Locked seat ownership changed unexpectedly")
        await conn.execute(
            """UPDATE user_show_quota SET active_seats = active_seats + %s
               WHERE show_id = %s AND user_id = %s""",
            (len(command.seats), command.show_id, command.user_id),
        )
        return Outcome(201, reservation_body(booking))

    async def cancel(self, reservation_id: UUID, user_id: str) -> dict:
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                "SELECT * FROM reservations WHERE id = %s",
                (reservation_id,),
            )
            booking = await cursor.fetchone()
            if booking is None:
                raise DomainError(404, "reservation_not_found", "Reservation does not exist")
            if booking["user_id"] != user_id:
                raise DomainError(403, "not_owner", "Only the reservation owner may cancel")

            # Ownership and show ID are immutable. Acquire quota before reservation/seats,
            # matching reserve's order and preventing a cancel/reserve deadlock.
            await conn.execute(
                """SELECT active_seats FROM user_show_quota
                   WHERE show_id = %s AND user_id = %s FOR UPDATE""",
                (booking["show_id"], user_id),
            )
            cursor = await conn.execute(
                "SELECT * FROM reservations WHERE id = %s FOR UPDATE",
                (reservation_id,),
            )
            booking = await cursor.fetchone()
            if booking["status"] == "cancelled":
                return reservation_body(booking)
            cursor = await conn.execute(
                """SELECT seat_number FROM seats WHERE reservation_id = %s
                   ORDER BY seat_number FOR UPDATE""",
                (reservation_id,),
            )
            owned = await cursor.fetchall()
            if len(owned) != len(booking["seats"]):
                raise RuntimeError("Reservation seat ownership invariant violated")
            await conn.execute(
                """UPDATE seats SET status = 'available', reservation_id = NULL
                   WHERE reservation_id = %s AND status = 'confirmed'""",
                (reservation_id,),
            )
            await conn.execute(
                """UPDATE user_show_quota SET active_seats = active_seats - %s
                   WHERE show_id = %s AND user_id = %s""",
                (len(owned), booking["show_id"], user_id),
            )
            cursor = await conn.execute(
                """UPDATE reservations SET status = 'cancelled', cancelled_at = now()
                   WHERE id = %s RETURNING *""",
                (reservation_id,),
            )
            result = reservation_body(await cursor.fetchone())
        return result

    @staticmethod
    async def _event(conn: AsyncConnection, reason: str) -> None:
        await conn.execute("INSERT INTO request_events (reason) VALUES (%s)", (reason,))

    async def metrics_snapshot(self) -> dict[str, Any]:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor = await conn.execute(
                """SELECT count(*) AS confirmed,
                          count(*) FILTER (WHERE status = 'cancelled') AS cancelled
                   FROM reservations""",
            )
            counters = await cursor.fetchone()
            cursor = await conn.execute(
                "SELECT reason, count(*) AS count FROM request_events GROUP BY reason",
            )
            events = await cursor.fetchall()
            cursor = await conn.execute(
                """SELECT show_id, count(*) AS total_seats,
                          count(*) FILTER (WHERE status = 'available') AS available,
                          count(*) FILTER (WHERE status = 'confirmed') AS confirmed
                   FROM seats GROUP BY show_id""",
            )
            shows = await cursor.fetchall()
        return {"counters": counters, "events": events, "shows": shows}
