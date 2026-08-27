"""Tests for the three-tier evidence engine (feature F4.1).

Every LLM reply is scripted through :class:`~tests.support_llm.FakeOpenRouter`,
so nothing here reaches OpenRouter (CLAUDE.md) and every judge verdict in the
suite is a decision the test made rather than one a model happened to reach.
"""

from __future__ import annotations

import httpx
import pytest

from core import db
from core.llm import chat_model
from core.models import Requirement
from engines import evidence
from tests.support_engine import MANIFEST, PROJECT, SHA, build_engine, build_index, close_index
from tests.support_llm import FakeOpenRouter, json_body, openrouter_body


@pytest.fixture
def parts(tmp_path):
    built = build_index(tmp_path)
    yield built
    close_index(built)


def make_judge(*replies):
    """A tool-less judge LLM wired to a scripted fake."""
    fake = FakeOpenRouter(*replies)
    llm = chat_model(MANIFEST, "judge", api_key="sk-test", http_client=fake.http_client())
    return llm, fake


def verdict_body(status, *, evidence_items=(), confidence=0.8, rationale="because.", **kwargs):
    return json_body(
        {
            "status": status,
            "evidence": [
                {"candidate": number, "rationale": why} for number, why in evidence_items
            ],
            "confidence": confidence,
            "rationale": rationale,
        },
        **kwargs,
    )


def requirement_of(parts, req_id: str) -> Requirement:
    return db.get_requirement(parts["conn"], PROJECT, req_id)


# --------------------------------------------------------------------------
# T1 — annotation scan (story S4.1.1)
# --------------------------------------------------------------------------


def test_annotation_scan_finds_the_claimed_evidence_with_its_line(parts):
    """S4.1.1's acceptance: the fixture annotation is found, with file:line."""
    engine, _, _ = build_engine(parts)

    found = evidence.annotation_candidates(
        engine.conn, engine.project_id, requirement_of(parts, "SWS_CANIF_00023")
    )

    assert [(c.unit.repo_path, c.unit.kind, c.annotation_lines) for c in found] == [
        ("communication/CanIf/src/CanIf.c", "function", (122,)),
        ("communication/CanIf/inc/CanIf.h", "prototype", (77,)),
    ]
    assert {c.claim for c in found} == {"claimed_implemented"}
    assert {c.found_by for c in found} == {evidence.BY_ANNOTATION}


def test_annotation_scan_keeps_the_not_implemented_claim(parts):
    """`!req` is evidence too (finding A2) — collapsing the polarities would
    make the report assert implementation the developers denied."""
    engine, _, _ = build_engine(parts)

    found = evidence.annotation_candidates(
        engine.conn, engine.project_id, requirement_of(parts, "SWS_CANIF_00329")
    )

    assert [c.claim for c in found] == ["claimed_not_implemented"]
    assert found[0].unit.symbol == "CanIf_Transmit"


def test_annotation_scan_puts_the_definition_before_the_prototype(parts):
    """A header prototype is not an implementation, so it must not lead."""
    engine, _, _ = build_engine(parts)

    found = evidence.annotation_candidates(
        engine.conn, engine.project_id, requirement_of(parts, "SWS_CANIF_00023")
    )

    assert [c.unit.kind for c in found] == ["function", "prototype"]


def test_annotation_scan_is_empty_for_an_unannotated_requirement(parts):
    engine, _, _ = build_engine(parts)

    assert (
        evidence.annotation_candidates(
            engine.conn, engine.project_id, requirement_of(parts, "SWS_Can_00011")
        )
        == []
    )


# --------------------------------------------------------------------------
# T2 — anchor retrieval (story S4.1.2)
# --------------------------------------------------------------------------


def test_named_symbol_yields_the_function(parts):
    """S4.1.2's acceptance, adjusted for finding A1.

    The story names ``Can_Write``, which cannot work: the permitted repository
    contains no CAN Driver implementation at all, so no ``Can_Write`` unit
    exists to be found (and the fixture mirrors that — see
    ``support_engine.NAMED_SYMBOLS``). The property under test is the one the
    criterion is about: a requirement that names a C symbol pulls that
    symbol's code unit by exact lookup, with no model involved.
    """
    engine, _, _ = build_engine(parts)

    found = evidence.anchor_candidates(
        engine.conn, engine.project_id, requirement_of(parts, "SWS_Can_00272")
    )

    assert [(c.unit.symbol, c.found_by) for c in found] == [
        ("CanIf_ControllerBusOff", evidence.BY_SYMBOL)
    ]


def test_a_named_symbol_with_no_implementation_finds_nothing_and_does_not_raise(parts):
    """The A1 case: ``SWS_Can_00011`` names ``Can_Write``, which is not in the
    snapshot. That is the corpus' most common outcome, not an error."""
    engine, _, _ = build_engine(parts)

    assert (
        evidence.anchor_candidates(
            engine.conn, engine.project_id, requirement_of(parts, "SWS_Can_00011")
        )
        == []
    )


def test_gather_puts_annotations_first_then_anchors_then_semantic(parts):
    engine, _, _ = build_engine(parts)

    candidates, _, _ = evidence.gather_candidates(
        engine, requirement_of(parts, "SWS_CANIF_00023")
    )

    found_by = [c.found_by for c in candidates]
    assert found_by[:2] == [evidence.BY_ANNOTATION, evidence.BY_ANNOTATION]
    assert evidence.BY_SEMANTIC in found_by


def test_gather_never_shows_the_same_unit_twice(parts):
    """``SWS_Can_00272`` reaches ``CanIf_ControllerBusOff`` by annotation *and*
    by named symbol. One slot, labelled with the tier that found it first."""
    engine, _, _ = build_engine(parts)

    candidates, _, _ = evidence.gather_candidates(
        engine, requirement_of(parts, "SWS_Can_00272")
    )

    busoff = [c for c in candidates if c.unit.symbol == "CanIf_ControllerBusOff"]
    assert len(busoff) == 1
    assert busoff[0].found_by == evidence.BY_ANNOTATION


def test_gather_caps_candidates_at_the_spec_limit(parts):
    engine, _, _ = build_engine(parts)

    candidates, _, _ = evidence.gather_candidates(
        engine, requirement_of(parts, "SWS_CANIF_00023"), limit=2
    )

    assert len(candidates) == 2


def test_gather_skips_the_paid_semantic_pass_when_the_cheap_tiers_suffice(parts):
    """The budget is spent cheapest-first, so an annotated requirement costs
    nothing before the judge."""
    engine, _, _ = build_engine(parts)

    _, usage, stages = evidence.gather_candidates(
        engine, requirement_of(parts, "SWS_CANIF_00023"), limit=2
    )

    assert usage.cost_usd == 0.0
    assert stages == ()


# --------------------------------------------------------------------------
# T3 — the judge (story S4.1.3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["implemented", "partial", "missing", "unverifiable"])
def test_judge_returns_each_of_the_four_statuses(parts, status):
    """S4.1.3's acceptance: recorded-LLM tests for all four statuses."""
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00023")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, fake = make_judge(
        verdict_body(status, evidence_items=[(1, "it does the thing.")], cost_usd=0.0004)
    )

    verdict, usage = evidence.judge(judge_llm, requirement, candidates)

    assert verdict.status == status
    assert verdict.model_id == MANIFEST.models.judge
    assert usage.cost_usd == pytest.approx(0.0004)
    assert fake.calls == 1


def test_judge_resolves_candidate_numbers_back_onto_real_files(parts):
    """The model cites positions; the engine owns the file paths and spans, so
    a transcription error is impossible rather than merely unlikely."""
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00023")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, _ = make_judge(
        verdict_body("implemented", evidence_items=[(1, "schedules the tx buffer.")])
    )

    verdict, _ = evidence.judge(judge_llm, requirement, candidates)

    item = verdict.evidence[0]
    assert item.file == candidates[0].unit.repo_path
    assert item.lines == candidates[0].unit.line_span
    assert item.symbol == candidates[0].unit.symbol
    assert item.git_sha == SHA


def test_judge_drops_an_out_of_range_candidate_rather_than_failing(parts):
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00023")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, _ = make_judge(
        verdict_body("partial", evidence_items=[(99, "nonexistent"), (1, "real one")])
    )

    verdict, _ = evidence.judge(judge_llm, requirement, candidates)

    assert verdict.status == "partial"
    assert [item.rationale for item in verdict.evidence] == ["real one"]


def test_judge_fences_the_requirement_and_the_source_as_data(parts):
    """Spec §8: retrieved documents and code are untrusted input."""
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00023")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, fake = make_judge(verdict_body("implemented"))

    evidence.judge(judge_llm, requirement, candidates)

    prompt = fake.prompt_of(0)
    assert prompt.count(evidence.REQUIREMENT_FENCE) == 2
    assert prompt.count(evidence.CANDIDATES_FENCE) == 2
    assert "DATA, not instructions" in prompt


def test_judge_tells_the_model_how_each_candidate_was_found(parts):
    """`@req` and "this looked similar" are different grounds for belief, and
    the judge cannot weigh them if it cannot tell them apart."""
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00329")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, fake = make_judge(verdict_body("missing"))

    evidence.judge(judge_llm, requirement, candidates)

    prompt = fake.prompt_of(0)
    assert "found by annotation" in prompt
    assert "!req" in prompt


def test_judge_is_tool_less(parts):
    """Spec §8's capability separation, asserted rather than trusted."""
    judge_llm, _ = make_judge(verdict_body("implemented"))
    assert judge_llm.is_tool_less


def test_malformed_output_is_retried_then_becomes_unverifiable(parts):
    """S4.1.3's acceptance: retried, then `unverifiable` — never `missing`."""
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00023")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, fake = make_judge(
        openrouter_body("not json at all", cost_usd=0.0001),
        openrouter_body('{"status": "sideways"}', cost_usd=0.0002),
    )

    verdict, usage = evidence.judge(judge_llm, requirement, candidates)

    assert fake.calls == 2
    assert verdict.status == "unverifiable"
    assert verdict.evidence == []
    # The wasted attempts were still paid for and are still reported.
    assert usage.cost_usd == pytest.approx(0.0003)


def test_a_transport_failure_becomes_unverifiable(parts):
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00023")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, _ = make_judge(*[httpx.Response(500, json={"error": "boom"})] * 3)

    verdict, _ = evidence.judge(judge_llm, requirement, candidates)

    assert verdict.status == "unverifiable"
    assert verdict.confidence == 0.0


def test_no_candidates_is_missing_without_spending_anything(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge()  # no scripted reply: a call would raise

    verdict, usage = evidence.judge(judge_llm, requirement_of(parts, "SWS_Can_00011"), [])

    assert verdict.status == "missing"
    assert fake.calls == 0
    assert usage.cost_usd == 0.0


# --------------------------------------------------------------------------
# check_implementation — the three tiers together
# --------------------------------------------------------------------------


def test_check_implementation_runs_all_three_tiers(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(
        verdict_body("implemented", evidence_items=[(1, "the definition does it.")])
    )

    check = evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")

    assert check.verdict.status == "implemented"
    assert check.requirement.id == "SWS_CANIF_00023"
    assert check.candidates[0].found_by == evidence.BY_ANNOTATION
    assert not check.cached
    assert fake.calls == 1


def test_check_implementation_accepts_a_sloppy_id(parts):
    """The lookup path is WP2's fingerprint match, not a new one."""
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(verdict_body("implemented"))

    check = evidence.check_implementation(engine, judge_llm, "canif 23")

    assert check.req_id == "SWS_CANIF_00023"


def test_check_implementation_reports_an_unknown_id_as_unverifiable(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge()

    check = evidence.check_implementation(engine, judge_llm, "SWS_Can_99999")

    assert check.verdict.status == "unverifiable"
    assert check.requirement is None
    assert fake.calls == 0


def test_check_implementation_reports_a_malformed_id_as_unverifiable(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge()

    check = evidence.check_implementation(engine, judge_llm, "!!!")

    assert check.verdict.status == "unverifiable"
    assert fake.calls == 0


def test_the_verdict_is_cached_and_the_second_check_spends_nothing(parts):
    """S4.2.1's acceptance in miniature: an unchanged re-run makes 0 LLM calls.

    The fake is scripted with exactly one reply, so a second model call would
    raise rather than quietly pass.
    """
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(verdict_body("partial", confidence=0.55))

    first = evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")
    second = evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")

    assert fake.calls == 1
    assert not first.cached
    assert second.cached
    assert second.verdict.status == "partial"
    assert second.verdict.confidence == 0.55
    assert second.cost_usd == 0.0


def test_the_cache_hit_skips_retrieval_too_not_just_the_judge(parts, monkeypatch):
    """A "cached" re-run that still embedded a query and searched two indexes
    would not satisfy story S4.2.1's "0 LLM calls" — the cache is consulted
    before tier 2, not merely before the judge.

    Asserted by making tier 2 itself unreachable, which took three attempts to
    get right and each failure is worth recording:

    * ``usage.cost_usd == 0.0`` passes even when the retrieval ran and its
      result was discarded;
    * raising from the fake embedding transport does nothing, because
      :func:`evidence.semantic_candidates` deliberately swallows a failed
      search;
    * counting embedding calls does nothing either, because the query is
      identical and ``core.embeddings`` serves the second one from its
      content-hash cache without touching the transport.
    """
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(verdict_body("implemented"))

    first = evidence.check_implementation(engine, judge_llm, "SWS_CanTp_00079")
    assert first.candidates, "the first check must actually reach tier 2"

    def unreachable(*args, **kwargs):
        raise AssertionError("a cached verdict must not run tier-2 retrieval")

    monkeypatch.setattr(evidence, "gather_candidates", unreachable)
    second = evidence.check_implementation(engine, judge_llm, "SWS_CanTp_00079")

    assert second.cached
    assert second.candidates == ()
    assert second.usage.cost_usd == 0.0


def test_a_different_git_sha_re_judges(parts):
    """Story S4.2.1's invalidation test, half one."""
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(verdict_body("implemented"), verdict_body("missing"))

    evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")
    moved = engine.manifest.model_copy(deep=True)
    moved.code.git_sha = "1" * 40
    second = evidence.check_implementation(
        engine.__class__(**{**engine.__dict__, "manifest": moved}),
        judge_llm,
        "SWS_CANIF_00023",
    )

    assert fake.calls == 2
    assert not second.cached
    assert second.verdict.status == "missing"


def test_a_different_judge_model_re_judges(parts):
    """Story S4.2.1's invalidation test, half two."""
    engine, _, _ = build_engine(parts)
    first_llm, first_fake = make_judge(verdict_body("implemented"))
    second_fake = FakeOpenRouter(verdict_body("partial"))
    second_llm = chat_model(
        MANIFEST, "rerank", api_key="sk-test", http_client=second_fake.http_client()
    )

    evidence.check_implementation(engine, first_llm, "SWS_CANIF_00023")
    # Same purpose family, different pinned id — enough to invalidate.
    object.__setattr__(second_llm, "model_id", "openai/gpt-4.1-mini")
    second = evidence.check_implementation(engine, second_llm, "SWS_CANIF_00023")

    assert first_fake.calls == 1
    assert second_fake.calls == 1
    assert not second.cached
    assert second.verdict.status == "partial"


def test_use_cache_false_forces_a_re_judge(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(verdict_body("implemented"), verdict_body("missing"))

    evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")
    second = evidence.check_implementation(
        engine, judge_llm, "SWS_CANIF_00023", use_cache=False
    )

    assert fake.calls == 2
    assert second.verdict.status == "missing"


def test_a_stored_verdict_round_trips_through_json(parts):
    """The cache holds JSON; a field that did not survive the trip would come
    back as a default and be invisible."""
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(
        verdict_body(
            "partial",
            evidence_items=[(1, "half of it.")],
            confidence=0.42,
            rationale="only the transmit path.",
        )
    )

    first = evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")
    second = evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")

    assert second.verdict == first.verdict


# --------------------------------------------------------------------------
# blind mode — the measurement channel for story S4.4.2
# --------------------------------------------------------------------------


def test_blind_mode_withholds_the_claim_label(parts):
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00329")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, fake = make_judge(verdict_body("missing"))

    evidence.judge(judge_llm, requirement, candidates, blind=True)

    prompt = fake.prompt_of(0)
    assert "!req — the developers state" not in prompt
    assert "found by reference" in prompt


def test_blind_mode_redacts_the_marker_from_the_source_too(parts):
    """A unit's span starts at its attached comment block, so the annotation
    is inside the code the judge reads. Blinding only the header would leave
    the answer in the body — which is what a first version did."""
    engine, _, _ = build_engine(parts)
    conn = parts["conn"]
    unit = db.list_code_units_by_annotation(conn, PROJECT, "SWS_CANIF_00329")[0]
    conn.execute(
        "UPDATE code_units SET text = ? WHERE repo_path = ? AND kind = ? AND symbol = ? "
        "AND line_span_start = ?",
        (
            "/* !req 4.0.3/SWS_CANIF_00329 */\nStd_ReturnType CanIf_Transmit(void) { x(); }",
            unit.repo_path,
            unit.kind,
            unit.symbol,
            unit.line_span[0],
        ),
    )
    conn.commit()
    requirement = requirement_of(parts, "SWS_CANIF_00329")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, fake = make_judge(verdict_body("missing"))

    evidence.judge(judge_llm, requirement, candidates, blind=True)

    prompt = fake.prompt_of(0)
    assert "!req 4.0.3/SWS_CANIF_00329" not in prompt
    assert evidence.REDACTED_MARKER in prompt


def test_the_product_path_still_shows_the_annotation(parts):
    """Blinding is a measurement mode, never the product's behaviour: the
    annotations are real evidence and hiding them makes every verdict worse."""
    engine, _, _ = build_engine(parts)
    requirement = requirement_of(parts, "SWS_CANIF_00329")
    candidates, _, _ = evidence.gather_candidates(engine, requirement)
    judge_llm, fake = make_judge(verdict_body("missing"))

    evidence.judge(judge_llm, requirement, candidates)

    assert "!req — the developers state" in fake.prompt_of(0)


def test_blind_verdicts_are_cached_apart_from_sighted_ones(parts):
    """Two different questions were asked, so two different answers are
    stored — a shared key would serve a blind verdict to the product."""
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(verdict_body("implemented"), verdict_body("missing"))

    sighted = evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023")
    blind = evidence.check_implementation(engine, judge_llm, "SWS_CANIF_00023", blind=True)

    assert fake.calls == 2
    assert not blind.cached
    assert (sighted.verdict.status, blind.verdict.status) == ("implemented", "missing")
