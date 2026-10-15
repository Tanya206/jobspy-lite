"""Frozen-job evaluation for the resume rewrite loop.

``python -m app.eval.rewrite --export`` writes ``data/eval/golden.json``.
``python -m app.eval.rewrite`` scores that frozen set and does not update jobs.
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.agent.hallucination import judge_claims, scan_patch
from app.agent.resume_io import apply_patches_to_docx, load_resume_docx, paragraph_count
from app.agent.resume_rewrite import make_client, prompt_for, run_loop, score_text

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.]{3,}")


def eval_dir() -> Path:
    from app.storage import DB_PATH

    path = DB_PATH.parent / "eval"
    path.mkdir(parents=True, exist_ok=True)
    return path


def select_golden(jobs: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, Any]]:
    """Up to ``limit`` jobs that have a description, spread across verdicts."""
    described = [job for job in jobs if (job.get("description") or "").strip()]
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "strong": [],
        "moderate": [],
        "weak": [],
        "other": [],
    }
    for job in described:
        verdict = (job.get("verdict") or "").lower()
        if "strong" in verdict:
            buckets["strong"].append(job)
        elif "moderate" in verdict or "good" in verdict:
            buckets["moderate"].append(job)
        elif "weak" in verdict or "poor" in verdict:
            buckets["weak"].append(job)
        else:
            buckets["other"].append(job)
    chosen: List[Dict[str, Any]] = []
    names = ("strong", "moderate", "weak", "other")
    while len(chosen) < limit and any(buckets[name] for name in names):
        for name in names:
            if buckets[name] and len(chosen) < limit:
                chosen.append(buckets[name].pop(0))
    return chosen


def export_golden(limit: int = 30) -> Path:
    """Freeze stored jobs plus the resume text so a later upload cannot change the set."""
    from app import storage

    folder = eval_dir()
    chosen = select_golden(storage.list_jobs(limit=None), limit)
    rows = [
        {
            "job_key": job.get("job_key"),
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "description": job.get("description"),
            "score": job.get("score"),
            "verdict": job.get("verdict"),
        }
        for job in chosen
    ]
    path = folder / "golden.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    resume = storage.get_setting("resume") or {}
    (folder / "resume.txt").write_text(resume.get("text") or "", encoding="utf-8")
    (folder / "additional_info.txt").write_text(
        storage.get_setting("additional_info") or "", encoding="utf-8"
    )
    docx = load_resume_docx()
    if docx:
        (folder / "resume.docx").write_bytes(docx)
    return path


def repeated_jd_terms(description: str) -> List[str]:
    counts: Dict[str, int] = {}
    for token in _WORD.findall(description or ""):
        folded = token.casefold()
        counts[folded] = counts.get(folded, 0) + 1
    return [term for term, count in counts.items() if count > 1]


def audit_patches(
    patches: List[Dict[str, str]],
    resume_text: str,
    additional_info: str,
    checker: Optional[Callable[..., Any]] = None,
) -> List[Dict[str, Any]]:
    """Deterministic scan, plus the checker model when one is supplied."""
    allowed = f"{resume_text}\n{additional_info or ''}"
    findings: List[Dict[str, Any]] = []
    for patch in patches:
        original = patch.get("original") or ""
        replacement = patch.get("replacement") or ""
        for miss in scan_patch(original, replacement, allowed):
            findings.append({**miss, "original": original, "replacement": replacement})
        if checker is not None:
            for claim in judge_claims(
                original, replacement, resume_text, additional_info, checker
            ):
                findings.append({**claim, "original": original, "replacement": replacement})
    return findings


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def summarize(records: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    lifts = [row["lift"] for row in records if row.get("lift") is not None]
    beats = [1 for row in records if row.get("beats_original")]
    clean_jobs = [1 for row in records if row.get("clean")]
    patch_proposed = sum(row.get("proposed") or 0 for row in records)
    patch_matched = sum(row.get("accepted") or 0 for row in records)
    clean_patches = sum(row.get("clean_patches") or 0 for row in records)
    patch_total = sum(row.get("patch_count") or 0 for row in records)
    kinds: Dict[str, int] = {}
    for row in records:
        for finding in row.get("findings") or []:
            kind = finding.get("kind") or "unknown"
            kinds[kind] = kinds.get(kind, 0) + 1
    format_rows = [row for row in records if row.get("paragraphs_unchanged") is not None]
    keyword_hit = sum(row.get("keyword_hits") or 0 for row in records)
    keyword_total = sum(row.get("keyword_terms") or 0 for row in records)
    return {
        **meta,
        "jobs": len(records),
        "mean_lift": _mean(lifts),
        "share_winner_beats_original": _mean(beats) if records else None,
        "share_clean_jobs": _mean(clean_jobs) if records else None,
        "share_clean_patches": (clean_patches / patch_total) if patch_total else None,
        "hallucinations_by_kind": kinds,
        "patch_match_rate": (patch_matched / patch_proposed) if patch_proposed else None,
        "paragraph_stable_share": (
            _mean([1 if row["paragraphs_unchanged"] else 0 for row in format_rows])
            if format_rows
            else None
        ),
        "keyword_lift_share": (keyword_hit / keyword_total) if keyword_total else None,
        "results": records,
    }


def write_audit(path: Path, records: List[Dict[str, Any]]) -> None:
    lines = ["# Resume rewrite audit", ""]
    for row in records[:5]:
        lines.append(f"## {row.get('title') or row.get('job_key')}")
        lines.append("")
        lines.append(f"Winner total: {row.get('winning_total')}  baseline: {row.get('baseline_total')}")
        lines.append("")
        for patch in row.get("patches") or []:
            lines.append(f"- original: {patch.get('original')}")
            lines.append(f"  replacement: {patch.get('replacement')}")
        if row.get("findings"):
            lines.append("")
            lines.append("Unsupported claims:")
            for finding in row["findings"]:
                lines.append(f"- {finding.get('kind')}: {finding.get('span')}")
        lines.append("")
        lines.append("Hand check: pass / fail")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _checker_from_client(client) -> Callable[..., Any]:
    def checker(messages, text_format):
        return client.parse(
            [{"role": "system", "content": prompt_for("checker")}, *messages],
            text_format,
            label="checker",
        )

    return checker


def run_eval(
    max_tries: int = 1,
    complete: Optional[Callable[..., Any]] = None,
    checker: Optional[Callable[..., Any]] = None,
    rewrite_model: str = "",
    checker_model: str = "",
) -> Path:
    """Score the frozen set. Does not update job scores or the scoring resume."""
    from app.config import AI_BASE_URL, AI_MODEL, agent_defaults
    from app import storage

    folder = eval_dir()
    golden_path = folder / "golden.json"
    if not golden_path.is_file():
        raise SystemExit("No golden set. Run: python -m app.eval.rewrite --export")
    jobs = json.loads(golden_path.read_text(encoding="utf-8"))
    resume_text = (folder / "resume.txt").read_text(encoding="utf-8") if (folder / "resume.txt").is_file() else ""
    additional = (
        (folder / "additional_info.txt").read_text(encoding="utf-8")
        if (folder / "additional_info.txt").is_file()
        else ""
    )
    docx = (folder / "resume.docx").read_bytes() if (folder / "resume.docx").is_file() else None
    agent = {**agent_defaults(), **(storage.get_setting("agent_params") or {})}
    if complete is None:
        client = make_client("", "", agent)
        rewrite_model = client.ai_model
        def complete(messages, text_format, label):  # type: ignore[no-redef]
            return client.parse(messages, text_format, label=label)
    if checker is None:
        checker_model = os.environ.get("CHECKER_MODEL") or AI_MODEL
        checker_base = os.environ.get("CHECKER_BASE_URL") or AI_BASE_URL
        from app.agent.ai_client import AIClient

        checker_client = AIClient(
            {
                "ai_model": checker_model,
                "ai_base_url": checker_base,
                "api_key": os.environ.get("NVIDIA_API_KEY"),
                "timeout_seconds": 90,
                "rate_limited": "api.nvidia.com" in checker_base,
            }
        )
        checker = _checker_from_client(checker_client)
    records = []
    for job in jobs:
        baseline = score_text(resume_text, complete)
        result = run_loop(
            resume_text=resume_text,
            job=job,
            additional_info=additional,
            max_tries=max_tries,
            complete=complete,
            resume_docx=docx,
        )
        winner = next((row for row in result["trace"] if row.get("try") == result.get("winner_try")), None)
        patches = (winner or {}).get("patches") or []
        per_patch = [
            audit_patches([patch], resume_text, additional, checker) for patch in patches
        ]
        findings = [item for group in per_patch for item in group]
        terms = repeated_jd_terms(job.get("description") or "")
        winning_text = result.get("text") or resume_text
        hits = [
            term
            for term in terms
            if term not in resume_text.casefold() and term in winning_text.casefold()
        ]
        paragraphs = None
        if docx is not None and result.get("patch_chain"):
            patched, _applied = apply_patches_to_docx(docx, result["patch_chain"])
            paragraphs = paragraph_count(docx) == paragraph_count(patched)
        proposed = sum(row.get("proposed") or 0 for row in result["trace"])
        accepted = sum(row.get("accepted") or 0 for row in result["trace"])
        winning_total = result.get("winning_total")
        baseline_total = baseline.total
        records.append(
            {
                "job_key": job.get("job_key"),
                "title": job.get("title"),
                "baseline_total": baseline_total,
                "winning_total": winning_total,
                "lift": None if winning_total is None else winning_total - baseline_total,
                "beats_original": bool(winning_total is not None and winning_total > baseline_total),
                "patches": patches,
                "findings": findings,
                "clean": not findings,
                "clean_patches": sum(1 for group in per_patch if not group),
                "patch_count": len(patches),
                "proposed": proposed,
                "accepted": accepted,
                "paragraphs_unchanged": paragraphs,
                "keyword_terms": len(terms),
                "keyword_hits": len(hits),
            }
        )
    meta = {
        "rewrite_model": rewrite_model or agent.get("rewrite_model") or AI_MODEL,
        "checker_model": checker_model or os.environ.get("CHECKER_MODEL") or AI_MODEL,
        "max_tries": max_tries,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    summary = summarize(records, meta)
    runs = folder / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = runs / f"{stamp}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_audit(folder / "audit.md", records)
    return out


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Export or score the resume-rewrite golden set.")
    parser.add_argument("--export", action="store_true", help="Freeze stored jobs into data/eval.")
    parser.add_argument("--max-tries", type=int, default=1)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)
    if args.export:
        print(export_golden(args.limit))
        return
    print(run_eval(max_tries=args.max_tries))


if __name__ == "__main__":
    main()
