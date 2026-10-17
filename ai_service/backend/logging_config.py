"""Structured logging helpers: request/job correlation IDs and optional JSON output."""

import datetime
import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-ID"

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
job_id_var: ContextVar[str] = ContextVar("job_id", default="-")

# Inbound IDs are only trusted if they look like a plain token (no log injection).
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_HANDLER_MARKER = "_ai_service_handler"
_RESERVED_KEYS = {"ts", "level", "logger", "request_id", "job_id", "msg", "event", "exc"}


class CorrelationFilter(logging.Filter):
    """Copy the current request_id and job_id onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.job_id = job_id_var.get()
        return True


class TextFormatter(logging.Formatter):
    """Keeps the existing `[LABEL] message | key=value` layout, then appends IDs."""

    def format(self, record: logging.LogRecord) -> str:
        label = getattr(record, "event", record.levelname)
        parts = [f"[{label}] {record.getMessage()}"]
        for key, value in getattr(record, "fields", {}).items():
            parts.append(f"{key}={value}")
        for key in ("request_id", "job_id"):
            value = getattr(record, key, "-")
            if value != "-":
                parts.append(f"{key}={value}")
        line = " | ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    """One valid JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
        payload = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "job_id": getattr(record, "job_id", "-"),
            "msg": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        for key, value in getattr(record, "fields", {}).items():
            payload[key if key not in _RESERVED_KEYS else f"field_{key}"] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is at emit time (safe under pytest capture)."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stdout


def setup_logging(logger: logging.Logger, log_format: str = "text") -> None:
    """Attach one stdout handler to `logger`. `json` gives JSON lines; anything else is text."""
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
    for existing in list(logger.filters):
        if isinstance(existing, CorrelationFilter):
            logger.removeFilter(existing)

    handler = _StdoutHandler()
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(
        JsonFormatter() if (log_format or "").strip().lower() == "json" else TextFormatter()
    )
    logger.addFilter(CorrelationFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class RequestIdMiddleware:
    """Pure ASGI middleware: reuse or create X-Request-ID, bind it, echo it on responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request_id = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                candidate = value.decode("latin-1")
                if _VALID_REQUEST_ID.match(candidate):
                    request_id = candidate
                break
        request_id = request_id or uuid.uuid4().hex
        header = (REQUEST_ID_HEADER.lower().encode(), request_id.encode())

        started = False

        async def send_with_id(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [h for h in message.get("headers", []) if h[0].lower() != header[0]]
                headers.append(header)
                message = {**message, "headers": headers}
            await send(message)

        token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            # Starlette's outer error handler would send a 500 without our header.
            if scope["type"] == "http" and not started:
                await send_with_id(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    }
                )
                await send_with_id(
                    {"type": "http.response.body", "body": b"Internal Server Error"}
                )
            raise
        finally:
            request_id_var.reset(token)
