"""Shared pydantic models for violations, fixes, and review jobs."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Layer(str, Enum):
    SPELL = "spell"
    LINT = "lint"
    SECURITY = "security"
    HARDCODE = "hardcode"
    LLM = "llm"
    GUIDELINE = "guideline"   # Phase 2: RAG violation against an uploaded rule


class Citation(BaseModel):
    """The guideline rule a GUIDELINE-layer violation was checked against."""
    collection: str = ""       # collection id
    collection_name: str = ""
    source: str = ""           # document filename
    page: Optional[int] = None
    quote: str = ""            # the retrieved rule text (trimmed)


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Fix(BaseModel):
    start_line: int
    end_line: int
    replacement: str
    # validation trail — shown in UI so the user can trust the fix
    validated: bool = False
    validation_notes: str = ""
    file_wide: bool = False  # e.g. identifier renames touch multiple lines
    fixed_code: Optional[str] = None  # full patched file when file_wide


class Violation(BaseModel):
    id: str
    layer: Layer
    rule: str                      # e.g. "F841", "B105", "spell", "magic-number", "logic_bug"
    severity: Severity
    line: int
    col: Optional[int] = None
    snippet: str = ""
    message: str
    suggestion: str = ""
    function: str = ""   # enclosing function/method name, for UI grouping
    fix: Optional[Fix] = None
    # provenance for trust: deterministic layers are marked verified by construction
    verified: bool = True
    verification_note: str = ""
    no_autofix: bool = False   # fix engine must not generate a fix (e.g. would break build)
    # when no validated fix could be produced: which gate rejected each attempt
    # (diagnosis for fix coverage — shown in UI and accuracy reports)
    fix_notes: str = ""
    # GUIDELINE layer only: the uploaded rule this violation was judged against
    citation: Optional[Citation] = None


class LayerStatus(BaseModel):
    name: str
    state: str = "pending"         # pending | running | done | skipped | error
    detail: str = ""
    found: int = 0


class ReviewJob(BaseModel):
    job_id: str
    state: str = "queued"          # queued | running | done | error
    filename: str = ""
    language: str = ""             # resolved display name
    requested_language: str = ""   # explicit UI selection (authoritative)
    code: str = ""
    layers: list[LayerStatus] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    error: str = ""
    llm_available: bool = True
    notice: str = ""     # non-fatal advisory shown to the user (e.g. generated file)
    stats: dict = Field(default_factory=dict)
    # ---- Phase 2 (RAG) — additive; empty/off means pure Phase 1 review ----
    user: str = ""                 # owner, for scoping private collections
    rag_enabled: bool = False      # the review-page toggle
    collection_ids: list[str] = Field(default_factory=list)  # selected collections
