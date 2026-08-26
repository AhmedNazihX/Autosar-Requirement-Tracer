"""Tests for the judge evaluation harness (feature F4.4).

The point of this harness is that its numbers can be trusted, so the tests are
mostly about the things that would quietly corrupt one: the scoring rule, the
sampling, the ordering under an abort, and the exclusion of contradictory
labels.
"""

from __future__ import annotations

import json

import pytest

from core.llm import chat_model
from evaluation import judge_eval
from evaluation.judge_eval import NEGATIVE, POSITIVE, EvalSet
from tests.support_engine import MANIFEST, build_engine, build_index
from tests.support_llm import FakeOpenRouter, json_body


@pytest.fixture
def parts(tmp_path):
    return build_index(tmp_path)


def verdict(status, *, confidence=0.8, cost_usd=0.0):
    return json_body(
        {
            "status": status,
            "evidence": [],
            "confidence": confidence,
            "rationale": f"scripted {status}.",
        },
        cost_usd=cost_usd,
    )


def make_judge(*replies):
    fake = FakeOpenRouter(*replies)
    return chat_model(MANIFEST, "judge", api_key="sk-test", http_client=fake.http_client()), fake


# --------------------------------------------------------------------------
# story S4.4.1 — the set
# --------------------------------------------------------------------------


def test_the_set_splits_the_two_annotation_polarities(parts):
    """S4.4.1's acceptance: both classes non-empty, counts recorded."""
    engine, _, _ = build_engine(parts)

    built = judge_eval.build_set(engine)

    assert built.positives == ["SWS_CANIF_00023", "SWS_Can_00272"]
    assert built.negatives == ["SWS_CANIF_00329"]
    assert built.git_sha == MANIFEST.code.git_sha


def test_a_requirement_annotated_both_ways_is_excluded_and_counted(parts):
    """The corpus contradicts itself about it, so the harness says so rather
    than picking the label that helps."""
    conn = parts["conn"]
    conn.execute(
        "UPDATE code_units SET req_annotations = ? WHERE symbol = ? AND kind = 'prototype'",
        (
            json.dumps(
                [
                    {
                        "canonical_id": "SWS_CANIF_00023",
                        "raw": "!req 4.0.3/CANIF023",
                        "marker": "!",
                        "claim": "claimed_not_implemented",
                        "line": 77,
                    }
                ]
            ),
            "CanIf_Transmit",
        ),
    )
    conn.commit()
    engine, _, _ = build_engine(parts)

    built = judge_eval.build_set(engine)

    assert built.both_markers == ["SWS_CANIF_00023"]
    assert "SWS_CANIF_00023" not in built.positives
    assert "SWS_CANIF_00023" not in built.negatives


def test_annotated_ids_with_no_requirement_are_counted_as_drift(parts):
    """Finding A4: ~32% of annotated ids resolve to nothing. That is the
    product's subject, not a build failure."""
    conn = parts["conn"]
    conn.execute(
        "UPDATE code_units SET req_annotations = ? WHERE symbol = 'CanTp_MainFunction'",
        (
            json.dumps(
                [
                    {
                        "canonical_id": "SWS_CanTp_09999",
                        "raw": "@req 4.0.3/CANTP9999",
                        "marker": "@",
                        "claim": "claimed_implemented",
                        "line": 1706,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    engine, _, _ = build_engine(parts)

    built = judge_eval.build_set(engine)

    assert built.unresolved == ["SWS_CANTP_09999"]


def test_the_set_uses_the_corpus_spelling_not_the_annotations(parts):
    """Finding B3: casing differs per document and must never be normalised —
    a row keyed on a made-up spelling would not resolve."""
    engine, _, _ = build_engine(parts)

    built = judge_eval.build_set(engine)

    assert "SWS_Can_00272" in built.positives
    assert "SWS_CAN_00272" not in built.positives


def test_the_set_round_trips_through_json(parts):
    engine, _, _ = build_engine(parts)
    built = judge_eval.build_set(engine)

    assert EvalSet.from_dict(json.loads(json.dumps(built.as_dict()))) == built


# --------------------------------------------------------------------------
# sampling and ordering
# --------------------------------------------------------------------------


def test_sampling_spreads_across_the_set_rather_than_taking_a_prefix():
    """The set is sorted by id, so a prefix is one module's low numbers."""
    items = [f"SWS_X_{n:05d}" for n in range(100)]

    assert judge_eval.sample(items, 4) == [
        "SWS_X_00000",
        "SWS_X_00025",
        "SWS_X_00050",
        "SWS_X_00075",
    ]


def test_sampling_is_deterministic():
    items = [f"SWS_X_{n:05d}" for n in range(100)]
    assert judge_eval.sample(items, 7) == judge_eval.sample(items, 7)


def test_sampling_zero_or_more_than_available_takes_everything():
    items = ["a", "b", "c"]
    assert judge_eval.sample(items, 0) == items
    assert judge_eval.sample(items, 99) == items


def test_the_classes_are_interleaved_so_an_abort_truncates_both():
    """A run ordered by class and stopped at the ceiling would produce a
    matrix missing a class, which reads as a result rather than a truncation."""
    assert judge_eval.interleave(["p1", "p2", "p3"], ["n1", "n2"]) == [
        "p1",
        "n1",
        "p2",
        "n2",
        "p3",
    ]


# --------------------------------------------------------------------------
# story S4.4.2 — the scoring rule
# --------------------------------------------------------------------------


def test_the_scoring_rule_is_the_one_the_story_fixed_in_advance():
    """Asserted as data, because the rule's whole purpose is that it was
    chosen before the numbers were seen."""
    assert judge_eval.CORRECT[POSITIVE] == {"implemented", "partial"}
    assert judge_eval.WRONG[POSITIVE] == {"missing"}
    assert judge_eval.CORRECT[NEGATIVE] == {"missing", "unverifiable"}
    assert judge_eval.WRONG[NEGATIVE] == {"implemented"}


def test_the_two_cells_the_rule_does_not_assign_are_scored_as_neither():
    """`partial` on a negative and `unverifiable` on a positive. Folding
    either into a column would be exactly the choice the rule prevents."""
    assert judge_eval.UNSCORED[POSITIVE] == {"unverifiable"}
    assert judge_eval.UNSCORED[NEGATIVE] == {"partial"}
    for label in (POSITIVE, NEGATIVE):
        buckets = (
            judge_eval.CORRECT[label] | judge_eval.WRONG[label] | judge_eval.UNSCORED[label]
        )
        assert buckets == set(judge_eval.STATUSES)
        assert not judge_eval.CORRECT[label] & judge_eval.WRONG[label]


def test_a_run_scores_each_class_against_its_own_rule(parts):
    engine, _, _ = build_engine(parts)
    # Interleaved: positive, negative, positive.
    judge_llm, fake = make_judge(
        verdict("implemented"), verdict("missing"), verdict("partial")
    )
    built = judge_eval.build_set(engine)

    result = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)

    assert fake.calls == 3
    assert result.positive.judged == 2
    assert result.negative.judged == 1
    assert result.positive.rate(judge_eval.CORRECT[POSITIVE]) == 100.0
    assert result.negative.rate(judge_eval.CORRECT[NEGATIVE]) == 100.0
    assert result.negative.rate(judge_eval.WRONG[NEGATIVE]) == 0.0


def test_an_implemented_verdict_on_a_negative_is_a_false_positive(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(
        verdict("implemented"), verdict("implemented"), verdict("implemented")
    )
    built = judge_eval.build_set(engine)

    result = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)

    assert result.negative.rate(judge_eval.WRONG[NEGATIVE]) == 100.0
    assert result.as_dict()["false_positive_rate_pct"] == 100.0
    # Two true positives against one false positive.
    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(1.0)


def test_a_missing_verdict_on_a_positive_is_a_false_negative(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(verdict("missing"), verdict("missing"), verdict("missing"))
    built = judge_eval.build_set(engine)

    result = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)

    assert result.positive.rate(judge_eval.WRONG[POSITIVE]) == 100.0
    assert result.recall == 0.0
    assert result.precision is None  # nothing was called implemented at all


def test_mean_confidence_is_reported_per_cell(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(
        verdict("implemented", confidence=0.9),
        verdict("missing", confidence=0.4),
        verdict("implemented", confidence=0.7),
    )
    built = judge_eval.build_set(engine)

    result = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)

    assert result.positive.mean_confidence("implemented") == pytest.approx(0.8)
    assert result.negative.mean_confidence("missing") == pytest.approx(0.4)
    assert result.positive.mean_confidence("partial") is None


def test_the_unverifiable_rate_is_reported_per_class(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(
        verdict("unverifiable"), verdict("unverifiable"), verdict("implemented")
    )
    built = judge_eval.build_set(engine)

    result = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)

    assert result.positive.as_dict()["unverifiable_pct"] == 50.0
    assert result.negative.as_dict()["unverifiable_pct"] == 100.0


def test_a_re_run_of_the_same_evaluation_costs_nothing(parts):
    """S4.4.2 requires the verdict cache to apply here like anywhere else."""
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(*[verdict("implemented")] * 3)
    built = judge_eval.build_set(engine)

    judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)
    again = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)

    assert fake.calls == 3
    assert again.cost_usd == 0.0
    assert again.positive.judged == 2


def test_the_blind_run_is_scored_separately_from_the_sighted_one(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, fake = make_judge(*[verdict("implemented")] * 3, *[verdict("missing")] * 3)
    built = judge_eval.build_set(engine)

    sighted = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)
    blind = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0, blind=True)

    assert fake.calls == 6, "the blind run must not read the sighted run's cache"
    assert sighted.mode == "sighted"
    assert blind.mode == "blind"
    assert blind.recall == 0.0


def test_a_run_stopped_at_the_ceiling_says_so(parts):
    """The ceiling applies here like anywhere else, and a truncated matrix
    must never be presented as a complete one."""
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict("implemented", cost_usd=0.9)] * 3)
    built = judge_eval.build_set(engine)

    result = judge_eval.evaluate(engine, judge_llm, built, ceiling=1.0)

    assert result.aborted
    assert "MAX_REPORT_COST_USD" in result.abort_reason
    assert result.positive.judged + result.negative.judged < 3


# --------------------------------------------------------------------------
# story S4.4.3 — the table
# --------------------------------------------------------------------------


def test_the_table_states_the_counts_the_caveats_depend_on(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict("implemented")] * 3)
    built = judge_eval.build_set(engine)
    result = judge_eval.evaluate(engine, judge_llm, built, ceiling=2.0)

    table = judge_eval.to_markdown(built, [result])

    assert "| sighted | positive (`@req`) |" in table
    assert "| sighted | negative (`!req`) |" in table
    assert "false-positive rate on the negative set" in table
    assert "excluded for carrying both markers" in table
    assert "resolve to no requirement" in table


def test_the_table_marks_a_truncated_run_as_incomplete(parts):
    engine, _, _ = build_engine(parts)
    judge_llm, _ = make_judge(*[verdict("implemented", cost_usd=0.9)] * 3)
    built = judge_eval.build_set(engine)
    result = judge_eval.evaluate(engine, judge_llm, built, ceiling=1.0)

    assert "Run incomplete" in judge_eval.to_markdown(built, [result])
