"""HTTP-independent commands, outcomes, and the small persistence contract."""

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class Outcome:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


class DomainError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def as_body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


@dataclass(frozen=True)
class ReserveCommand:
    user_id: str
    show_id: UUID
    seats: tuple[str, ...]
    key: str

    def request_body(self) -> dict:
        return {"show_id": str(self.show_id), "seats": list(self.seats)}


class ReservationStore(Protocol):
    """Implementations must commit each mutation atomically before returning."""

    async def create_show(self, name: str, seats: list[str], price: int, limit: int) -> dict: ...
    async def get_show(self, show_id: UUID) -> dict: ...
    async def reserve(self, command: ReserveCommand) -> Outcome: ...
    async def cancel(self, reservation_id: UUID, user_id: str) -> dict: ...


class ReservationService:
    def __init__(self, store: ReservationStore):
        self.store = store

    async def create_show(self, name: str, seats: list[str], price: int, limit: int) -> dict:
        return await self.store.create_show(name, sorted(seats), price, limit)

    async def get_show(self, show_id: UUID) -> dict:
        return await self.store.get_show(show_id)

    async def reserve(self, user_id: str, show_id: UUID, seats: list[str], key: str) -> Outcome:
        # Ordering does not change the meaning of an all-or-nothing request.
        command = ReserveCommand(user_id, show_id, tuple(sorted(seats)), key)
        return await self.store.reserve(command)

    async def cancel(self, reservation_id: UUID, user_id: str) -> dict:
        return await self.store.cancel(reservation_id, user_id)
