"""Core normalized records: ``Requirement`` and ``CodeUnit``.

Per the design spec (§2), everything downstream of ingestion depends on
exactly these two Pydantic models plus the per-corpus ``project.yaml``
manifest (see ``core/manifest.py``). Format- and corpus-specific handling
(regexes, globs, module names) lives in the manifest, never here — these
models must stay usable for a different corpus with only a new manifest.

Both models are strict about unknown fields (``extra="forbid"``) so a typo
in a caller fails loudly instead of silently dropping data.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DocType = Literal["requirement", "context"]
CodeUnitKind = Literal["function", "struct", "enum", "typedef", "macro"]
AnnotationMarker = Literal["@", "!"]
AnnotationClaim = Literal["claimed_implemented", "claimed_not_implemented"]


class Requirement(BaseModel):
    """A single normalized requirement (or context chunk) from an SWS document.

    ``id`` preserves the document's own spelling exactly (``SWS_Can_00011``
    in the CAN Driver doc, ``SWS_CANIF_00001`` in the CAN Interface doc) —
    citations must render verbatim. Use :meth:`canonical_id` to normalize an
    id for case-insensitive lookup; it never mutates ``id`` itself.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str | None = None
    text: str = Field(min_length=1)
    section_path: str | None = None
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float] | None = None
    char_span: tuple[int, int] | None = None
    source_doc: str = Field(min_length=1)
    upstream_ids: list[str] = Field(default_factory=list)
    named_symbols: list[str] = Field(default_factory=list)
    version: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    doc_type: DocType = "requirement"

    @field_validator("char_span")
    @classmethod
    def _char_span_ordered(cls, v: tuple[int, int] | None) -> tuple[int, int] | None:
        if v is not None:
            start, end = v
            if start < 0 or end < start:
                raise ValueError(f"char_span must satisfy 0 <= start <= end, got {v}")
        return v

    @field_validator("bbox")
    @classmethod
    def _bbox_ordered(
        cls, v: tuple[float, float, float, float] | None
    ) -> tuple[float, float, float, float] | None:
        if v is not None:
            x0, y0, x1, y1 = v
            if x1 < x0 or y1 < y0:
                raise ValueError(f"bbox must satisfy x0 <= x1 and y0 <= y1, got {v}")
        return v

    @classmethod
    def canonical_id(cls, raw_id: str) -> str:
        """Upper-case ``raw_id`` for case-insensitive matching.

        Does not affect ``id``, which always preserves the document's own
        spelling for citation rendering.
        """
        return raw_id.strip().upper()


class ReqAnnotation(BaseModel):
    """One ``@req``/``!req`` annotation parsed from a code comment.

    The code uses two markers with opposite meanings: ``@req`` claims the
    requirement *is* implemented, ``!req`` explicitly claims it is *not*.
    Both are recorded — collapsing them would mis-state real evidence.
    """

    model_config = ConfigDict(extra="forbid")

    canonical_id: str = Field(min_length=1)
    raw: str = Field(min_length=1)
    marker: AnnotationMarker
    claim: AnnotationClaim
    line: int = Field(ge=1)

    @model_validator(mode="after")
    def _marker_claim_agree(self) -> ReqAnnotation:
        expected = {"@": "claimed_implemented", "!": "claimed_not_implemented"}[self.marker]
        if self.claim != expected:
            raise ValueError(
                f"marker {self.marker!r} implies claim {expected!r}, got {self.claim!r}"
            )
        return self


class CodeUnit(BaseModel):
    """A single AST-level chunk (function/struct/enum/typedef/macro) of C source."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str = Field(min_length=1)
    language: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    kind: CodeUnitKind
    line_span: tuple[int, int]
    text: str
    req_annotations: list[ReqAnnotation] = Field(default_factory=list)
    git_sha: str
    project_id: str = Field(min_length=1)

    @field_validator("line_span")
    @classmethod
    def _line_span_ordered(cls, v: tuple[int, int]) -> tuple[int, int]:
        start, end = v
        if start < 1 or end < start:
            raise ValueError(f"line_span must satisfy 1 <= start <= end, got {v}")
        return v

    @field_validator("git_sha")
    @classmethod
    def _git_sha_hex40(cls, v: str) -> str:
        if len(v) != 40 or any(c not in "0123456789abcdef" for c in v.lower()):
            raise ValueError(f"git_sha must be a 40-character hex SHA, got {v!r}")
        return v

    @property
    def claimed_implemented_ids(self) -> list[str]:
        """Canonical IDs this code unit claims to implement (``@req`` only).

        This is what tier-1 evidence consumes — ``!req`` annotations are
        excluded since they explicitly claim non-implementation.
        """
        seen: list[str] = []
        for ann in self.req_annotations:
            if ann.claim == "claimed_implemented" and ann.canonical_id not in seen:
                seen.append(ann.canonical_id)
        return seen
