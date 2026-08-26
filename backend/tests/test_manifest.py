"""Tests for core/manifest.py (S1.2.2): loads the real manifest, rejects broken ones."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from core.manifest import ManifestError, load_manifest

REAL_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "projects" / "autosar-can" / "project.yaml"
)


@pytest.fixture
def real_manifest_dict() -> dict:
    with REAL_MANIFEST_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_manifest(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# valid manifest
# --------------------------------------------------------------------------


def test_loads_real_manifest():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    assert manifest.project_id == "autosar-can"


def test_pinned_git_sha():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    assert manifest.code.git_sha == "09433770bebb8f27a7b480d7c96d814c68ffed3e"


def test_both_document_urls():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    urls = {d.key: d.url for d in manifest.documents}
    assert urls["can_driver"] == (
        "https://www.autosar.org/fileadmin/standards/R23-11/CP/AUTOSAR_CP_SWS_CANDriver.pdf"
    )
    assert urls["can_interface"] == (
        "https://www.autosar.org/fileadmin/standards/R23-11/CP/AUTOSAR_CP_SWS_CANInterface.pdf"
    )


def test_embedding_model_id():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    assert manifest.models.embedding == "openai/text-embedding-3-small"


def test_include_globs_omit_bare_can_module():
    """Ruling R5: no CAN Driver implementation exists in the repo at this SHA."""
    manifest = load_manifest(REAL_MANIFEST_PATH)
    assert not any(g.startswith("communication/Can/") for g in manifest.code.include_globs)


def test_manifest_dir_resolved_relative_to_file_not_cwd():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    assert manifest.manifest_dir == REAL_MANIFEST_PATH.parent


# --------------------------------------------------------------------------
# broken variants — each must raise ManifestError naming the bad field
# --------------------------------------------------------------------------


def test_bad_git_sha_rejected(tmp_path, real_manifest_dict):
    data = copy.deepcopy(real_manifest_dict)
    data["code"]["git_sha"] = "abc123"
    path = _write_manifest(tmp_path, data)

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert "code.git_sha" in str(excinfo.value)


def test_uncompilable_regex_rejected(tmp_path, real_manifest_dict):
    data = copy.deepcopy(real_manifest_dict)
    data["extraction"]["req_id_pattern"] = "SWS_[unclosed"
    path = _write_manifest(tmp_path, data)

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert "extraction.req_id_pattern" in str(excinfo.value)


def test_missing_required_section_rejected(tmp_path, real_manifest_dict):
    data = copy.deepcopy(real_manifest_dict)
    del data["code"]
    path = _write_manifest(tmp_path, data)

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert "code" in str(excinfo.value)


def test_duplicate_document_key_rejected(tmp_path, real_manifest_dict):
    data = copy.deepcopy(real_manifest_dict)
    data["documents"][1]["key"] = data["documents"][0]["key"]
    path = _write_manifest(tmp_path, data)

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert "documents" in str(excinfo.value)


def test_empty_documents_rejected(tmp_path, real_manifest_dict):
    data = copy.deepcopy(real_manifest_dict)
    data["documents"] = []
    path = _write_manifest(tmp_path, data)

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert "documents" in str(excinfo.value)


def test_non_http_url_rejected(tmp_path, real_manifest_dict):
    data = copy.deepcopy(real_manifest_dict)
    data["documents"][0]["url"] = "ftp://example.com/file.pdf"
    path = _write_manifest(tmp_path, data)

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert "documents.0.url" in str(excinfo.value)


def test_non_positive_cost_ceiling_rejected(tmp_path, real_manifest_dict):
    data = copy.deepcopy(real_manifest_dict)
    data["limits"]["max_report_cost_usd"] = 0
    path = _write_manifest(tmp_path, data)

    with pytest.raises(ManifestError) as excinfo:
        load_manifest(path)
    assert "limits.max_report_cost_usd" in str(excinfo.value)


def test_missing_manifest_file_raises_manifest_error(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ManifestError):
        load_manifest(missing)
