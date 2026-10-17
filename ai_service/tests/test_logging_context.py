import asyncio
import json
import logging
import sys
from pathlib import Path


backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import logging_config
from logging_config import (
    CorrelationFilter,
    JsonFormatter,
    RequestIdMiddleware,
    TextFormatter,
    job_id_var,
    request_id_var,
    setup_logging,
)


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def make_logger(name, log_format="text"):
    logger = logging.getLogger(name)
    logger.propagate = False
    setup_logging(logger, log_format)
    handler = ListHandler()
    logger.addHandler(handler)
    return logger, handler


def log_like_service(logger, label, message, **fields):
    logger.info(message, extra={"event": label, "fields": fields})


def test_job_id_and_request_id_on_every_record():
    logger, handler = make_logger("t_ids")
    req_token = request_id_var.set("req-1")
    job_token = job_id_var.set("job-1")
    try:
        log_like_service(logger, "RUN", "step one")
        log_like_service(logger, "OK", "step two")
    finally:
        job_id_var.reset(job_token)
        request_id_var.reset(req_token)

    assert [r.job_id for r in handler.records] == ["job-1", "job-1"]
    assert [r.request_id for r in handler.records] == ["req-1", "req-1"]


def test_ids_default_to_dash_outside_request_and_job():
    logger, handler = make_logger("t_defaults")
    log_like_service(logger, "INFO", "no context")
    assert handler.records[0].job_id == "-"
    assert handler.records[0].request_id == "-"


def test_concurrent_jobs_do_not_mix_ids():
    logger, handler = make_logger("t_concurrent")

    async def job(name):
        token = job_id_var.set(name)
        try:
            for i in range(3):
                log_like_service(logger, "RUN", f"{name} step {i}")
                await asyncio.sleep(0)
        finally:
            job_id_var.reset(token)

    async def main():
        await asyncio.gather(job("job-a"), job("job-b"))

    asyncio.run(main())
    for record in handler.records:
        assert record.getMessage().startswith(record.job_id)


def test_json_output_is_parseable():
    logger = logging.getLogger("t_json")
    record = logger.makeRecord(
        "t_json", logging.INFO, __file__, 1, "Starting generation", (), None,
        extra={"event": "START", "fields": {"topic": "React Hooks", "job": "x"}},
    )
    record.request_id = "9f2c"
    record.job_id = "React_Hooks_1"
    data = json.loads(JsonFormatter().format(record))
    assert data["level"] == "INFO"
    assert data["request_id"] == "9f2c"
    assert data["job_id"] == "React_Hooks_1"
    assert data["msg"] == "Starting generation"
    assert data["topic"] == "React Hooks"
    assert data["ts"].endswith("Z")


def test_json_reserved_field_names_do_not_overwrite():
    logger = logging.getLogger("t_json2")
    record = logger.makeRecord(
        "t_json2", logging.INFO, __file__, 1, "m", (), None,
        extra={"fields": {"job_id": "spoofed"}},
    )
    record.request_id = "-"
    record.job_id = "real"
    data = json.loads(JsonFormatter().format(record))
    assert data["job_id"] == "real"
    assert data["field_job_id"] == "spoofed"


def test_text_format_keeps_existing_layout_and_appends_ids():
    logger = logging.getLogger("t_text")
    record = logger.makeRecord(
        "t_text", logging.INFO, __file__, 1, "Lesson request accepted", (), None,
        extra={"event": "QUEUE", "fields": {"topic": "Asyncio"}},
    )
    record.request_id = "-"
    record.job_id = "job-9"
    assert TextFormatter().format(record) == (
        "[QUEUE] Lesson request accepted | topic=Asyncio | job_id=job-9"
    )


def test_setup_logging_is_idempotent():
    logger = logging.getLogger("t_idem")
    setup_logging(logger, "json")
    setup_logging(logger, "text")
    ours = [h for h in logger.handlers if getattr(h, "_ai_service_handler", False)]
    filters = [f for f in logger.filters if isinstance(f, CorrelationFilter)]
    assert len(ours) == 1
    assert len(filters) == 1
    assert isinstance(ours[0].formatter, TextFormatter)


def run_asgi(app, headers=None, scope_type="http"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": scope_type, "headers": headers or []}
    try:
        asyncio.run(RequestIdMiddleware(app)(scope, receive, send))
    except Exception:
        pass
    return sent


def header_value(sent, name=b"x-request-id"):
    start = next(m for m in sent if m["type"] == "http.response.start")
    values = [v for k, v in start["headers"] if k.lower() == name]
    return values[0].decode() if values else None


async def ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_middleware_generates_request_id():
    sent = run_asgi(ok_app)
    assert header_value(sent)
    assert len(header_value(sent)) == 32


def test_middleware_echoes_valid_inbound_request_id():
    sent = run_asgi(ok_app, headers=[(b"x-request-id", b"abc-123")])
    assert header_value(sent) == "abc-123"


def test_middleware_replaces_unsafe_inbound_request_id():
    sent = run_asgi(ok_app, headers=[(b"x-request-id", b"bad id\twith spaces")])
    assert header_value(sent) != "bad id\twith spaces"
    assert len(header_value(sent)) == 32


def test_middleware_does_not_duplicate_existing_header():
    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"X-Request-ID", b"from-app")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    sent = run_asgi(app, headers=[(b"x-request-id", b"abc")])
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert [v for k, v in start["headers"] if k.lower() == b"x-request-id"] == [b"abc"]


def test_middleware_adds_header_to_unhandled_error_response():
    async def boom(scope, receive, send):
        raise RuntimeError("boom")

    sent = run_asgi(boom, headers=[(b"x-request-id", b"err-1")])
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 500
    assert header_value(sent) == "err-1"


def test_request_id_is_visible_to_work_done_after_response_start():
    """Background tasks run after the response is sent but inside the same ASGI call."""
    logger, handler = make_logger("t_bg")

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
        token = job_id_var.set("bg-job")
        try:
            log_like_service(logger, "RUN", "background work")
        finally:
            job_id_var.reset(token)

    run_asgi(app, headers=[(b"x-request-id", b"req-bg")])
    assert handler.records[0].request_id == "req-bg"
    assert handler.records[0].job_id == "bg-job"
    assert request_id_var.get() == "-"


def test_process_lesson_logs_include_job_id():
    """A real process_lesson run: every log line it emits carries the job_id."""
    import os
    from unittest.mock import AsyncMock, MagicMock, patch

    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "CLOUDINARY_CLOUD_NAME",
                "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        os.environ.setdefault(key, f"dummy_{key.lower()}")

    import api

    handler = ListHandler()
    api.logger.addHandler(handler)
    try:
        req = api.LessonRequest(course="Python", topic="Logging", celebrity="Modi")
        base_filename = "Logging_job_320"

        gemini_resp = MagicMock()
        gemini_resp.text = "A short test script about logging."
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"")

        with patch.object(api.gemini_client.models, "generate_content", return_value=gemini_resp), \
             patch("api.generate_tts", new_callable=AsyncMock), \
             patch("api.create_avatar_video", side_effect=Exception("D-ID disabled")), \
             patch("os.path.exists", side_effect=lambda p: False if p.endswith(".mp3") else True), \
             patch("os.remove"), \
             patch("asyncio.create_subprocess_exec", return_value=proc), \
             patch("cloudinary.uploader.upload", return_value={"secure_url": "http://cloudinary.com/x.mp4"}):
            asyncio.run(api.process_lesson(req, base_filename))
    finally:
        api.logger.removeHandler(handler)

    assert handler.records, "process_lesson emitted no log records"
    assert all(r.job_id == base_filename for r in handler.records)
    assert job_id_var.get() == "-"
