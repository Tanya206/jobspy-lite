"""Agentic resume rewrite: plan, patch, score, rewrite, then score again.

Scoring of jobs stays on the pipeline model. This loop uses its own client
and never replaces the stored scoring resume.
"""

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .ai_client import AIClient
from .hallucination import grounded
from .resume_rewrite_schema import (
    AtsScore,
    EditPlan,
    ExecutorOutput,
    RewriterOutput,
    TextPatch,
)

_PROMPT_PATH = Path(__file__).resolve().parent / "Prompt" / "resume_rewrite.md"

_STEP_LABELS = (
    "planner",
    "executor",
    "ats",
    "rewriter",
    "planner-2",
    "executor-2",
    "ats-2",
)


def load_sections() -> Dict[str, str]:
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    parts: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        if line.startswith("# "):
            current = line[2:].strip().lower()
            parts[current] = []
        elif current is not None:
            parts[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in parts.items()}


def prompt_for(step: str) -> str:
    sections = load_sections()
    shared = sections.get("shared rules", "")
    body = sections.get(step, "")
    return f"{shared}\n\n{body}".strip()


def apply_text(text: str, patches: Sequence[Dict[str, str]]) -> str:
    updated = text
    for patch in patches:
        original = patch.get("original") or ""
        if original and updated.count(original) == 1:
            updated = updated.replace(original, patch.get("replacement") or "", 1)
    return updated


def _as_patches(value: Any) -> List[TextPatch]:
    raw = getattr(value, "patches", None) or []
    patches: List[TextPatch] = []
    for item in raw:
        if isinstance(item, TextPatch):
            patches.append(item)
        else:
            patches.append(TextPatch.model_validate(item))
    return patches


def accept_patches(
    current: str, patches: Sequence[TextPatch], allowed: str
) -> Dict[str, List[Dict[str, str]]]:
    """Keep a patch only when its original span is unique and grounded."""
    kept: List[Dict[str, str]] = []
    dropped: List[Dict[str, str]] = []
    used: set = set()
    for patch in patches:
        original = patch.original or ""
        replacement = patch.replacement or ""
        row = {"original": original, "replacement": replacement}
        if original in used or not original or current.count(original) != 1:
            dropped.append({**row, "reason": "not_unique"})
            continue
        if not grounded(original, replacement, allowed):
            dropped.append({**row, "reason": "ungrounded"})
            continue
        kept.append(row)
        used.add(original)
    return {"kept": kept, "dropped": dropped}


def score_payload(score: AtsScore) -> Dict[str, Any]:
    data = score.model_dump()
    data["total"] = score.total
    return data


def _context_block(
    *,
    resume_text: str,
    job: Dict[str, Any],
    additional_info: str,
    years: str,
    industry: str,
    company_type: str,
) -> str:
    return (
        f"Job title: {job.get('title') or ''}\n"
        f"Company: {job.get('company') or ''}\n"
        f"Company type: {company_type or '(not specified)'}\n"
        f"Years band: {years or '(not specified)'}\n"
        f"Industry: {industry or '(not specified)'}\n\n"
        "Additional info (facts the candidate supplied beyond the resume):\n"
        f"{additional_info or '(none)'}\n\n"
        "Job description (not a source of facts about the candidate):\n"
        f"{job.get('description') or ''}\n\n"
        "Current resume:\n"
        f"{resume_text}"
    )


def _fit_one_page(original_docx: Optional[bytes], current_docx: Optional[bytes], accepted: Dict[str, List[Dict[str, str]]]) -> None:
    if not original_docx or not current_docx:
        return
    from .resume_io import keep_one_page

    kept, dropped = keep_one_page(original_docx, current_docx, accepted["kept"])
    accepted["kept"] = kept
    accepted["dropped"].extend(dropped)


def run_loop(
    *,
    resume_text: str,
    job: Dict[str, Any],
    additional_info: str,
    max_tries: int,
    complete: Callable[..., Any],
    years: str = "",
    industry: str = "",
    company_type: str = "",
    resume_docx: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Run up to ``max_tries`` successful passes. Keep the higher ATS total.

    ``complete(messages, text_format, label)`` is one model call. A try that
    returns unusable output does not count. Two failures in a row stop the loop.
    """
    allowed = f"{resume_text}\n{additional_info or ''}"
    current = resume_text
    current_docx = resume_docx
    chain: List[List[Dict[str, str]]] = []
    trace: List[Dict[str, Any]] = []
    best_total: Optional[int] = None
    winner_index: Optional[int] = None
    failures = 0
    calls = 0

    while sum(1 for row in trace if row.get("ok")) < max_tries:
        if failures >= 2:
            break
        start = current
        try_no = len(trace) + 1
        row: Dict[str, Any] = {
            "try": try_no,
            "ok": False,
            "total": None,
            "scores": None,
            "first_scores": None,
            "plans": [],
            "patches": [],
            "proposed": 0,
            "accepted": 0,
            "dropped": [],
            "error": None,
        }
        try:
            base = _context_block(
                resume_text=start,
                job=job,
                additional_info=additional_info,
                years=years,
                industry=industry,
                company_type=company_type,
            )
            plan_messages = [
                {"role": "system", "content": prompt_for("planner")},
                {"role": "user", "content": base},
            ]
            calls += 1
            plan = complete(plan_messages, EditPlan, _STEP_LABELS[0])
            if not isinstance(plan, EditPlan):
                plan = EditPlan.model_validate(plan)
            row["plans"].append(plan.model_dump())

            exec_messages = [
                {"role": "system", "content": prompt_for("executor")},
                {
                    "role": "user",
                    "content": base + "\n\nEdit plan:\n" + plan.model_dump_json(),
                },
            ]
            calls += 1
            executed = complete(exec_messages, ExecutorOutput, _STEP_LABELS[1])
            if not isinstance(executed, ExecutorOutput):
                executed = ExecutorOutput.model_validate(executed)
            first = accept_patches(start, _as_patches(executed), allowed)
            _fit_one_page(resume_docx, current_docx, first)
            scored_text = apply_text(start, first["kept"])

            ats_messages = [
                {"role": "system", "content": prompt_for("ats scorer")},
                {"role": "user", "content": f"Score this resume:\n{scored_text}"},
            ]
            calls += 1
            first_score = complete(ats_messages, AtsScore, _STEP_LABELS[2])
            if not isinstance(first_score, AtsScore):
                first_score = AtsScore.model_validate(first_score)
            row["first_scores"] = score_payload(first_score)

            rewrite_messages = [
                {"role": "system", "content": prompt_for("rewriter")},
                {
                    "role": "user",
                    "content": (
                        base
                        + "\n\nPatches:\n"
                        + str(first["kept"])
                        + "\n\nScore notes:\n"
                        + (first_score.notes or "")
                    ),
                },
            ]
            calls += 1
            rewritten = complete(rewrite_messages, RewriterOutput, _STEP_LABELS[3])
            if not isinstance(rewritten, RewriterOutput):
                rewritten = RewriterOutput.model_validate(rewritten)

            plan2_messages = [
                {"role": "system", "content": prompt_for("planner")},
                {
                    "role": "user",
                    "content": (
                        base
                        + "\n\nRewriter patches:\n"
                        + rewritten.model_dump_json()
                    ),
                },
            ]
            calls += 1
            plan2 = complete(plan2_messages, EditPlan, _STEP_LABELS[4])
            if not isinstance(plan2, EditPlan):
                plan2 = EditPlan.model_validate(plan2)
            row["plans"].append(plan2.model_dump())

            exec2_messages = [
                {"role": "system", "content": prompt_for("executor")},
                {
                    "role": "user",
                    "content": base
                    + "\n\nEdit plan:\n"
                    + plan2.model_dump_json(),
                },
            ]
            calls += 1
            executed2 = complete(exec2_messages, ExecutorOutput, _STEP_LABELS[5])
            if not isinstance(executed2, ExecutorOutput):
                executed2 = ExecutorOutput.model_validate(executed2)
            second = accept_patches(start, _as_patches(executed2), allowed)
            _fit_one_page(resume_docx, current_docx, second)
            final_text = apply_text(start, second["kept"])

            ats2_messages = [
                {"role": "system", "content": prompt_for("ats scorer")},
                {"role": "user", "content": f"Score this resume:\n{final_text}"},
            ]
            calls += 1
            second_score = complete(ats2_messages, AtsScore, _STEP_LABELS[6])
            if not isinstance(second_score, AtsScore):
                second_score = AtsScore.model_validate(second_score)
            total = second_score.total
            row["ok"] = True
            row["total"] = total
            row["scores"] = score_payload(second_score)
            row["patches"] = second["kept"]
            row["dropped"] = second["dropped"]
            row["proposed"] = len(_as_patches(executed2))
            row["accepted"] = len(second["kept"])
            row["keyword_summary"] = executed2.keyword_summary
            row["change_log"] = executed2.change_log
            row["weak_spots"] = executed2.weak_spots
            row["tailoring_note"] = executed2.tailoring_note
            failures = 0
            if best_total is None or total > best_total:
                best_total = total
                winner_index = try_no
                current = final_text
                chain.append(second["kept"])
                if current_docx is not None and second["kept"]:
                    from .resume_io import apply_patches_to_docx

                    current_docx, _applied = apply_patches_to_docx(current_docx, [second["kept"]])
        except Exception as exc:  # noqa: BLE001 - one bad step fails the try
            row["error"] = str(exc)
            failures += 1
        trace.append(row)

    succeeded = [item for item in trace if item.get("ok")]
    return {
        "trace": trace,
        "winning_total": best_total,
        "winner_try": winner_index,
        "patch_chain": chain,
        "text": current,
        "calls": calls,
        "error": None if succeeded else "No rewrite try succeeded.",
    }


def score_text(text: str, complete: Callable[..., Any]) -> AtsScore:
    """One ATS call, used by evaluation for the untouched resume."""
    messages = [
        {"role": "system", "content": prompt_for("ats scorer")},
        {"role": "user", "content": f"Score this resume:\n{text}"},
    ]
    score = complete(messages, AtsScore, "baseline")
    if not isinstance(score, AtsScore):
        score = AtsScore.model_validate(score)
    return score


def rewrite_target(model: str, base_url: str, agent_params: Dict[str, Any]) -> Dict[str, Any]:
    """Empty model or base URL means use the scoring model and base URL."""
    from ..config import AI_BASE_URL, AI_MODEL

    chosen_model = (model or "").strip() or str(agent_params.get("rewrite_model") or "").strip() or AI_MODEL
    chosen_base = (
        (base_url or "").strip()
        or str(agent_params.get("rewrite_base_url") or "").strip()
        or AI_BASE_URL
    )
    return {
        "model": chosen_model,
        "base_url": chosen_base,
        "rate_limited": "api.nvidia.com" in chosen_base,
        "api_key": os.environ.get("REWRITE_API_KEY") or os.environ.get("NVIDIA_API_KEY"),
    }


def make_client(model: str, base_url: str, agent_params: Dict[str, Any]) -> AIClient:
    target = rewrite_target(model, base_url, agent_params)
    return AIClient(
        {
            "ai_model": target["model"],
            "ai_base_url": target["base_url"],
            "api_key": target["api_key"],
            "timeout_seconds": 180,
            "rate_limited": target["rate_limited"],
        }
    )
