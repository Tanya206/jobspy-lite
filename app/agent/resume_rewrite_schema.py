"""Structured outputs for the resume-rewrite loop.

The model edits by patch, never by rebuilding the document.
"""

from typing import List

from pydantic import BaseModel, Field


class TextPatch(BaseModel):
    original: str = ""
    replacement: str = ""


class EditPlan(BaseModel):
    """Which existing spans to change. No replacement prose."""

    targets: List[str] = Field(default_factory=list)
    leave_alone: List[str] = Field(default_factory=list)
    keep_facts: List[str] = Field(default_factory=list)
    missing_keywords: List[str] = Field(default_factory=list)


class ExecutorOutput(BaseModel):
    patches: List[TextPatch] = Field(default_factory=list)
    keyword_summary: str = ""
    change_log: str = ""
    weak_spots: str = ""
    tailoring_note: str = ""


class AtsScore(BaseModel):
    keyword_match: int = Field(ge=0, le=10)
    quantification: int = Field(ge=0, le=10)
    action_verbs: int = Field(ge=0, le=10)
    parsability: int = Field(ge=0, le=10)
    readability: int = Field(ge=0, le=10)
    title_alignment: int = Field(ge=0, le=10)
    summary_punch: int = Field(ge=0, le=10)
    notes: str = ""

    @property
    def total(self) -> int:
        return (
            self.keyword_match
            + self.quantification
            + self.action_verbs
            + self.parsability
            + self.readability
            + self.title_alignment
            + self.summary_punch
        )


class RewriterOutput(BaseModel):
    patches: List[TextPatch] = Field(default_factory=list)


class ClaimJudgement(BaseModel):
    claim: str = ""
    supported: bool = False
    source_sentence: str = ""


class HallucinationCheck(BaseModel):
    """Checker output. The job description is not a source."""

    claims: List[ClaimJudgement] = Field(default_factory=list)
