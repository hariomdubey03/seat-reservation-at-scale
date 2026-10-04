from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

SeatNumber = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]
UserId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")]
IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SeatSelection(StrictBody):
    seats: list[SeatNumber] = Field(min_length=1, max_length=10000)

    @field_validator("seats")
    @classmethod
    def unique_seats(cls, seats: list[str]) -> list[str]:
        if len(seats) != len(set(seats)):
            raise ValueError("Seat numbers must be unique")
        return seats


class CreateShow(SeatSelection):
    name: str = Field(min_length=1, max_length=200)
    price_paise: int = Field(ge=0, le=10**12)
    per_user_limit: int = Field(default=4, ge=1, le=100)


class ReserveSeats(SeatSelection):
    idempotency_key: IdempotencyKey | None = None


class IssueToken(StrictBody):
    user_id: UserId
