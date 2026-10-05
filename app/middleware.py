"""ASGI request logging without a task group and response stream per request."""

import logging
import re
from time import monotonic
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("reservation.api")


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("X-Request-ID", "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", supplied) else str(uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = monotonic()
        response_started = False
        status = 500

        async def correlated_send(message: Message) -> None:
            nonlocal response_started, status
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, correlated_send)
        except Exception:
            log.exception("Unhandled request failure", extra={"fields": {"request_id": request_id}})
            if response_started:
                raise
            response = JSONResponse(
                {
                    "error": {"code": "internal_error", "message": "Unexpected server failure"},
                    "request_id": request_id,
                },
                status_code=500,
            )
            await response(scope, receive, correlated_send)
        finally:
            log.info(
                "request_completed",
                extra={
                    "fields": {
                        "request_id": request_id,
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": status,
                        "duration_ms": round((monotonic() - started) * 1000, 2),
                        "outcome": state.get("outcome", "http_response"),
                    }
                },
            )
