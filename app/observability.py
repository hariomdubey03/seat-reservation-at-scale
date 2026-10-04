import json
import logging
import os
from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception_type"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


class SnapshotCollector:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot

    def collect(self):
        snapshot = self.snapshot
        yield CounterMetricFamily(
            "reservations_confirmed",
            "New committed reservations, including later cancellations",
            value=snapshot["counters"]["confirmed"],
        )
        yield CounterMetricFamily(
            "reservations_cancelled",
            "Reservations cancelled by their owner",
            value=snapshot["counters"]["cancelled"],
        )
        events = {row["reason"]: row["count"] for row in snapshot["events"]}
        yield CounterMetricFamily(
            "reservations_replayed",
            "Idempotent replays; no new reservation",
            value=events.get("idempotent_replay", 0),
        )
        declines = CounterMetricFamily(
            "reservations_declined",
            "Non-new reservation outcomes; replays included for exercise",
            labels=["reason"],
        )
        for reason in sorted(
            set(events)
            | {
                "seat_taken",
                "per_user_limit",
                "idempotency_conflict",
                "idempotent_replay",
                "show_not_found",
                "seat_not_found",
            }
        ):
            declines.add_metric([reason], events.get(reason, 0))
        yield declines
        for state in ("available", "held", "confirmed", "total_seats"):
            name = "seats_total" if state == "total_seats" else f"seats_{state}"
            gauge = GaugeMetricFamily(name, f"Current {state} seats", labels=["show_id"])
            for show in snapshot["shows"]:
                gauge.add_metric([str(show["show_id"])], show.get(state, 0))
            yield gauge


def render_metrics(snapshot: dict) -> bytes:
    registry = CollectorRegistry()
    registry.register(SnapshotCollector(snapshot))
    return generate_latest(registry)
