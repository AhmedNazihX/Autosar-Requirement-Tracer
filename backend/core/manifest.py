"""Per-corpus manifest loader (``projects/<name>/project.yaml``).

Per the design spec (§2), all corpus-specific variation — document URLs,
requirement-ID regexes, the pinned repo SHA, include/exclude globs, model
IDs, cost ceilings — lives in this YAML manifest rather than in code.
Everything downstream (ingestion, retrieval, evidence engine) reads the
manifest through :func:`load_manifest`; nothing hard-codes these values.

Validation errors are wrapped as :class:`ManifestError` with the dotted
field path and offending value in the message, per story S1.2.2's
acceptance criterion — never a bare Pydantic traceback.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.models import is_git_sha


class ManifestError(Exception):
    """Raised when a project manifest fails to load or fails validation.

    The message always has the form
    ``"<manifest path>: <dotted field path> — <reason>, got <value>"`` so a
    caller (or a human reading a stack trace) can immediately see which
    field in the YAML is wrong.
    """


def _check_regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"not a valid regex ({exc})") from exc
    return value


def _check_http_url(value: str) -> str:
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError("expected an http(s) URL")
    return value


class DocumentEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str
    filename: str = Field(min_length=1)
    module: str = Field(min_length=1)
    req_id_pattern: str
    expected_requirements: int = Field(ge=0)

    @field_validator("url")
    @classmethod
    def _url_http(cls, v: str) -> str:
        return _check_http_url(v)

    @field_validator("req_id_pattern")
    @classmethod
    def _pattern_compiles(cls, v: str) -> str:
        return _check_regex(v)


class ExtractionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    req_id_pattern: str
    upstream_id_pattern: str
    symbol_pattern: str
    body_open: str = Field(min_length=1)
    body_close: str = Field(min_length=1)
    footer_patterns: list[str] = Field(default_factory=list)
    drop_markers: list[str] = Field(default_factory=list)

    @field_validator("req_id_pattern", "upstream_id_pattern", "symbol_pattern")
    @classmethod
    def _pattern_compiles(cls, v: str) -> str:
        return _check_regex(v)

    @field_validator("footer_patterns")
    @classmethod
    def _footer_patterns_compile(cls, v: list[str]) -> list[str]:
        for i, pattern in enumerate(v):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"footer_patterns[{i}] is not a valid regex ({exc})") from exc
        return v


class CodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_url: str
    git_sha: str
    license: str = Field(min_length=1)
    language: str = Field(min_length=1)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)
    annotation_pattern: str
    annotation_markers: dict[str, str] = Field(default_factory=dict)
    annotation_module_map: dict[str, str] = Field(default_factory=dict)
    annotation_id_template: str = Field(min_length=1)

    @field_validator("repo_url")
    @classmethod
    def _repo_url_http(cls, v: str) -> str:
        return _check_http_url(v)

    @field_validator("git_sha")
    @classmethod
    def _git_sha_hex40(cls, v: str) -> str:
        if not is_git_sha(v):
            raise ValueError("expected a 40-character hex SHA")
        return v

    @field_validator("annotation_pattern")
    @classmethod
    def _annotation_pattern_compiles(cls, v: str) -> str:
        return _check_regex(v)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat: str = Field(min_length=1)
    judge: str = Field(min_length=1)
    rerank: str = Field(min_length=1)
    #: Query rewriting (story S2.3.1) and self-query filter extraction (S2.4.1).
    translate: str = Field(min_length=1)
    title: str = Field(min_length=1)
    embedding: str = Field(min_length=1)


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_report_cost_usd: float = Field(gt=0)


class ProjectManifest(BaseModel):
    """The fully validated contents of a ``project.yaml`` manifest."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    documents: list[DocumentEntry] = Field(min_length=1)
    extraction: ExtractionConfig
    code: CodeConfig
    models: ModelsConfig
    limits: LimitsConfig

    # Not part of the YAML; set by load_manifest() after validation so
    # downstream code can resolve paths relative to the manifest's own
    # directory rather than the process CWD.
    manifest_dir: Path | None = Field(default=None, exclude=True)

    @field_validator("documents")
    @classmethod
    def _unique_document_keys(cls, v: list[DocumentEntry]) -> list[DocumentEntry]:
        keys = [d.key for d in v]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate document key(s): {dupes}")
        return v

    @property
    def project_root(self) -> Path | None:
        """Repo root, i.e. the parent of the ``projects/`` directory.

        ``None`` if this manifest was constructed directly (e.g. in a test)
        rather than via :func:`load_manifest`.
        """
        if self.manifest_dir is None:
            return None
        return self.manifest_dir.parent.parent

    def resolve(self, relative: str) -> Path:
        """Resolve a path relative to this manifest file's own directory."""
        base = self.manifest_dir or Path.cwd()
        return (base / relative).resolve()


def _format_validation_error(path: str | Path, exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc_path = ".".join(str(p) for p in first["loc"])
    msg = first["msg"]
    if msg.startswith("Value error, "):
        msg = msg[len("Value error, ") :]

    value = first.get("input")
    scalar_types = (str, int, float, bool, type(None))
    show_value = first["type"] != "missing" and isinstance(value, scalar_types)

    if not loc_path:
        detail = msg
    elif show_value:
        detail = f"{loc_path} — {msg}, got {value!r}"
    else:
        detail = f"{loc_path} — {msg}"
    return f"{path}: {detail}"


def load_manifest(path: str | Path) -> ProjectManifest:
    """Load and validate a ``project.yaml`` manifest.

    Raises :class:`ManifestError` (never a bare Pydantic/YAML exception) on
    any failure, with a message naming the offending field path.
    """
    manifest_path = Path(path)
    resolved = manifest_path if manifest_path.is_absolute() else Path.cwd() / manifest_path
    resolved = resolved.resolve()

    if not resolved.is_file():
        raise ManifestError(f"{path}: manifest file not found at {resolved}")

    try:
        raw: Any = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML — {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(
            f"{path}: manifest root — expected a mapping, got {type(raw).__name__}"
        )

    try:
        manifest = ProjectManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(_format_validation_error(path, exc)) from exc

    manifest.manifest_dir = resolved.parent
    return manifest
