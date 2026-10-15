"""Hallucination checks for resume patches.

The job description is never a source of facts about the candidate.
"""

import re
from typing import Any, Dict, List, Optional

from .resume_rewrite_schema import HallucinationCheck

_NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#]{2,}(?:\.[A-Za-z0-9+#]+)*")
_STOP = {
    "the", "and", "for", "with", "from", "this", "that", "into", "over",
    "using", "built", "lead", "led", "team", "role", "work", "years",
    "experience", "including", "across", "their", "have", "been", "were",
    "was", "are", "our", "his", "her", "its", "not", "but", "also",
}


def _numbers(text: str) -> List[str]:
    return _NUMBER.findall(text or "")


def scan_patch(original: str, replacement: str, allowed: str) -> List[Dict[str, str]]:
    """Flag new numbers and proper-noun tokens that the sources do not contain."""
    allowed_cf = (allowed or "").casefold()
    original_cf = (original or "").casefold()
    misses: List[Dict[str, str]] = []
    for number in _numbers(replacement):
        if number in (original or ""):
            continue
        if number.casefold() not in allowed_cf:
            misses.append({"kind": "invented_metric", "span": number})
    for token in _TOKEN.findall(replacement or ""):
        folded = token.casefold()
        if folded in _STOP or folded in original_cf or folded in allowed_cf:
            continue
        if not token[0].isupper():
            continue
        misses.append({"kind": "invented_tool", "span": token})
    return misses


def grounded(original: str, replacement: str, allowed: str) -> bool:
    return not scan_patch(original, replacement, allowed)


def judge_claims(
    original: str,
    replacement: str,
    resume_text: str,
    additional_info: str,
    checker,
) -> List[Dict[str, Any]]:
    """Ask a separate model whether each new claim is supported.

    ``checker`` is a callable ``(messages, text_format) -> HallucinationCheck``.
    The job description is not included.
    """
    messages = [
        {
            "role": "user",
            "content": (
                "Original span:\n"
                f"{original}\n\nReplacement:\n{replacement}\n\n"
                "Resume:\n"
                f"{resume_text}\n\nAdditional info:\n{additional_info or '(none)'}"
            ),
        }
    ]
    result = checker(messages, HallucinationCheck)
    if not isinstance(result, HallucinationCheck):
        result = HallucinationCheck.model_validate(result)
    unsupported = []
    for claim in result.claims:
        if not claim.supported:
            unsupported.append(
                {
                    "kind": "unsupported_claim",
                    "span": claim.claim,
                    "source_sentence": claim.source_sentence,
                }
            )
    return unsupported
