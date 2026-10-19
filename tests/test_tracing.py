"""OpenTelemetry span shape. No network and no collector."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

from app.agent.ai_client import AIClient
from app.agent.tracing import (
    annotate_pipeline,
    experience_match_span,
    finish_rewrite,
    install_span_exporter,
    job_session_id,
    otlp_headers,
    pipeline_span,
    record_output,
    resume_match_span,
    rewrite_span,
    setup_tracing,
    shutdown_tracing,
    traces_endpoint,
    tracing_enabled,
)

JOB = {"title": "AI Engineer", "company": "Acme"}
JOB_KEY = "https://example.com/jobs/" + "x" * 300


class Out(BaseModel):
    score: int = 0


class _FakeHTTPError(Exception):
    def __init__(self, status: int):
        super().__init__(f"http {status}")
        self.response = SimpleNamespace(status_code=status)


SECRET = "SECRET_RESUME_TEXT"


class MemoryExporter(SpanExporter):
    """Keeps ended spans in a list. The SDK no longer ships an in-memory one."""

    def __init__(self) -> None:
        self._spans = []

    def export(self, spans):
        self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def get_finished_spans(self):
        return list(self._spans)


@pytest.fixture(autouse=True)
def _isolated_tracing():
    shutdown_tracing()
    yield
    shutdown_tracing()


def _client() -> AIClient:
    return AIClient(
        config={
            "api_key": "test-key",
            "ai_model": "test-model",
            "ai_base_url": "http://localhost:9/v1",
            "rate_limited": False,
        }
    )


def _ok_response():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(parsed=Out(score=7), refusal=None))
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=5),
    )


def _blob(span) -> str:
    parts = [span.name, str(span.status.description or "")]
    parts.extend(f"{key}={value}" for key, value in (span.attributes or {}).items())
    for event in span.events:
        parts.append(event.name)
        parts.extend(
            f"{key}={value}" for key, value in (event.attributes or {}).items()
        )
    return "\n".join(parts)


def test_match_traces_are_separate_roots_in_the_job_session(monkeypatch):
    monkeypatch.setattr("app.agent.ai_client.time.sleep", lambda _s: None)
    monkeypatch.setattr("app.agent.ai_client.MAX_PARSE_ATTEMPTS", 3)
    exporter = MemoryExporter()
    install_span_exporter(exporter, capture_content=False)
    client = _client()
    calls = {"n": 0}

    def fake_parse(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ok_response()
        raise _FakeHTTPError(503)

    monkeypatch.setattr(client._client.beta.chat.completions, "parse", fake_parse)

    with pipeline_span("run-1", "manual") as root:
        with experience_match_span(JOB_KEY, JOB):
            client.parse(
                messages=[{"role": "user", "content": SECRET}],
                text_format=Out,
                label="experience[Role @ Co]",
            )
        with pytest.raises(_FakeHTTPError):
            with resume_match_span(JOB_KEY, JOB):
                client.parse(
                    messages=[{"role": "user", "content": SECRET}],
                    text_format=Out,
                    label="fit[Role @ Co]",
                )
        annotate_pipeline(
            root,
            {
                "status": "degraded",
                "jobs_scraped": 2,
                "new_jobs": 1,
                "scored": 1,
                "score_failed": 1,
                "alerts_sent": 0,
            },
        )

    finished = exporter.get_finished_spans()
    parents = [span for span in finished if span.name == "pipeline · manual"]
    children = [
        span
        for span in finished
        if (span.attributes or {}).get("gen_ai.operation.name") == "chat"
    ]
    assert len(parents) == 1
    assert len(children) == 2
    parent = parents[0]
    assert all(child.context.trace_id != parent.context.trace_id for child in children)
    assert "langfuse.session.id" not in parent.attributes

    by_name = {span.name: span for span in finished}
    experience_root = by_name["Experience Match"]
    resume_root = by_name["Resume Match"]
    session = job_session_id(JOB_KEY, "AI Engineer @ Acme")
    for job_root in (experience_root, resume_root):
        assert job_root.parent is None
        assert job_root.attributes["langfuse.session.id"] == session
        assert job_root.attributes["langfuse.trace.metadata.run_id"] == "run-1"
        assert job_root.attributes["langfuse.trace.metadata.job_title"] == "AI Engineer @ Acme"
    assert experience_root.context.trace_id != resume_root.context.trace_id
    assert experience_root.status.status_code != StatusCode.ERROR
    assert resume_root.status.status_code == StatusCode.ERROR
    assert parent.attributes["run_id"] == "run-1"
    assert parent.attributes["status"] == "degraded"
    assert parent.attributes["jobs_scraped"] == 2
    assert "jobspy.agent" not in parent.attributes
    assert "gen_ai.request.model" not in parent.attributes
    assert "jobspy.attempts" not in parent.attributes
    assert parent.status.status_code != StatusCode.ERROR

    by_agent = {span.attributes["jobspy.agent"]: span for span in children}
    assert by_agent["experience"].parent.span_id == experience_root.context.span_id
    assert by_agent["fit"].parent.span_id == resume_root.context.span_id
    assert all(span.attributes["langfuse.session.id"] == session for span in children)
    success = by_agent["experience"]
    assert success.attributes["jobspy.attempts"] == 1
    assert success.attributes["gen_ai.request.model"] == "test-model"
    assert success.attributes["gen_ai.operation.name"] == "chat"
    assert success.attributes["gen_ai.usage.input_tokens"] == 11
    assert success.attributes["gen_ai.usage.output_tokens"] == 5
    assert "http.response.status_code" not in success.attributes
    assert success.status.status_code != StatusCode.ERROR
    assert success.events == ()

    failure = by_agent["fit"]
    assert failure.attributes["jobspy.attempts"] == 3
    assert failure.attributes["http.response.status_code"] == 503
    assert failure.status.status_code == StatusCode.ERROR
    assert len(failure.events) == 3
    assert all(event.name == "attempt.failed" for event in failure.events)

    exported = "\n".join(_blob(span) for span in finished)
    assert SECRET not in exported


def test_content_capture_records_inputs_and_outputs(monkeypatch):
    exporter = MemoryExporter()
    install_span_exporter(exporter)
    client = _client()
    monkeypatch.setattr(
        client._client.beta.chat.completions, "parse", lambda **_kwargs: _ok_response()
    )

    with resume_match_span(JOB_KEY, JOB, SECRET) as root:
        client.parse(
            messages=[{"role": "user", "content": SECRET}],
            text_format=Out,
            label="fit[Role @ Co]",
        )
        record_output(root, {"score": 7})
    with rewrite_span("rw-2", JOB_KEY, 2, title="AI Engineer @ Acme", inputs={"job": JOB}) as rw:
        finish_rewrite(rw, "done", output={"status": "done", "winning_total": 9})

    finished = exporter.get_finished_spans()
    by_name = {span.name: span for span in finished}
    chat = by_name["fit[Role @ Co]"].attributes
    assert json.loads(chat["langfuse.observation.input"]) == [
        {"role": "user", "content": SECRET}
    ]
    assert json.loads(chat["langfuse.observation.output"]) == {"score": 7}
    assert "langfuse.trace.input" not in chat

    match = by_name["Resume Match"].attributes
    for key in ("langfuse.observation.input", "langfuse.trace.input"):
        assert json.loads(match[key]) == {"job": JOB, "resume_text": SECRET}
    for key in ("langfuse.observation.output", "langfuse.trace.output"):
        assert json.loads(match[key]) == {"score": 7}

    rewrite = by_name["Resume Rewrite"].attributes
    assert json.loads(rewrite["langfuse.trace.input"]) == {"job": JOB}
    assert json.loads(rewrite["langfuse.trace.output"])["winning_total"] == 9


def test_long_content_is_truncated(monkeypatch):
    monkeypatch.setattr("app.agent.tracing._CONTENT_LIMIT", 50)
    exporter = MemoryExporter()
    install_span_exporter(exporter)
    with experience_match_span(JOB_KEY, {"title": "x" * 500}):
        pass
    root = exporter.get_finished_spans()[0]
    assert root.attributes["langfuse.trace.input"].endswith("…[truncated]")
    assert len(root.attributes["langfuse.trace.input"]) < 70


def test_worker_thread_job_trace_records_the_run(monkeypatch):
    exporter = MemoryExporter()
    install_span_exporter(exporter)
    client = _client()
    monkeypatch.setattr(
        client._client.beta.chat.completions, "parse", lambda **_kwargs: _ok_response()
    )

    def score():
        with experience_match_span(JOB_KEY, JOB):
            client.parse(
                messages=[{"role": "user", "content": SECRET}],
                text_format=Out,
                label="experience[Role @ Co]",
            )

    with pipeline_span("run-2", "cron"):
        import contextvars

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contextvars.copy_context().run, score).result()

    finished = exporter.get_finished_spans()
    root = next(span for span in finished if span.name == "Experience Match")
    child = next(
        span
        for span in finished
        if (span.attributes or {}).get("gen_ai.operation.name") == "chat"
    )
    assert child.parent.span_id == root.context.span_id
    assert root.attributes["langfuse.trace.metadata.run_id"] == "run-2"
    assert child.attributes["jobspy.agent"] == "experience"


def test_session_id_is_short_ascii_and_stable_per_job():
    session = job_session_id(JOB_KEY, "Ingénieur IA @ Acme")
    assert session == job_session_id(JOB_KEY, "Ingénieur IA @ Acme")
    assert session != job_session_id(JOB_KEY + "2", "Ingénieur IA @ Acme")
    assert session.isascii()
    assert len(session) < 200
    assert session.startswith("Ingnieur IA @ Acme #")
    assert job_session_id(JOB_KEY).startswith("job #")


def test_rewrite_steps_share_one_named_trace(monkeypatch):
    exporter = MemoryExporter()
    install_span_exporter(exporter)
    client = _client()
    monkeypatch.setattr(
        client._client.beta.chat.completions, "parse", lambda **_kwargs: _ok_response()
    )

    with rewrite_span("rw-1", JOB_KEY, 3, title="AI Engineer @ Acme"):
        client.parse(
            messages=[{"role": "user", "content": SECRET}],
            text_format=Out,
            label="planner",
        )
        client.parse(
            messages=[{"role": "user", "content": SECRET}],
            text_format=Out,
            label="executor",
        )

    finished = exporter.get_finished_spans()
    root = next(span for span in finished if span.attributes.get("rewrite_id") == "rw-1")
    children = [span for span in finished if span is not root]
    session = job_session_id(JOB_KEY, "AI Engineer @ Acme")
    assert root.name == "Resume Rewrite"
    assert root.attributes["langfuse.trace.name"] == root.name
    assert root.attributes["langfuse.session.id"] == session
    assert root.attributes["langfuse.trace.metadata.rewrite_id"] == "rw-1"
    assert {span.name for span in children} == {"planner", "executor"}
    assert all(span.parent.span_id == root.context.span_id for span in children)
    assert all(span.context.trace_id == root.context.trace_id for span in children)
    assert all(
        span.attributes["langfuse.trace.name"] == root.name for span in children
    )
    assert all(span.attributes["langfuse.session.id"] == session for span in children)


def test_orphan_parse_is_dropped(monkeypatch):
    exporter = MemoryExporter()
    install_span_exporter(exporter)
    client = _client()
    monkeypatch.setattr(
        client._client.beta.chat.completions, "parse", lambda **_kwargs: _ok_response()
    )
    client.parse(
        messages=[{"role": "user", "content": SECRET}],
        text_format=Out,
        label="experience[Role @ Co]",
    )
    assert exporter.get_finished_spans() == []


def test_langfuse_host_uses_the_otel_path():
    assert traces_endpoint("https://us.cloud.langfuse.com") == (
        "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    )
    assert traces_endpoint("https://us.cloud.langfuse.com/api/public/otel") == (
        "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    )
    assert traces_endpoint("http://localhost:4318") == "http://localhost:4318/v1/traces"


def test_raw_langfuse_basic_token_is_encoded():
    import base64

    headers = otlp_headers("Authorization=Basic pk-lf-abc:sk-lf-def")
    assert headers is not None
    scheme, token = headers["Authorization"].split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == "pk-lf-abc:sk-lf-def"
    already = base64.b64encode(b"pk-lf-abc:sk-lf-def").decode()
    assert otlp_headers(f"Authorization=Basic {already}")["Authorization"] == (
        f"Basic {already}"
    )


def test_disabled_records_nothing(monkeypatch):
    from app.agent.tracing import shutdown_tracing

    shutdown_tracing()
    monkeypatch.setattr("app.config.OTEL_ENDPOINT", "")
    setup_tracing()
    assert tracing_enabled() is False
    exporter = MemoryExporter()
    client = _client()
    monkeypatch.setattr(
        client._client.beta.chat.completions, "parse", lambda **_kwargs: _ok_response()
    )
    with pipeline_span("run-3", "manual"):
        client.parse(
            messages=[{"role": "user", "content": SECRET}],
            text_format=Out,
            label="checker",
        )
    assert exporter.get_finished_spans() == []
