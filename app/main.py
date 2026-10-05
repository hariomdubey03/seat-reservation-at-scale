import asyncio
import logging
import re
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from app.auth import TokenService
from app.config import Settings
from app.domain import DomainError, ReservationService
from app.observability import configure_logging, render_metrics
from app.repository import PostgresReservationStore
from app.schemas import CreateShow, IssueToken, ReserveSeats

log = logging.getLogger("reservation.api")
bearer = HTTPBearer(auto_error=False)


def make_pool(settings: Settings, *, health: bool = False) -> AsyncConnectionPool:
    return AsyncConnectionPool(
        settings.database_url,
        open=False,
        min_size=1 if health else min(2, settings.db_pool_size),
        max_size=1 if health else settings.db_pool_size,
        timeout=2 if health else settings.db_pool_timeout,
        max_waiting=25000,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "connect_timeout": 3,
            "options": "-c statement_timeout=15000 -c idle_in_transaction_session_timeout=15000",
        },
        check=AsyncConnectionPool.check_connection,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = Settings.from_env()
    pool = make_pool(settings)
    health_pool = make_pool(settings, health=True)
    await pool.open()
    await health_pool.open()
    app.state.repository = PostgresReservationStore(pool)
    app.state.service = ReservationService(app.state.repository)
    app.state.tokens = TokenService(settings)
    app.state.health_pool = health_pool
    try:
        yield
    finally:
        await health_pool.close()
        await pool.close()


app = FastAPI(title="Seat Reservation", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", supplied) else str(uuid4())
    request.state.request_id = request_id
    started = monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("Unhandled request failure", extra={"fields": {"request_id": request_id}})
        response = JSONResponse(
            {
                "error": {"code": "internal_error", "message": "Unexpected server failure"},
                "request_id": request_id,
            },
            status_code=500,
        )
    response.headers["X-Request-ID"] = request_id
    fields = {
        "request_id": request_id,
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "duration_ms": round((monotonic() - started) * 1000, 2),
        "outcome": getattr(request.state, "outcome", "http_response"),
    }
    log.info("request_completed", extra={"fields": fields})
    return response


@app.exception_handler(DomainError)
async def domain_error(request: Request, error: DomainError):
    request.state.outcome = error.code
    headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else {}
    return JSONResponse(
        {**error.as_body(), "request_id": request.state.request_id},
        status_code=error.status_code,
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    # Avoid echoing untrusted fields or authentication material into the response/log.
    return await domain_error(
        request, DomainError(422, "invalid_request", "Invalid request fields")
    )


async def dependency_error(request: Request, error: Exception):
    log.warning(
        "Database unavailable",
        extra={
            "fields": {
                "request_id": request.state.request_id,
                "exception_type": type(error).__name__,
            }
        },
    )
    return await domain_error(
        request,
        DomainError(503, "dependency_unavailable", "Database temporarily unavailable"),
    )


for error_class in (psycopg.OperationalError, PoolTimeout, PoolClosed, TooManyRequests):
    app.add_exception_handler(error_class, dependency_error)


def token_value(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None:
        raise DomainError(401, "authentication_required", "Bearer token required")
    return credentials.credentials


async def require_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    request.app.state.tokens.require_admin(token_value(credentials))


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    return request.app.state.tokens.user_id(token_value(credentials))


async def service(request: Request) -> ReservationService:
    return request.app.state.service


@app.post("/auth/token", dependencies=[Depends(require_admin)])
async def issue_token(body: IssueToken, request: Request):
    return {"access_token": request.app.state.tokens.issue(body.user_id), "token_type": "bearer"}


@app.post("/shows", status_code=201, dependencies=[Depends(require_admin)])
async def create_show(body: CreateShow, svc: Annotated[ReservationService, Depends(service)]):
    return await svc.create_show(body.name, body.seats, body.price_paise, body.per_user_limit)


@app.get("/shows/{show_id}")
async def show_state(show_id: UUID, svc: Annotated[ReservationService, Depends(service)]):
    return await svc.get_show(show_id)


@app.post("/shows/{show_id}/reserve", status_code=201)
async def reserve(
    show_id: UUID,
    body: ReserveSeats,
    request: Request,
    user_id: Annotated[str, Depends(current_user)],
    svc: Annotated[ReservationService, Depends(service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if idempotency_key and body.idempotency_key and idempotency_key != body.idempotency_key:
        raise DomainError(422, "invalid_request", "Header and body idempotency keys must match")
    key = idempotency_key if idempotency_key is not None else body.idempotency_key
    if key is None or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key) is None:
        raise DomainError(422, "invalid_request", "A valid idempotency key is required")
    outcome = await svc.reserve(user_id, show_id, body.seats, key)
    payload = dict(outcome.body)
    if "error" in payload:
        payload["request_id"] = request.state.request_id
    request.state.outcome = (
        "idempotent_replay"
        if outcome.replayed
        else payload["error"]["code"]
        if "error" in payload
        else "confirmed"
    )
    return JSONResponse(
        payload,
        status_code=outcome.status_code,
        headers={"Idempotent-Replayed": "true"} if outcome.replayed else {},
    )


@app.post("/reservations/{reservation_id}/cancel")
async def cancel(
    reservation_id: UUID,
    request: Request,
    user_id: Annotated[str, Depends(current_user)],
    svc: Annotated[ReservationService, Depends(service)],
):
    result = await svc.cancel(reservation_id, user_id)
    request.state.outcome = "cancelled"
    return result


@app.get("/health/live")
async def liveness():
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness(request: Request):
    try:
        async with asyncio.timeout(3), request.app.state.health_pool.connection() as conn:
            cursor = await conn.execute("SELECT version FROM schema_migrations WHERE version = 1")
            if await cursor.fetchone() is None:
                raise DomainError(503, "dependency_unavailable", "Schema is not ready")
    except (TimeoutError, psycopg.Error, PoolTimeout, PoolClosed) as exc:
        raise DomainError(503, "dependency_unavailable", "Database is not ready") from exc
    return {"status": "ready"}


@app.get("/metrics")
async def metrics(request: Request):
    snapshot = await request.app.state.repository.metrics_snapshot()
    return Response(render_metrics(snapshot), headers={"Content-Type": CONTENT_TYPE_LATEST})
