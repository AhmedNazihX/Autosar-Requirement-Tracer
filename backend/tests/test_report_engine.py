"""Tests for engines/report.py — scope, cost, the run loop, the matrix (F4.2).

Scoped to the report engine itself; the HTTP surface is ``test_reports_api``.
Judge replies are scripted, so every verdict in a matrix here is one the test
chose.
"""

from __future__ import annotations

import json

import pytest

from core import pricing
from core.config import Settings
from core.llm import chat_model
from core.manifest import ProjectManifest
from engines import evidence, report
from engines.report import ReportScope, ScopeError
from tests.support_engine import MANIFEST, build_engine, build_index, close_index
from tests.support_llm import FakeOpenRouter, json_body

PRICE = pricing.ModelPrice(model_id=MANIFEST.models.judge, prompt=1.5e-7, completion=6e-7)


@pytest.fixture
def parts(tmp_path):
    built = build_index(tmp_path)
    yield built
    close_index(built)


def verdict_body(status="implemented", *, confidence=0.8, evidence_items=(), **kwargs):
    return json_body(
        {
            "status": status,
            "evidence": [{"candidate": n, "rationale": why} for n, why in evidence_items],
            "confidence": confidence,
            "rationale": f"scripted {status}.",
        },
        **kwargs,
    )


def make_judge(*replies):
    fake = FakeOpenRouter(*replies)
    llm = chat_model(MANIFEST, "judge", api_key="sk-test", http_client=fake.http_client())
    return llm, fake


# --------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------


def test_a_module_scope_resolves_through_the_manifest(parts):
    """`CanIf` names a document's module — never a list hard-coded in code."""
    engine, _, _ = build_engine(parts)

    requirements, unknown = report.resolve_scope(engine, ReportScope(module="CanIf"))

    assert {r.id for r in requirements} == {"SWS_CANIF_00023", "SWS_CANIF_00329"}
    assert unknown == []


def test_a_module_scope_is_case_insensitive(parts):
    engine, _, _ = build_engine(parts)

    requirements, _ = report.resolve_scope(engine, ReportScope(module="canif"))

    assert len(requirements) == 2


def test_an_unknown_module_is_a_scope_error_naming_the_known_ones(parts):
    engine, _, _ = build_engine(parts)

    with pytest.raises(ScopeError) as excinfo:
        report.resolve_scope(engine, ReportScope(module="Ethernet"))

    assert "CanIf" in str(excinfo.value)


def test_a_document_scope_selects_that_document(parts):
    engine, _, _ = build_engine(parts)

    requirements, _ = report.resolve_scope(engine, ReportScope(document="can_transport"))

    assert [r.id for r in requirements] == ["SWS_CanTp_00079"]


def test_an_explicit_id_list_keeps_unknown_ids_rather_than_failing(parts):
    """Eleven good ids and one typo should produce eleven rows and a note,
    not a 400 for the batch."""
    engine, _, _ = build_engine(parts)

    requirements, unknown = report.resolve_scope(
        engine, ReportScope(req_ids=["SWS_Can_00011", "SWS_Can_99999", "!!!"])
    )

    assert [r.id for r in requirements] == ["SWS_Can_00011"]
    assert unknown == ["SWS_Can_99999", "!!!"]


def test_an_empty_scope_is_the_whole_corpus_minus_context_prose(parts):
    """Context chunks have synthetic ids and nothing to trace, so a report
    is about normative requirements only."""
    engine, _, _ = build_engine(parts)

    requirements, _ = report.resolve_scope(engine, ReportScope())

    assert len(requirements) == 8
    assert not any(r.id.startswith("CTX_") for r in requirements)


def test_a_limit_truncates_the_scope(parts):
    engine, _, _ = build_engine(parts)

    requirements, _ = report.resolve_scope(engine, ReportScope(module="Can", limit=2))

    assert len(requirements) == 2


def test_the_scope_label_reads_as_a_sentence():
    assert ReportScope(module="CanIf").label() == "module CanIf"
    assert ReportScope(req_ids=["a", "b"]).label() == "2 requirement(s)"
    assert ReportScope().label() == "the whole corpus"


# --------------------------------------------------------------------------
# cost estimate (story S4.2.2)
# --------------------------------------------------------------------------


def test_the_estimate_prices_the_scope_from_the_catalogue(parts):
    engine, _, _ = build_engine(parts)
    requirements, _ = report.resolve_scope(engine, ReportScope(module="CanIf"))

    estimate = report.estimate_cost(engine, MANIFEST.models.judge, requirements, price=PRICE)

    assert estimate.requirements == 2
    assert estimate.to_judge == 2
    assert estimate.usd > 0
    assert estimate.usd == pytest.approx(
        PRICE.cost_of(
            prompt_tokens=estimate.prompt_tokens,
            completion_tokens=estimate.completion_tokens,
        )
    )
    assert "openrouter" in estimate.basis


def test_the_estimate_costs_nothing_to_produce(parts):
    """It must not spend money working out what spending money would cost:
    the candidate count comes from SQL, with the semantic pass skipped."""
    engine, translate_fake, rerank_fake = build_engine(parts)
    requirements, _ = report.resolve_scope(engine, ReportScope())

    report.estimate_cost(engine, MANIFEST.models.judge, requirements, price=PRICE)

    assert translate_fake.calls == 0
    assert rerank_fake.calls == 0


def test_the_estimate_excludes_verdicts_already_cached(parts):
    """Re-running a scope after a small change quotes the small number."""
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(verdict_body())
    evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")
    requirements, _ = report.resolve_scope(engine, ReportScope(module="CanIf"))

    estimate = report.estimate_cost(engine, MANIFEST.models.judge, requirements, price=PRICE)

    assert estimate.cached == 1
    assert estimate.to_judge == 1


def test_rejudge_prices_every_requirement_again(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(verdict_body())
    evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")
    requirements, _ = report.resolve_scope(engine, ReportScope(module="CanIf"))

    estimate = report.estimate_cost(
        engine, MANIFEST.models.judge, requirements, rejudge=True, price=PRICE
    )

    assert estimate.to_judge == 2
    assert estimate.cached == 0


def test_no_price_means_no_figure_and_a_stated_reason(parts, monkeypatch):
    """An invented number next to a Launch button is worse than none."""
    engine, _, _ = build_engine(parts)
    monkeypatch.setattr(pricing, "price_of", lambda *a, **k: None)
    monkeypatch.setattr(pricing, "failure_reason", lambda: "ConnectError: no route")
    requirements, _ = report.resolve_scope(engine, ReportScope(module="CanIf"))

    estimate = report.estimate_cost(engine, MANIFEST.models.judge, requirements)

    assert estimate.usd is None
    assert "ConnectError" in estimate.basis
    # The count is still useful, and still reported.
    assert estimate.to_judge == 2


# --------------------------------------------------------------------------
# the ceiling
# --------------------------------------------------------------------------


def test_the_environment_ceiling_overrides_the_manifest():
    settings = Settings(_env_file=None, max_report_cost_usd=0.25)
    assert report.ceiling_usd(MANIFEST, settings) == 0.25


def test_the_manifest_ceiling_applies_when_the_environment_is_silent():
    settings = Settings(_env_file=None)
    assert report.ceiling_usd(MANIFEST, settings) == MANIFEST.limits.max_report_cost_usd


# --------------------------------------------------------------------------
# the run loop (story S4.2.1)
# --------------------------------------------------------------------------


def test_a_run_produces_one_row_per_requirement(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict_body("implemented")] * 2)

    result = report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)

    assert [row.req_id for row in result.rows] == ["SWS_CANIF_00023", "SWS_CANIF_00329"]
    assert result.coverage.implemented == 2
    assert not result.aborted


def test_a_row_carries_the_srs_ids_it_traces_up_to(parts):
    """SRS → SWS → code. The SRS documents are not ingested, so the row
    carries the id and the frontend renders it as a non-clickable chip."""
    engine, _, _ = build_engine(parts)
    conn = parts["conn"]
    conn.execute(
        "UPDATE requirements SET upstream_ids = ? WHERE id = ?",
        (json.dumps(["SRS_Can_01059"]), "SWS_CANIF_00023"),
    )
    conn.commit()
    judge_llm, _ = make_judge(*[verdict_body()] * 2)

    result = report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)

    assert result.rows[0].upstream_ids == ["SRS_Can_01059"]


def test_a_row_carries_the_module_not_just_the_document_key(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict_body()] * 2)

    result = report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)

    assert result.rows[0].doc == "can_interface"
    assert result.rows[0].module == "CanIf"


def test_progress_is_reported_once_per_requirement(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict_body()] * 2)
    seen = []

    report.run_report(
        engine,
        judge_llm,
        ReportScope(module="CanIf"),
        ceiling=2.0,
        on_progress=lambda done, total, current: seen.append((done, total, current)),
    )

    assert seen == [(1, 2, "SWS_CANIF_00023"), (2, 2, "SWS_CANIF_00329")]


def test_a_second_run_of_an_unchanged_scope_makes_no_model_calls(parts):
    """Story S4.2.1's acceptance criterion, at report level.

    The fake is scripted with exactly two replies, so a third call raises.
    """
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(*[verdict_body("partial")] * 2)

    report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)
    second = report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)

    assert fake.calls == 2
    assert second.coverage.from_cache == 2
    assert second.cost_usd == 0.0
    assert second.coverage.partial == 2


def test_rejudge_ignores_the_cache(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(
        *[verdict_body("implemented")] * 2, *[verdict_body("missing")] * 2
    )

    report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)
    second = report.run_report(
        engine, judge_llm, ReportScope(module="CanIf", rejudge=True), ceiling=2.0
    )

    assert fake.calls == 4
    assert second.coverage.missing == 2


def test_the_run_stops_at_the_cost_ceiling_and_keeps_what_it_bought(parts):
    """Story S4.2.2's acceptance: the run aborts at the ceiling.

    Eight requirements at $0.90 a verdict against a $2.00 ceiling. After two
    the run has spent $1.80 and projects $2.70 for a third, so it stops with
    two rows — the projection is checked *before* the call, so the ceiling is
    a limit rather than a thing the run notices having exceeded.

    The margin is deliberately wide. An earlier version put the arithmetic
    exactly on the boundary ($0.50 × 4 = $2.00) and was decided by the
    fractions of a cent the semantic pass spends on embeddings, which is a
    test measuring rounding rather than behaviour.
    """
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(*[verdict_body(cost_usd=0.9)] * 8)

    result = report.run_report(engine, judge_llm, ReportScope(), ceiling=2.0)

    assert result.aborted
    assert len(result.rows) == 2
    assert fake.calls == 2
    assert result.cost_usd < 2.0
    assert "MAX_REPORT_COST_USD ($2.00)" in result.abort_reason
    assert "kept" in result.abort_reason


def test_an_aborted_run_leaves_its_verdicts_in_the_cache(parts):
    """"Re-running will reuse them" has to be true, not just reassuring."""
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict_body(cost_usd=0.9)] * 8)

    first = report.run_report(engine, judge_llm, ReportScope(), ceiling=2.0)
    judged = [row.req_id for row in first.rows]

    resumed_llm, resumed_fake = make_judge(*[verdict_body(cost_usd=0.0)] * 8)
    second = report.run_report(engine, resumed_llm, ReportScope(), ceiling=2.0)

    assert [row.req_id for row in second.rows[: len(judged)]] == judged
    assert resumed_fake.calls == 8 - len(judged)
    assert second.coverage.from_cache == len(judged)
    assert not second.aborted


def test_a_cached_row_does_not_count_toward_the_ceiling(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict_body(cost_usd=0.9)] * 8)
    report.run_report(engine, judge_llm, ReportScope(), ceiling=2.0)

    free_llm, _ = make_judge(*[verdict_body(cost_usd=0.0)] * 8)
    result = report.run_report(engine, free_llm, ReportScope(), ceiling=0.01)

    assert not result.aborted
    assert len(result.rows) == 8


def test_a_run_can_be_cancelled_between_requirements(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict_body()] * 8)
    seen = []

    def stop_after_two():
        seen.append(1)
        return len(seen) > 2

    result = report.run_report(
        engine, judge_llm, ReportScope(), ceiling=2.0, should_stop=stop_after_two
    )

    assert result.aborted
    assert "cancelled" in result.abort_reason
    assert len(result.rows) == 2


def test_one_unjudgeable_requirement_does_not_end_the_run(parts):
    """A 398-row run must not be lost to one bad row."""
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(
        json_body({"nonsense": True}),
        json_body({"nonsense": True}),
        verdict_body("implemented"),
    )

    result = report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)

    assert [row.status for row in result.rows] == ["unverifiable", "implemented"]


def test_coverage_counts_every_status_and_the_cache(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(
        verdict_body("implemented", evidence_items=[(1, "yes")]),
        verdict_body("missing"),
    )

    result = report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)

    coverage = result.coverage
    assert (coverage.implemented, coverage.missing) == (1, 1)
    assert coverage.judged == 2
    assert coverage.total == 2
    assert coverage.with_evidence == 1
    assert coverage.covered_pct == pytest.approx(50.0)


def test_the_result_records_what_produced_it(parts):
    """A verdict is only meaningful against a snapshot and a judge model."""
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict_body()] * 2)

    result = report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)

    assert result.git_sha == MANIFEST.code.git_sha
    assert result.judge_model_id == MANIFEST.models.judge
    assert result.corpus_version == MANIFEST.version
    assert result.ceiling_usd == 2.0


# --------------------------------------------------------------------------
# export (story S4.3.1)
# --------------------------------------------------------------------------


@pytest.fixture
def finished(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(
        verdict_body("implemented", evidence_items=[(1, "the definition does it.")]),
        verdict_body("missing"),
    )
    return report.run_report(engine, judge_llm, ReportScope(module="CanIf"), ceiling=2.0)


def test_markdown_export_leads_with_what_produced_the_verdicts(finished):
    text = report.export(finished, "md")

    assert MANIFEST.code.git_sha in text
    assert MANIFEST.models.judge in text
    assert "| implemented | 1 |" in text
    assert "`SWS_CANIF_00023`" in text
    assert "communication/CanIf/src/CanIf.c:120-168" in text


def test_markdown_export_says_so_when_a_run_was_cut_short(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict_body(cost_usd=0.9)] * 8)
    result = report.run_report(engine, judge_llm, ReportScope(), ceiling=2.0)

    assert "Run incomplete" in report.export(result, "md")


def test_csv_export_is_one_row_per_requirement(finished):
    text = report.export(finished, "csv")
    lines = text.strip().splitlines()

    assert lines[0].startswith("req_id,module,document")
    assert len(lines) == 1 + len(finished.rows)
    assert "SWS_CANIF_00023" in lines[1]


def test_json_export_round_trips_into_the_same_result(finished):
    restored = report.ReportResult.model_validate_json(report.export(finished, "json"))

    assert restored == finished


def test_an_unknown_export_format_is_rejected(finished):
    with pytest.raises(ValueError, match="unknown export format"):
        report.export(finished, "pdf")


def test_module_of_falls_back_to_the_document_key():
    """A manifest that gains a document mid-run must not crash a matrix."""
    manifest = ProjectManifest.model_validate(MANIFEST.model_dump())
    assert report.module_of(manifest, "not_a_document") == "not_a_document"
