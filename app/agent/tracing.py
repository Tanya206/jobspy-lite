"""OpenTelemetry traces for pipeline runs and resume rewrites.

Export is off unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. There are two
traces: one ``jobspy.pipeline`` root per run, and one ``jobspy.rewrite`` root
per resume rewrite. A ``gen_ai.chat`` span is a child of whichever root is
open. It is not its own trace, and a call with no open root is dropped.

Run totals live only on the pipeline root. Model, agent, attempts, HTTP
status, and token counts live only on the chat span. Prompts, completions,
and resume text are never attributes.
"""

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_provider: Any = None
_enabled = False

_KNOWN_AGENTS = frozenset({"experience", "fit", "rewrite", "checker"})
_TEXT_LIMIT = 300


def agent_from_label(label: str) -> str:
    """Map a parse label to an agent name. Anything else is ``other``."""
    prefix = (label or "").split("[", 1)[0].strip().lower()
    if prefix in _KNOWN_AGENTS:
        return prefix
    return "other"


def tracing_enabled() -> bool:
    return _enabled


def setup_tracing() -> None:
    """Start the OTLP HTTP exporter. No-op when unset or already started."""
    global _provider, _enabled
    with _LOCK:
        if _provider is not None:
            return
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if not endpoint:
            return
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            # The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT and appends
            # /v1/traces, and reads OTEL_EXPORTER_OTLP_HEADERS for auth.
            provider = TracerProvider(
                resource=Resource.create({"service.name": "jobspy-lite"})
            )
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            _provider = provider
            _enabled = True
            logger.info("OpenTelemetry export enabled (endpoint %s)", endpoint)
        except Exception:
            logger.exception(
                "OpenTelemetry setup failed; traces will not be exported"
            )


def install_span_exporter(exporter: Any) -> None:
    """Send ended spans straight to ``exporter``. Used by tests."""
    global _provider, _enabled
    with _LOCK:
        _shutdown_locked()
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": "jobspy-lite"})
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        _provider = provider
        _enabled = True


def shutdown_tracing() -> None:
    """Flush and stop the exporter. Safe to call more than once."""
    with _LOCK:
        _shutdown_locked()


def _shutdown_locked() -> None:
    global _provider, _enabled
    provider = _provider
    _provider = None
    _enabled = False
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception:
        logger.exception("OpenTelemetry shutdown failed")


def _clip(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    return str(value)[:_TEXT_LIMIT]


def _mark_error(span: Any, message: str) -> None:
    if span is None:
        return
    from opentelemetry.trace import Status, StatusCode

    span.set_status(Status(StatusCode.ERROR, (message or "failed")[:_TEXT_LIMIT]))


@contextmanager
def _open(name: str, attributes: Dict[str, Any]) -> Iterator[Any]:
    """Start a span, or yield None when tracing is off or setup failed."""
    if not _enabled or _provider is None:
        yield None
        return
    otel_cm = None
    try:
        tracer = _provider.get_tracer("jobspy-lite")
        otel_cm = tracer.start_as_current_span(name)
        span = otel_cm.__enter__()
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, _clip(value))
    except Exception:
        logger.exception("Could not start span %s", name)
        if otel_cm is not None:
            try:
                otel_cm.__exit__(None, None, None)
            except Exception:
                logger.exception("Could not end span %s", name)
        yield None
        return
    try:
        yield span
    finally:
        try:
            otel_cm.__exit__(None, None, None)
        except Exception:
            logger.exception("Could not end span %s", name)


@contextmanager
def pipeline_span(run_id: str, trigger: str) -> Iterator[Any]:
    """Root span for one pipeline run. Yields None when tracing is off."""
    with _open("jobspy.pipeline", {"run_id": run_id, "trigger": trigger}) as span:
        yield span


def annotate_pipeline(span: Any, summary: Dict[str, Any]) -> None:
    """Set run totals on the pipeline root. Does not copy chat attributes."""
    if span is None:
        return
    try:
        for key in (
            "status",
            "jobs_scraped",
            "new_jobs",
            "scored",
            "score_failed",
            "alerts_sent",
        ):
            value = summary.get(key)
            if value is None:
                continue
            span.set_attribute(key, _clip(value))
        if summary.get("status") == "failed":
            _mark_error(span, summary.get("error") or "failed")
    except Exception:
        logger.exception("Could not annotate pipeline span")


@contextmanager
def rewrite_span(rewrite_id: str, job_key: str, max_tries: int) -> Iterator[Any]:
    """Root span for one resume rewrite. Not nested under a pipeline run."""
    with _open(
        "jobspy.rewrite",
        {
            "rewrite_id": rewrite_id,
            "job_key": job_key,
            "max_tries": int(max_tries),
        },
    ) as span:
        try:
            yield span
        except Exception as exc:
            _mark_error(span, str(exc))
            raise


def finish_rewrite(span: Any, status: str) -> None:
    """Record the rewrite row's final status on the root span."""
    if span is None:
        return
    try:
        span.set_attribute("status", status)
        if status == "error":
            _mark_error(span, "error")
    except Exception:
        logger.exception("Could not finish rewrite span")


def _parent_open() -> bool:
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    return bool(context and context.is_valid)


@contextmanager
def parse_span(label: str, model: str) -> Iterator[Any]:
    """Child chat span. Yields None when tracing is off or no root is open."""
    if not _enabled or not _parent_open():
        yield None
        return
    with _open(
        "gen_ai.chat",
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model or "",
            "jobspy.agent": agent_from_label(label),
            "jobspy.label": label or "",
        },
    ) as span:
        yield span


def note_parse_attempt(
    span: Any,
    attempt: int,
    http_status: Optional[int],
    error: Optional[str],
) -> None:
    """One failed HTTP attempt inside the chat span. Not a new span."""
    if span is None:
        return
    try:
        attributes: Dict[str, Any] = {"attempt": int(attempt)}
        if http_status is not None:
            attributes["http.response.status_code"] = int(http_status)
        if error:
            attributes["exception.message"] = str(error)[:_TEXT_LIMIT]
        span.add_event("attempt.failed", attributes=attributes)
    except Exception:
        logger.exception("Could not record a failed model attempt")


def finish_parse(
    span: Any,
    *,
    ok: bool,
    attempts: int,
    http_status: Optional[int] = None,
    usage: Any = None,
    error: Optional[str] = None,
) -> None:
    """Close out one logical parse(): attempts, last HTTP status, tokens."""
    if span is None:
        return
    try:
        span.set_attribute("jobspy.attempts", int(attempts))
        if http_status is not None:
            span.set_attribute("http.response.status_code", int(http_status))
        _set_usage(span, usage)
        if not ok:
            _mark_error(span, error or "failed")
    except Exception:
        logger.exception("Could not finish parse span")


def _set_usage(span: Any, usage: Any) -> None:
    if usage is None:
        return
    input_tokens = getattr(usage, "prompt_tokens", None)
    if input_tokens is None:
        input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None)
    if output_tokens is None:
        output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is not None:
        span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
    if output_tokens is not None:
        span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
