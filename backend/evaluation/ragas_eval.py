"""RAGAS: the full retrieval pipeline against a naive top-k baseline (F7.2).

    uv run python -m evaluation.ragas_eval run      # spends money
    uv run python -m evaluation.ragas_eval table    # free, from the saved run

**What this measures, and what it does not.** The subject here is spec §4's
retrieval pipeline, not the chat agent. Each question is retrieved for twice —
once with everything on, once reduced to a single dense/BM25 top-k pass — and
an answer is generated from *only* what that arm retrieved. Two arms, one
generator, one metric set: the difference between the columns is the pipeline,
which is the claim story S7.2 exists to test.

The naive arm is not a strawman built for this script. ``search_requirements``
has documented since WP2 that ``rewrites=0, extract_filters=False,
use_rerank=False`` reduces it to the baseline, and that is exactly what is
passed — same code path, same fusion, same hydration, three stages switched
off.

**The four metrics split into two honest halves.**

* *Context precision / recall* are scored against ``reference``, the
  hand-written ground-truth answer in the golden set. They ask whether the
  right requirement text was retrieved at all — the question the pipeline is
  actually responsible for.
* *Faithfulness / answer relevancy* are scored on the generated answer.
  Faithfulness is the one that matters for this product's central claim: an
  answer that is not grounded in what was retrieved is exactly the failure
  source-linked citations exist to prevent.

**The golden set is hand-authored and committed** (``tests/fixtures/
ragas_golden_set.json``): 25 questions written from the requirement text in
the ingested corpus, spread across all four documents, each carrying the
requirement ids a correct retrieval must find. Generating the questions with a
model would have made this an eval of one model's reading by another's, and
re-deriving them per run would move the denominator between two runs that get
compared.

**ragas talks to OpenRouter like everything else** (CLAUDE.md): an
``AsyncOpenAI`` client pointed at the OpenRouter base URL, and the model ids
come from the manifest rather than from this file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from openai import AsyncOpenAI
from ragas.embeddings import OpenAIEmbeddings
from ragas.llms import llm_factory
from ragas.metrics.collections import (
    AnswerRelevancy,
    ContextPrecisionWithReference,
    ContextRecall,
    Faithfulness,
)

from core import llm
from core.config import get_settings
from core.llm import system, user
from core.manifest import ProjectManifest, load_manifest
from evaluation.judge_eval import open_engine
from retrieval import pipeline
from retrieval.pipeline import Engine
from retrieval.prompting import fence

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

DEFAULT_SET_PATH = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ragas_golden_set.json"
)
DEFAULT_RESULT_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "evaluations" / "ragas-eval.json"
)

#: How many requirements each arm retrieves. The same number for both, or the
#: comparison would be measuring the cut-off rather than the pipeline.
TOP_N = 5

#: The two arms. ``full`` is the product as it ships; ``naive`` is
#: ``search_requirements``' own documented baseline — one query, no inferred
#: filter, no rerank.
ARMS = {
    "naive": {"rewrites": 0, "extract_filters": False, "use_rerank": False},
    "full": {},
}

FENCE = "<<<RETRIEVED_REQUIREMENTS>>>"

ANSWER_PROMPT = """\
You are answering a question about AUTOSAR software specifications using only \
the retrieved passages provided. Answer in two or three sentences, in the \
specification's own terms.

Use nothing but the passages. If they do not contain the answer, say that the \
retrieved passages do not cover it — do not fall back on your own knowledge of \
AUTOSAR, because the point of this exercise is to measure what was retrieved.\
"""


@dataclass
class ItemScore:
    """One golden question, scored in one arm."""

    id: str
    arm: str
    doc: str
    retrieved_ids: list[str] = field(default_factory=list)
    hit: bool = False
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None
    answer: str = ""
    error: str | None = None


@dataclass
class ArmResult:
    arm: str
    scores: list[ItemScore] = field(default_factory=list)

    def mean(self, metric: str) -> float | None:
        values = [
            getattr(score, metric)
            for score in self.scores
            if getattr(score, metric) is not None
        ]
        return statistics.fmean(values) if values else None

    @property
    def hit_rate(self) -> float | None:
        if not self.scores:
            return None
        return sum(1 for score in self.scores if score.hit) / len(self.scores)


METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
LABELS = {
    "faithfulness": "Faithfulness",
    "answer_relevancy": "Answer relevancy",
    "context_precision": "Context precision",
    "context_recall": "Context recall",
}


def load_golden(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["items"]


# --------------------------------------------------------------------------
# one arm, one question
# --------------------------------------------------------------------------


def retrieve(engine: Engine, question: str, arm: str) -> tuple[list[str], list[str]]:
    """Run one arm's retrieval. Returns (context texts, requirement ids)."""
    result = pipeline.search_requirements(engine, question, top_n=TOP_N, **ARMS[arm])
    texts, ids = [], []
    for item in result.results:
        requirement = item.requirement
        ids.append(requirement.id)
        texts.append(f"[{requirement.id}] {requirement.text}")
    return texts, ids


def answer_from(answerer: llm.Llm, question: str, contexts: Sequence[str]) -> str:
    """Generate an answer from retrieved context only.

    The same generator for both arms, so the columns differ by retrieval and
    nothing else. Contexts are fenced like everywhere else in this codebase —
    retrieved text is untrusted input even inside an evaluation (spec §8).
    """
    if not contexts:
        return "The retrieved passages do not cover this question."
    body = fence(FENCE, "\n\n".join(contexts), what="retrieved requirements")
    completion = answerer.complete(
        [system(ANSWER_PROMPT), user(f"Question: {question}\n\n{body}")]
    )
    return completion.text.strip()


async def score_item(metrics: dict, item: dict, contexts: list[str], answer: str) -> dict:
    """The four ragas metrics for one answer, run concurrently."""
    question, reference = item["question"], item["reference"]

    async def guarded(name, coroutine):
        try:
            return name, (await coroutine).value
        except Exception as exc:  # noqa: BLE001 - one metric must not lose the row
            print(f"      {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return name, None

    pending = [
        guarded("faithfulness", metrics["faithfulness"].ascore(
            user_input=question, response=answer, retrieved_contexts=contexts)),
        guarded("answer_relevancy", metrics["answer_relevancy"].ascore(
            user_input=question, response=answer)),
        guarded("context_precision", metrics["context_precision"].ascore(
            user_input=question, reference=reference, retrieved_contexts=contexts)),
        guarded("context_recall", metrics["context_recall"].ascore(
            user_input=question, retrieved_contexts=contexts, reference=reference)),
    ]
    return dict(await asyncio.gather(*pending))


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def build_metrics(manifest: ProjectManifest, api_key: str) -> dict:
    """The ragas metric objects, pointed at OpenRouter.

    An async client because ``ascore`` refuses a synchronous one, and the
    evaluator model is the manifest's ``judge`` — the tool-less one, which is
    the right shape for something whose whole job is to assess text.
    """
    client = AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    evaluator = llm_factory(manifest.models.judge, provider="openai", client=client)
    embeddings = OpenAIEmbeddings(client=client, model=manifest.models.embedding)
    return {
        "faithfulness": Faithfulness(llm=evaluator),
        "answer_relevancy": AnswerRelevancy(llm=evaluator, embeddings=embeddings),
        "context_precision": ContextPrecisionWithReference(llm=evaluator),
        "context_recall": ContextRecall(llm=evaluator),
    }


def run_arm(
    engine: Engine, answerer: llm.Llm, metrics: dict, golden: Sequence[dict], arm: str
) -> ArmResult:
    result = ArmResult(arm=arm)
    for index, item in enumerate(golden, start=1):
        score = ItemScore(id=item["id"], arm=arm, doc=item["doc"])
        try:
            contexts, ids = retrieve(engine, item["question"], arm)
            score.retrieved_ids = ids
            score.hit = any(rid in ids for rid in item["reference_context_ids"])
            score.answer = answer_from(answerer, item["question"], contexts)
            values = asyncio.run(score_item(metrics, item, contexts, score.answer))
            for name, value in values.items():
                setattr(score, name, value)
        except Exception as exc:  # noqa: BLE001 - a failed row is reported, not fatal
            score.error = f"{type(exc).__name__}: {exc}"
        result.scores.append(score)
        mark = "·" if score.error else ("✓" if score.hit else "✗")
        print(f"  [{arm}] {index:>2}/{len(golden)} {mark} {item['id']}", file=sys.stderr)
    return result


def to_markdown(payload: dict) -> str:
    arms = {name: ArmResult(arm=name, scores=[ItemScore(**s) for s in scores])
            for name, scores in payload["arms"].items()}
    naive, full = arms.get("naive"), arms.get("full")

    lines = [
        f"Answers `{payload['answer_model_id']}` · metrics `{payload['evaluator_model_id']}` "
        f"· embeddings `{payload['embedding_model_id']}` · ragas {payload['ragas_version']} "
        f"· {payload['questions']} questions · top-{payload['top_n']}",
        "",
        "| metric | naive top-k | full pipeline | Δ |",
        "| --- | ---: | ---: | ---: |",
    ]
    for metric in METRICS:
        left = naive.mean(metric) if naive else None
        right = full.mean(metric) if full else None
        delta = ""
        if left is not None and right is not None:
            change = right - left
            delta = f"{change:+.3f}"
        lines.append(
            f"| {LABELS[metric]} | {left:.3f} | {right:.3f} | {delta} |"
            if left is not None and right is not None
            else f"| {LABELS[metric]} | — | — | — |"
        )
    if naive and full:
        lines.append(
            f"| Ground-truth hit rate | {naive.hit_rate:.3f} | {full.hit_rate:.3f} | "
            f"{full.hit_rate - naive.hit_rate:+.3f} |"
        )
    lines += [
        "",
        "*Naive top-k* is `search_requirements` with `rewrites=0, "
        "extract_filters=False, use_rerank=False` — the same code path with "
        "multi-query expansion, self-query filtering and LLM rerank switched "
        "off. Both arms retrieve the same number of passages and use the same "
        "generator, so the columns differ by retrieval alone.",
        "",
        "*Hit rate* is not a ragas metric: it is the plain fraction of "
        "questions whose hand-labelled requirement id appeared in what was "
        "retrieved. It is here because it needs no model to compute and cannot "
        "be argued with.",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evaluation.ragas_eval", description=__doc__)
    parser.add_argument("command", choices=("run", "table"))
    parser.add_argument("--manifest", default=get_settings().project_manifest)
    parser.add_argument("--set", dest="set_path", type=Path, default=DEFAULT_SET_PATH)
    parser.add_argument("--out", dest="out_path", type=Path, default=DEFAULT_RESULT_PATH)
    parser.add_argument(
        "--limit", type=int, default=0, help="questions per arm; 0 for all of them"
    )
    args = parser.parse_args(argv)

    if args.command == "table":
        if not args.out_path.is_file():
            print(f"no saved run at {args.out_path}; run 'run' first", file=sys.stderr)
            return 1
        print(to_markdown(json.loads(args.out_path.read_text(encoding="utf-8"))))
        return 0

    settings = get_settings()
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set — this eval calls real models", file=sys.stderr)
        return 1

    manifest = load_manifest(args.manifest)
    golden = load_golden(args.set_path)
    if args.limit:
        golden = golden[: args.limit]

    engine = open_engine(manifest)
    answerer = llm.chat_model(manifest, "chat")
    metrics = build_metrics(manifest, settings.openrouter_api_key)

    import ragas

    payload = {
        "answer_model_id": answerer.model_id,
        "evaluator_model_id": manifest.models.judge,
        "embedding_model_id": manifest.models.embedding,
        "ragas_version": ragas.__version__,
        "corpus_version": manifest.version,
        "questions": len(golden),
        "top_n": TOP_N,
        "arms": {},
    }
    for arm in ARMS:
        result = run_arm(engine, answerer, metrics, golden, arm)
        payload["arms"][arm] = [asdict(score) for score in result.scores]

    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    args.out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print()
    print(to_markdown(payload))
    print(f"\nwrote {args.out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
