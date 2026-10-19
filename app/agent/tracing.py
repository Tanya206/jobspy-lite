"""OpenTelemetry traces for pipeline runs and per-job agent work.

Export is off unless ``tracing.otel_endpoint`` is set in config.yaml.

Each job is one Langfuse session (see ``job_session_id``). A session holds
three kinds of trace, one per agent step:

* ``Experience Match``: required-years estimate vs. the experience window.
* ``Resume Match``: resume suitability score for the job.
* ``Resume Rewrite``: the planner/executor/checker rewrite loop.

Each pipeline run also gets a ``pipeline · …`` trace with run totals only.
Job traces are separate roots and carry the run id as metadata. Model
calls are children of whichever job trace is open. A call with no open
root is dropped. Langfuse gets ``langfuse.trace.name`` and
``langfuse.session.id`` on every span so the steps stay under one header.

Run totals live only on the pipeline root. Model, agent, attempts, HTTP
status, and token counts live only on the chat span.

When ``tracing.capture_content`` is on (the default), chat spans carry the
request messages as input and the parsed response as output. Job roots
carry the job (plus resume text or rewrite options) as input and the
agent's result as output, at both observation and trace level. The pipeline
root never carries content.
"""

import base64
import binascii
import contextvars
import hashlib
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_provider: Any = None
_enabled = False
_capture_content = False

_KNOWN_AGENTS = frozenset({"experience", "fit", "rewrite", "checker"})
_TEXT_LIMIT = 300
_CONTENT_LIMIT = 100_000
# Langfuse drops session ids of 200 characters or more, and non-ASCII ones.
_SESSION_TITLE_LIMIT = 120

EXPERIENCE_MATCH = "Experience Match"
RESUME_MATCH = "Resume Match"
RESUME_REWRITE = "Resume Rewrite"

# Copied onto every span opened while a job trace is active, so Langfuse
# shows one named trace instead of one row per model call.
_langfuse_trace: contextvars.ContextVar[Optional[Dict[str, str]]] = contextvars.ContextVar(
    "jobspy_langfuse_trace", default=None
)
_pipeline_run: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "jobspy_pipeline_run", default=None
)


def agent_from_label(label: str) -> str:
    """Map a parse label to an agent name. Anything else is ``other``."""
    prefix = (label or "").split("[", 1)[0].strip().lower()
    if prefix in _KNOWN_AGENTS:
        return prefix
    return "other"


def tracing_enabled() -> bool:
    return _enabled


def job_title(job: Optional[Dict[str, Any]]) -> str:
    """``Role @ Company`` for a job dict, skipping empty parts."""
    job = job or {}
    return " @ ".join(part for part in (job.get("title"), job.get("company")) if part)


def job_session_id(job_key: str, title: str = "") -> str:
    """Langfuse session id for one job. Same job key, same session.

    Job keys are usually long URLs, so the id is the readable title plus a
    short hash of the key.
    """
    digest = hashlib.sha256((job_key or "").encode("utf-8")).hexdigest()[:10]
    readable = re.sub(r"[^A-Za-z0-9@&.,()/ _-]+", "", title or "")
    readable = re.sub(r"\s+", " ", readable).strip()[:_SESSION_TITLE_LIMIT].strip()
    return f"{readable} #{digest}" if readable else f"job #{digest}"


def traces_endpoint(endpoint: str) -> str:
    """Full OTLP traces URL. Langfuse's collector is under ``/api/public/otel``.

    A host-only value such as ``https://us.cloud.langfuse.com`` would otherwise
    be posted to ``/v1/traces``, which Langfuse answers with 404.
    """
    base = endpoint.strip().rstrip("/")
    lowered = base.lower()
    if (
        "langfuse" in lowered
        and "/api/public/otel" not in lowered
        and not lowered.endswith("/v1/traces")
    ):
        base = base + "/api/public/otel"
    if base.endswith("/v1/traces"):
        return base
    return base + "/v1/traces"


def _is_base64(token: str) -> bool:
    try:
        base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def otlp_headers(raw: str) -> Optional[Dict[str, str]]:
    """Authorization header for the exporter.

    Langfuse wants ``Basic`` plus the base64 of ``public_key:secret_key``.
    A raw ``pk-...:sk-...`` token is encoded here. ``None`` leaves header
    parsing to the SDK.
    """
    match = re.search(r"(?i)authorization=Basic\s+(\S+)", raw or "")
    if not match:
        return None
    token = match.group(1).strip().strip('"').strip("'")
    if _is_base64(token):
        encoded = token
        rewritten = False
    elif ":" in token:
        encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
        rewritten = True
    else:
        return None
    if rewritten:
        logger.warning(
            "OTLP Basic token is not base64; encoding public_key:secret_key before export."
        )
    return {"Authorization": f"Basic {encoded}"}


def setup_tracing() -> None:
    """Start the OTLP HTTP exporter. No-op when unset or already started."""
    global _provider, _enabled, _capture_content
    with _LOCK:
        if _provider is not None:
            return
        from ..config import OTEL_CAPTURE_CONTENT, OTEL_ENDPOINT

        endpoint = (OTEL_ENDPOINT or "").strip()
        if not endpoint:
            return
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            # Pass the full signal URL. The exporter only appends /v1/traces
            # when it reads OTEL_EXPORTER_OTLP_ENDPOINT itself.
            url = traces_endpoint(endpoint)
            headers = otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", ""))
            provider = TracerProvider(
                resource=Resource.create({"service.name": "jobspy-lite"})
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=url, headers=headers))
            )
            _provider = provider
            _enabled = True
            _capture_content = bool(OTEL_CAPTURE_CONTENT)
            logger.info(
                "OpenTelemetry export enabled (%s, content %s)",
                url,
                "on" if _capture_content else "off",
            )
        except Exception:
            logger.exception(
                "OpenTelemetry setup failed; traces will not be exported"
            )


def install_span_exporter(exporter: Any, capture_content: bool = True) -> None:
    """Send ended spans straight to ``exporter``. Used by tests."""
    global _provider, _enabled, _capture_content
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
        _capture_content = capture_content


def shutdown_tracing() -> None:
    """Flush and stop the exporter. Safe to call more than once."""
    with _LOCK:
        _shutdown_locked()


def _shutdown_locked() -> None:
    global _provider, _enabled, _capture_content
    provider = _provider
    _provider = None
    _enabled = False
    _capture_content = False
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


def _json_default(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return str(value)


def _content(value: Any) -> str:
    """JSON text for an input/output attribute, cut at ``_CONTENT_LIMIT``."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, default=_json_default, ensure_ascii=False)
    if len(text) > _CONTENT_LIMIT:
        text = text[:_CONTENT_LIMIT] + "…[truncated]"
    return text


def _set_content(span: Any, kind: str, value: Any, trace_level: bool) -> None:
    """Set ``kind`` (input or output) on the span. No-op when capture is off."""
    if span is None or not _capture_content or value is None:
        return
    try:
        text = _content(value)
        span.set_attribute(f"langfuse.observation.{kind}", text)
        if trace_level:
            span.set_attribute(f"langfuse.trace.{kind}", text)
    except Exception:
        logger.exception("Could not record span %s", kind)


def record_output(span: Any, value: Any) -> None:
    """Result of a job trace. Shown as the trace output in Langfuse."""
    _set_content(span, "output", value, trace_level=True)


def _mark_error(span: Any, message: str) -> None:
    if span is None:
        return
    from opentelemetry.trace import Status, StatusCode

    span.set_status(Status(StatusCode.ERROR, (message or "failed")[:_TEXT_LIMIT]))


@contextmanager
def _open(name: str, attributes: Dict[str, Any], root: bool = False) -> Iterator[Any]:
    """Start a span, or yield None when tracing is off or setup failed.

    ``root`` starts a new trace even when another span is current.
    """
    if not _enabled or _provider is None:
        yield None
        return
    otel_cm = None
    try:
        tracer = _provider.get_tracer("jobspy-lite")
        if root:
            from opentelemetry.context import Context

            otel_cm = tracer.start_as_current_span(name, context=Context())
        else:
            otel_cm = tracer.start_as_current_span(name)
        span = otel_cm.__enter__()
        merged = _with_trace_attributes(attributes)
        for key, value in merged.items():
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


def _with_trace_attributes(attributes: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp the open Langfuse trace name and session onto this span."""
    current = _langfuse_trace.get()
    if not current:
        return attributes
    merged = dict(attributes)
    merged.setdefault("langfuse.trace.name", current["name"])
    merged.setdefault("langfuse.session.id", current["session_id"])
    return merged


@contextmanager
def _trace_header(name: str, session_id: str) -> Iterator[None]:
    token = _langfuse_trace.set({"name": name, "session_id": session_id})
    try:
        yield
    finally:
        _langfuse_trace.reset(token)


@contextmanager
def pipeline_span(run_id: str, trigger: str) -> Iterator[Any]:
    """Root span for one pipeline run. Yields None when tracing is off.

    Job traces opened during the run are separate roots that record
    ``run_id`` as metadata, so this trace holds run totals only.
    """
    header = f"pipeline · {trigger}"
    token = _pipeline_run.set(run_id)
    try:
        with _open(
            header,
            {
                "run_id": run_id,
                "trigger": trigger,
                "langfuse.trace.name": header,
                "langfuse.observation.type": "span",
            },
            root=True,
        ) as span:
            yield span
    finally:
        _pipeline_run.reset(token)


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
def _job_trace(
    name: str,
    job_key: str,
    title: str,
    attributes: Dict[str, Any],
    inputs: Any = None,
) -> Iterator[Any]:
    """New root trace ``name`` in the job's session. Errors mark the root."""
    session_id = job_session_id(job_key, title)
    merged: Dict[str, Any] = {
        "job_key": job_key,
        "langfuse.trace.name": name,
        "langfuse.session.id": session_id,
        "langfuse.observation.type": "span",
        "langfuse.trace.metadata.job_key": job_key,
        "langfuse.trace.metadata.job_title": title or None,
        "langfuse.trace.metadata.run_id": _pipeline_run.get(),
    }
    merged.update(attributes)
    with _trace_header(name, session_id):
        with _open(name, merged, root=True) as span:
            _set_content(span, "input", inputs, trace_level=True)
            try:
                yield span
            except Exception as exc:
                _mark_error(span, str(exc))
                raise


@contextmanager
def experience_match_span(job_key: str, job: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
    """``Experience Match`` trace for one job's required-years estimate."""
    with _job_trace(
        EXPERIENCE_MATCH, job_key, job_title(job), {}, inputs={"job": job}
    ) as span:
        yield span


@contextmanager
def resume_match_span(
    job_key: str,
    job: Optional[Dict[str, Any]] = None,
    resume_text: Optional[str] = None,
) -> Iterator[Any]:
    """``Resume Match`` trace for one job's suitability score."""
    with _job_trace(
        RESUME_MATCH,
        job_key,
        job_title(job),
        {},
        inputs={"job": job, "resume_text": resume_text},
    ) as span:
        yield span


@contextmanager
def rewrite_span(
    rewrite_id: str,
    job_key: str,
    max_tries: int,
    title: str = "",
    inputs: Optional[Dict[str, Any]] = None,
) -> Iterator[Any]:
    """``Resume Rewrite`` trace for one rewrite. Model calls are children of it.

    ``title`` is usually ``Role @ Company``. It shares the job's session
    with that job's match traces, and later rewrites group there too.
    ``inputs`` is what the loop was given (job, resume text, options).
    """
    with _job_trace(
        RESUME_REWRITE,
        job_key,
        title.strip(),
        {
            "rewrite_id": rewrite_id,
            "max_tries": int(max_tries),
            "langfuse.trace.metadata.rewrite_id": rewrite_id,
        },
        inputs=inputs,
    ) as span:
        yield span


def finish_rewrite(span: Any, status: str, output: Any = None) -> None:
    """Record the rewrite row's final status and result on the root span."""
    if span is None:
        return
    record_output(span, output)
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
def parse_span(label: str, model: str, messages: Any = None) -> Iterator[Any]:
    """Child chat span. Yields None when tracing is off or no root is open.

    ``messages`` is the request, recorded as the generation input.
    """
    if not _enabled or not _parent_open():
        yield None
        return
    with _open(
        label or "gen_ai.chat",
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model or "",
            "jobspy.agent": agent_from_label(label),
            "jobspy.label": label or "",
            "langfuse.observation.type": "generation",
        },
    ) as span:
        _set_content(span, "input", messages, trace_level=False)
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
    output: Any = None,
) -> None:
    """Close out one logical parse(): attempts, last HTTP status, tokens, output."""
    if span is None:
        return
    _set_content(span, "output", output, trace_level=False)
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
