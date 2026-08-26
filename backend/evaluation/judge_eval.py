"""Measuring the LLM judge against the corpus' own labels (feature F4.4).

The C sources carry two markers meaning opposite things: ``@req`` claims a
requirement **is** implemented, ``!req`` states it explicitly is **not**
(findings A2/A3 — 914 against 150 across the scoped files). That is a
developer-authored labelled set, positives and negatives, sitting in the
corpus for free, and it is the only ground truth ``check_implementation`` has.

    uv run python -m evaluation.judge_eval build      # story S4.4.1
    uv run python -m evaluation.judge_eval run        # story S4.4.2

**The scoring rule is fixed here, in advance, so it cannot be chosen to
flatter the result.** It is story S4.4.2's, verbatim:

* on the **negative** set, ``missing`` or ``unverifiable`` is correct and
  ``implemented`` is a false positive;
* on the **positive** set, ``implemented`` or ``partial`` is correct and
  ``missing`` is a false negative.

Two cells the story does not assign — ``partial`` on a negative and
``unverifiable`` on a positive — are reported as their own line and scored as
neither. Folding them into either column would be exactly the choice this rule
exists to prevent.

**The eval set is a committed fixture, not a query.** Story S4.4.1 requires it:
re-ingesting the corpus, or widening a glob, would otherwise change the
denominator between two runs and make two numbers look comparable when they
are not.

**Two runs, and the difference between them is the point.**

* *sighted* — the product exactly as it ships. The judge sees the annotation
  and its claim, because that is real evidence and withholding it would make
  every shipped verdict worse.
* *blind* — the annotation's claim withheld and its marker redacted from the
  source (:func:`engines.evidence.redact_annotations`). This is the honest
  measurement: in the sighted run the judge can read the very label being
  scored against, so a high score there measures obedience, not analysis.

Report both. The sighted number says "does the judge respect and pass through
what the developers documented"; the blind number says "can it tell from the
code". They answer different questions and only one of them is an accuracy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core import db, llm, paths
from core import embeddings as embeddings_module
from core.config import get_settings
from core.manifest import ProjectManifest, load_manifest
from core.models import Requirement
from engines import report
from engines.report import ReportScope
from retrieval import bm25, vector_store
from retrieval.pipeline import Engine

#: The committed eval set. Under ``tests/fixtures`` because that is where this
#: repository already keeps "facts a later change must not silently move".
DEFAULT_SET_PATH = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "judge_eval_set.json"
)

#: Where a run's full result is written, for regenerating the README table
#: without paying for the run again.
DEFAULT_RESULT_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "evaluations" / "judge-eval.json"
)

#: Requirements per class in a default run. 40 + 40 is ~80 judge calls, a few
#: cents on the pinned judge model, and enough for a rate to mean something.
#: ``0`` means the whole set.
DEFAULT_LIMIT_PER_CLASS = 40

POSITIVE = "positive"
NEGATIVE = "negative"

#: Story S4.4.2's rule, as data so the report cannot describe one thing and
#: compute another.
CORRECT: dict[str, frozenset[str]] = {
    POSITIVE: frozenset({"implemented", "partial"}),
    NEGATIVE: frozenset({"missing", "unverifiable"}),
}
WRONG: dict[str, frozenset[str]] = {
    POSITIVE: frozenset({"missing"}),
    NEGATIVE: frozenset({"implemented"}),
}
#: Everything else: scored as neither, reported by name.
UNSCORED: dict[str, frozenset[str]] = {
    POSITIVE: frozenset({"unverifiable"}),
    NEGATIVE: frozenset({"partial"}),
}

STATUSES = ("implemented", "partial", "missing", "unverifiable")


# --------------------------------------------------------------------------
# story S4.4.1 — building the set
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalSet:
    """The labelled requirements, and the accounting behind them."""

    project_id: str
    corpus_version: str
    git_sha: str
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    #: Annotated with **both** markers somewhere in the code. The corpus
    #: contradicts itself about these, so story S4.4.1 excludes them rather
    #: than guessing — and counts them, because a silent exclusion is a
    #: denominator nobody can check.
    both_markers: list[str] = field(default_factory=list)
    #: Annotated ids that resolve to no requirement in this corpus. Not an
    #: error: this is the 4.0.3 → R23-11 release drift finding A4 measures.
    unresolved: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "corpus_version": self.corpus_version,
            "git_sha": self.git_sha,
            "counts": {
                "positives": len(self.positives),
                "negatives": len(self.negatives),
                "both_markers": len(self.both_markers),
                "unresolved": len(self.unresolved),
            },
            "positives": self.positives,
            "negatives": self.negatives,
            "both_markers": self.both_markers,
            "unresolved": self.unresolved,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> EvalSet:
        return cls(
            project_id=payload["project_id"],
            corpus_version=payload["corpus_version"],
            git_sha=payload["git_sha"],
            positives=list(payload["positives"]),
            negatives=list(payload["negatives"]),
            both_markers=list(payload["both_markers"]),
            unresolved=list(payload["unresolved"]),
        )


def build_set(engine: Engine) -> EvalSet:
    """Derive the labelled set from the ingested corpus.

    Ids are stored as the *corpus* spells them, not as the annotation does:
    finding B3 says casing differs per document and must never be normalised,
    and a report row keyed on a made-up spelling would not resolve.
    """
    claims = db.annotated_ids(engine.conn, engine.project_id)
    spelling = {
        Requirement.canonical_id(requirement.id): requirement.id
        for requirement in db.list_requirements(engine.conn, engine.project_id)
    }

    positives: list[str] = []
    negatives: list[str] = []
    both: list[str] = []
    unresolved: list[str] = []

    for canonical_id in sorted(claims):
        real_id = spelling.get(canonical_id)
        if real_id is None:
            unresolved.append(canonical_id)
            continue
        markers = claims[canonical_id]
        if len(markers) > 1:
            both.append(real_id)
        elif "claimed_implemented" in markers:
            positives.append(real_id)
        else:
            negatives.append(real_id)

    return EvalSet(
        project_id=engine.project_id,
        corpus_version=engine.manifest.version,
        git_sha=engine.manifest.code.git_sha,
        positives=positives,
        negatives=negatives,
        both_markers=both,
        unresolved=unresolved,
    )


def sample(items: Sequence[str], limit: int) -> list[str]:
    """``limit`` items spread evenly across ``items``. Deterministic.

    Evenly spaced rather than the first ``limit``: the set is sorted by id, so
    a prefix would be one module's low-numbered requirements and the sample
    would silently be about one area of one document.
    """
    if limit <= 0 or limit >= len(items):
        return list(items)
    stride = len(items) / limit
    return [items[int(index * stride)] for index in range(limit)]


def interleave(positives: Sequence[str], negatives: Sequence[str]) -> list[str]:
    """One from each class in turn.

    The run can stop at ``MAX_REPORT_COST_USD`` partway through, and a run
    ordered by class would then produce a matrix with one class missing —
    which reads as a result rather than as a truncation.
    """
    ordered: list[str] = []
    for index in range(max(len(positives), len(negatives))):
        if index < len(positives):
            ordered.append(positives[index])
        if index < len(negatives):
            ordered.append(negatives[index])
    return ordered


# --------------------------------------------------------------------------
# story S4.4.2 — running it
# --------------------------------------------------------------------------


@dataclass
class ClassResult:
    """One class' outcomes: how often each verdict came back, and how sure."""

    label: str
    counts: Counter = field(default_factory=Counter)
    confidence: dict[str, list[float]] = field(default_factory=dict)

    def add(self, status: str, confidence: float) -> None:
        self.counts[status] += 1
        self.confidence.setdefault(status, []).append(confidence)

    @property
    def judged(self) -> int:
        return sum(self.counts.values())

    def mean_confidence(self, status: str) -> float | None:
        values = self.confidence.get(status)
        return sum(values) / len(values) if values else None

    def rate(self, statuses: frozenset[str]) -> float:
        hit = sum(self.counts[status] for status in statuses)
        return 100.0 * hit / self.judged if self.judged else 0.0

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "judged": self.judged,
            "counts": {status: self.counts[status] for status in STATUSES},
            "mean_confidence": {
                status: self.mean_confidence(status) for status in STATUSES
            },
            "correct_pct": self.rate(CORRECT[self.label]),
            "wrong_pct": self.rate(WRONG[self.label]),
            "unscored_pct": self.rate(UNSCORED[self.label]),
            "unverifiable_pct": self.rate(frozenset({"unverifiable"})),
        }


@dataclass
class EvalResult:
    """A finished evaluation of one mode."""

    mode: str
    judge_model_id: str
    git_sha: str
    corpus_version: str
    positive: ClassResult
    negative: ClassResult
    cost_usd: float = 0.0
    aborted: bool = False
    abort_reason: str | None = None

    @property
    def precision(self) -> float | None:
        """Precision of the ``implemented`` label, across both classes."""
        true_positive = self.positive.counts["implemented"]
        false_positive = self.negative.counts["implemented"]
        total = true_positive + false_positive
        return true_positive / total if total else None

    @property
    def recall(self) -> float | None:
        """Recall of the ``implemented`` label over the judged positives."""
        judged = self.positive.judged
        return self.positive.counts["implemented"] / judged if judged else None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "judge_model_id": self.judge_model_id,
            "git_sha": self.git_sha,
            "corpus_version": self.corpus_version,
            "positive": self.positive.as_dict(),
            "negative": self.negative.as_dict(),
            "precision_implemented": self.precision,
            "recall_implemented": self.recall,
            "false_positive_rate_pct": self.negative.rate(WRONG[NEGATIVE]),
            "cost_usd": self.cost_usd,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
        }


def evaluate(
    engine: Engine,
    judge_llm: llm.Llm,
    eval_set: EvalSet,
    *,
    limit_per_class: int = DEFAULT_LIMIT_PER_CLASS,
    ceiling: float,
    blind: bool = False,
    on_progress=None,
) -> EvalResult:
    """Judge the sampled set and score it against the fixed rule.

    Runs through :func:`engines.report.run_report`, so it inherits the verdict
    cache and the cost ceiling rather than reimplementing either — a re-run
    costs nothing, which is what story S4.4.2 requires.
    """
    positives = sample(eval_set.positives, limit_per_class)
    negatives = sample(eval_set.negatives, limit_per_class)
    label_of = {req_id: POSITIVE for req_id in positives}
    label_of.update({req_id: NEGATIVE for req_id in negatives})

    scope = ReportScope(req_ids=interleave(positives, negatives))
    result = report.run_report(
        engine,
        judge_llm,
        scope,
        ceiling=ceiling,
        on_progress=on_progress,
        blind=blind,
    )

    per_class = {
        POSITIVE: ClassResult(label=POSITIVE),
        NEGATIVE: ClassResult(label=NEGATIVE),
    }
    for row in result.rows:
        per_class[label_of[row.req_id]].add(row.status, row.confidence)

    return EvalResult(
        mode="blind" if blind else "sighted",
        judge_model_id=judge_llm.model_id,
        git_sha=engine.manifest.code.git_sha,
        corpus_version=engine.manifest.version,
        positive=per_class[POSITIVE],
        negative=per_class[NEGATIVE],
        cost_usd=result.cost_usd,
        aborted=result.aborted,
        abort_reason=result.abort_reason,
    )


# --------------------------------------------------------------------------
# story S4.4.3 — the table
# --------------------------------------------------------------------------


def to_markdown(eval_set: EvalSet, results: Sequence[EvalResult]) -> str:
    """The README table, with the scoring rule and the caveats attached."""
    lines = [
        "| mode | class | n | implemented | partial | missing | unverifiable | "
        "correct | false positives |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        for outcome in (result.positive, result.negative):
            counts = outcome.counts
            lines.append(
                f"| {result.mode} | {outcome.label} (`"
                f"{'@req' if outcome.label == POSITIVE else '!req'}`) | {outcome.judged} | "
                f"{counts['implemented']} | {counts['partial']} | {counts['missing']} | "
                f"{counts['unverifiable']} | {outcome.rate(CORRECT[outcome.label]):.0f}% | "
                f"{counts['implemented'] if outcome.label == NEGATIVE else '—'} |"
            )

    lines.append("")
    for result in results:
        precision = f"{result.precision:.2f}" if result.precision is not None else "n/a"
        recall = f"{result.recall:.2f}" if result.recall is not None else "n/a"
        lines.append(
            f"**{result.mode}** — precision of `implemented` {precision}, recall "
            f"{recall}, false-positive rate on the negative set "
            f"{result.negative.rate(WRONG[NEGATIVE]):.1f}%, `unverifiable` "
            f"{result.positive.rate(frozenset({'unverifiable'})):.0f}% of positives / "
            f"{result.negative.rate(frozenset({'unverifiable'})):.0f}% of negatives. "
            f"Judge `{result.judge_model_id}`, ${result.cost_usd:.4f}."
        )
        if result.aborted:
            lines.append(f"> Run incomplete: {result.abort_reason}")

    lines.extend(
        [
            "",
            f"Set built from the ingested corpus at `{eval_set.git_sha[:12]}`: "
            f"{len(eval_set.positives)} positives, {len(eval_set.negatives)} negatives, "
            f"{len(eval_set.both_markers)} excluded for carrying both markers, "
            f"{len(eval_set.unresolved)} annotated ids that resolve to no requirement "
            "in this release.",
        ]
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def open_engine(manifest: ProjectManifest) -> Engine:
    """A read-only :class:`Engine` over the ingested corpus, for a CLI run."""
    conn = db.pooled(paths.db_path(manifest))
    db.migrate(conn)
    collection = vector_store.open_collection(
        vector_store.open_client(paths.chroma_dir(manifest)), manifest.project_id
    )
    return Engine(
        manifest=manifest,
        conn=conn,
        collection=collection,
        bm25_index=bm25.build_from_sqlite(conn, manifest),
        embeddings=embeddings_module.client_for(manifest),
        translate_llm=llm.chat_model(manifest, "translate"),
        rerank_llm=llm.chat_model(manifest, "rerank"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evaluation.judge_eval", description=__doc__)
    parser.add_argument("command", choices=("build", "run"))
    parser.add_argument(
        "--manifest", default=get_settings().project_manifest, help="project.yaml to use"
    )
    parser.add_argument("--set", dest="set_path", type=Path, default=DEFAULT_SET_PATH)
    parser.add_argument("--out", dest="out_path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument(
        "--limit-per-class",
        type=int,
        default=DEFAULT_LIMIT_PER_CLASS,
        help="requirements per class; 0 for the whole set",
    )
    parser.add_argument(
        "--mode",
        choices=("sighted", "blind", "both"),
        default="both",
        help="sighted runs the product as it ships; blind withholds the labels",
    )
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    engine = open_engine(manifest)

    if args.command == "build":
        eval_set = build_set(engine)
        args.set_path.parent.mkdir(parents=True, exist_ok=True)
        args.set_path.write_text(
            json.dumps(eval_set.as_dict(), indent=2) + "\n", encoding="utf-8"
        )
        counts = eval_set.as_dict()["counts"]
        print(f"wrote {args.set_path}")
        print(
            f"  {counts['positives']} positive (@req only), "
            f"{counts['negatives']} negative (!req, never @req)"
        )
        print(
            f"  {counts['both_markers']} excluded — annotated both ways; "
            f"{counts['unresolved']} annotated ids resolve to no requirement "
            "(release drift, finding A4)"
        )
        return 0

    if not args.set_path.is_file():
        print(f"no eval set at {args.set_path}; run 'build' first", file=sys.stderr)
        return 1
    eval_set = EvalSet.from_dict(json.loads(args.set_path.read_text(encoding="utf-8")))
    if eval_set.git_sha != manifest.code.git_sha:
        print(
            f"the eval set was built against {eval_set.git_sha[:12]} but the manifest "
            f"pins {manifest.code.git_sha[:12]} — rebuild it before comparing numbers",
            file=sys.stderr,
        )
        return 1

    ceiling = report.ceiling_usd(manifest, get_settings())
    modes = ("sighted", "blind") if args.mode == "both" else (args.mode,)
    results = []
    for mode in modes:
        print(f"running the {mode} evaluation…", file=sys.stderr)
        results.append(
            evaluate(
                engine,
                llm.chat_model(manifest, "judge"),
                eval_set,
                limit_per_class=args.limit_per_class,
                ceiling=ceiling,
                blind=mode == "blind",
                on_progress=lambda done, total, current: print(
                    f"  {done}/{total} {current}", end="\r", file=sys.stderr
                ),
            )
        )
        print(file=sys.stderr)

    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    args.out_path.write_text(
        json.dumps(
            {
                "set": eval_set.as_dict()["counts"],
                "limit_per_class": args.limit_per_class,
                "results": [result.as_dict() for result in results],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out_path}", file=sys.stderr)
    print(to_markdown(eval_set, results))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
