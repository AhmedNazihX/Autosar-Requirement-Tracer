"""Chunk C source into :class:`~core.models.CodeUnit`s with tree-sitter (S1.4.2).

One unit per top-level definition — function, prototype, struct, union, enum,
typedef, macro — each with a 1-based inclusive ``line_span`` and a breadcrumb
header (``// CanIf.c > CanIf_Transmit``) prefixed to its text, per spec §4. The
annotations found by :mod:`ingestion.annotations` are attached to the unit that
encloses them, so tier-1 evidence (spec §5) can name a file and a line.

Decisions worth knowing about, all of them load-bearing:

* **A declaration is not an implementation.** A header prototype is
  ``kind="prototype"``, never ``"function"``, because the evidence judge
  decides whether a requirement is implemented and 301 of this corpus's 641
  function-shaped units only declare. Annotations that live only on a
  prototype must be weighable as the weak evidence they are.
* **Annotations belonging to no definition become ``kind="file"`` units**
  rather than borrowing another kind's name — ``kind`` is part of the storage
  key, so a mislabel corrected later would orphan both the rows and their
  cached embeddings.

* **Identity must be stable across runs.** ``code_units`` is keyed on
  ``(project_id, repo_path, kind, symbol, line_span_start, line_span_end)``
  (ruling R16) and the embedding cache is keyed on content, so a ``kind``,
  span or text that wobbles between two runs over the same commit re-embeds
  the corpus. Everything here is derived from the parse tree and the file
  bytes with no dict-ordering or set-iteration in the path, and units are
  returned in a total order; ``test_code_indexer.py`` asserts a second index
  of the same bytes is identical.
* **Symbols are not unique within a file** (ruling R16): a named struct and
  the typedef that names it legitimately produce two units with the same
  symbol string. ``kind`` and ``line_span`` are what separate them, so
  ``list_code_units_by_symbol`` (which returns a *list*) is the lookup to
  prefer downstream.
* **The name comes from the declarator, never from a descendant walk.** For
  ``typedef struct { uint8 a; } Can_PduType;`` the first ``identifier``
  descendant is ``uint8`` — the first field's *type*. A mis-named unit is
  worse than a missing one because it poisons symbol-anchored retrieval with
  a plausible-looking wrong answer.
* **An anonymous struct/enum/union inside a typedef yields one unit**, the
  typedef, since the record has no name of its own and the typedef's span
  covers the same lines; emitting both would duplicate every one of the 134
  such records in this corpus, and with it their embeddings and their
  retrieval hits. A *named* record with a body gets its own unit as well as
  any typedef naming it, because code refers to ``struct Fixture_Global``
  under that name.
* **Recovery, not abortion.** AUTOSAR C is full of ``#if`` arms with
  unbalanced braces, so tree-sitter produces ERROR nodes over large regions
  (in this corpus, 6 of 83 files, including 890 lines of ``CanIf.c``). The
  walk descends *into* ERROR nodes and keeps every unit it can still
  identify; the count is reported rather than raised.
* **Trivial units are skipped unless annotated.** "Trivial" means an *empty
  body* — an ``{}`` function, a valueless ``#define`` include guard — rather
  than a size threshold, which would throw away real one-line accessors. A
  unit carrying an annotation is never skipped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import tree_sitter_c
from tree_sitter import Language, Node, Parser

from core.manifest import CodeConfig, ProjectManifest
from core.models import CodeUnit, CodeUnitKind, ReqAnnotation
from ingestion.annotations import ScanResult, scan_annotations
from ingestion.code_fetcher import ProgressCallback, select_files

#: ``// <file> > <symbol>`` — the chunk header the spec (§4) requires.
BREADCRUMB_TEMPLATE = "// {file} > {symbol}"

#: Kind for a unit that only *declares* a function. This is a first-class
#: ``CodeUnitKind``, not a note in the text: the evidence judge (spec §5)
#: decides whether a requirement is *implemented*, and a header prototype is
#: not an implementation — 301 of this corpus's 641 function-shaped units are
#: declarations, so an annotation found only on a prototype must be
#: distinguishable from one found on a body without string-matching prose.
PROTOTYPE_KIND: CodeUnitKind = "prototype"

#: Kind for a unit that *defines* a function — the counterpart of
#: :data:`PROTOTYPE_KIND` and the one kind that counts as an implementation.
#: Named for the same reason: callers that compare against it (the vector-store
#: filter in ``ingestion.run``, for one) must not spell the string themselves.
FUNCTION_KIND: CodeUnitKind = "function"

#: Breadcrumb suffix for a prototype. Redundant with ``kind`` on purpose — the
#: breadcrumb is embedded, so it is what tells the retriever and the reranker
#: that this chunk is a declaration.
DECLARATION_SUFFIX = " (declaration)"

#: Symbol shown for a file-scope unit; the file's own name is the symbol.
FILE_SCOPE_SUFFIX = " (file scope)"

#: Kind for a file-scope unit holding annotations that belong to no definition
#: (a file-header ``@req`` block, an annotation on a global).
FILE_UNIT_KIND: CodeUnitKind = "file"

#: An annotation up to this many lines *above* a unit's (comment-extended)
#: start still belongs to that unit. Measured over the real corpus: 3 lines
#: attaches 80 annotations that containment alone misses, and going wider adds
#: little but risk of stealing a neighbour's annotation.
ATTACH_GAP_LINES = 3

#: File-scope annotations further apart than this start a new file-scope unit,
#: so one stray annotation deep in a file does not drag a whole file's text
#: into a single chunk.
FILE_CLUSTER_GAP_LINES = 5

#: tree-sitter node types that become a unit, and the kind they map to. A
#: ``declaration`` is a prototype, a record definition or a variable depending
#: on its children, so it is resolved in :func:`_emissions` instead.
_RECORD_KINDS: dict[str, CodeUnitKind] = {
    "struct_specifier": "struct",
    "enum_specifier": "enum",
    # A union gets its own kind rather than being folded into "struct": the two
    # in this corpus are anonymous and covered by their typedef, so folding
    # would be invisible today and a silent mislabel the moment a named union
    # appears. `kind` is part of the storage key, so a label corrected later
    # orphans rows and embeddings.
    "union_specifier": "union",
}

_DEFINITION_TYPES = frozenset(
    {
        "function_definition",
        "declaration",
        "type_definition",
        "preproc_def",
        "preproc_function_def",
        *_RECORD_KINDS,
    }
)

#: Declarator wrappers to descend through when looking for a name.
_DECLARATOR_TYPES = frozenset(
    {
        "function_declarator",
        "pointer_declarator",
        "parenthesized_declarator",
        "array_declarator",
        "init_declarator",
        "attributed_declarator",
    }
)

_NAME_TYPES = frozenset({"identifier", "type_identifier", "field_identifier"})


@lru_cache(maxsize=1)
def c_parser() -> Parser:
    """A cached tree-sitter parser for C.

    The language comes from the ``tree_sitter_c`` module —
    ``Language.build_library`` was removed from the modern bindings.

    .. warning::
       One process-wide ``Parser`` instance, and a ``Parser`` is **not**
       thread-safe: ``parse()`` mutates parser state, so two threads sharing
       this object can corrupt each other's trees. Indexing is single-threaded
       today (:func:`index_repo` is a plain loop). If D6 parallelizes it, give
       each worker its own parser — ``Parser(Language(tree_sitter_c.language()))``
       is cheap — rather than sharing this one.
    """
    return Parser(Language(tree_sitter_c.language()))


@dataclass(frozen=True)
class FileIndexResult:
    """Units and diagnostics for one source file."""

    repo_path: str
    units: list[CodeUnit] = field(default_factory=list)
    annotations: int = 0
    claimed: int = 0
    not_claimed: int = 0
    file_scope_annotations: int = 0
    skipped_trivial: int = 0
    #: ERROR/MISSING nodes met on the walk, i.e. damage *between* definitions.
    error_nodes: int = 0
    #: Whether tree-sitter reported damage anywhere in the file, including
    #: inside a function body the walk never descends into. This is the
    #: honest "partial parse" signal; ``error_nodes`` is the narrower count of
    #: regions the walk had to recover units out of.
    has_error: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        """True when tree-sitter could not fully parse the file."""
        return self.has_error


@dataclass(frozen=True)
class RepoIndexResult:
    """Everything :func:`index_repo` produced, plus roll-up counters."""

    files: list[FileIndexResult] = field(default_factory=list)

    @property
    def units(self) -> list[CodeUnit]:
        return [unit for result in self.files for unit in result.units]

    @property
    def annotations(self) -> int:
        return sum(r.annotations for r in self.files)

    @property
    def claimed(self) -> int:
        return sum(r.claimed for r in self.files)

    @property
    def not_claimed(self) -> int:
        return sum(r.not_claimed for r in self.files)

    @property
    def file_scope_annotations(self) -> int:
        return sum(r.file_scope_annotations for r in self.files)

    @property
    def partial_files(self) -> list[str]:
        return [r.repo_path for r in self.files if r.partial]

    @property
    def warnings(self) -> list[str]:
        return [w for r in self.files for w in r.warnings]

    @property
    def canonical_ids(self) -> list[str]:
        """Every canonical id any unit's annotations mention, deduplicated."""
        seen: dict[str, None] = {}
        for unit in self.units:
            for annotation in unit.req_annotations:
                seen.setdefault(annotation.canonical_id, None)
        return list(seen)


# --------------------------------------------------------------------------
# names and spans
# --------------------------------------------------------------------------


def declarator_name(node: Node) -> str | None:
    """The name a declarator declares, descending only the declarator chain.

    Returns ``None`` rather than guessing when the chain ends in something
    unnamed (an abstract declarator, a macro-mangled declarator). The caller
    then skips the unit — a nameless unit is better than a wrongly named one.
    """
    current: Node | None = node
    while current is not None:
        if current.type in _NAME_TYPES:
            return current.text.decode("utf-8", "replace") if current.text else None
        if current.type not in _DECLARATOR_TYPES:
            return None
        inner = current.child_by_field_name("declarator")
        if inner is None and current.type == "parenthesized_declarator":
            # `typedef void (*Fn)(int)`: the parentheses wrap the real
            # declarator without naming it as a field.
            inner = next(iter(current.named_children), None)
        current = inner
    return None


def _field_name(node: Node, field_name: str = "name") -> str | None:
    child = node.child_by_field_name(field_name)
    if child is None or child.text is None:
        return None
    return child.text.decode("utf-8", "replace")


def _has_body(node: Node) -> bool:
    return node.child_by_field_name("body") is not None


def _named_records(node: Node) -> list[Node]:
    """Direct struct/enum/union children of ``node`` that define a *named* record."""
    return [
        child
        for child in node.named_children
        if child.type in _RECORD_KINDS and _has_body(child) and _field_name(child) is not None
    ]


def _is_function_declaration(node: Node) -> bool:
    declarator = node.child_by_field_name("declarator")
    while declarator is not None:
        if declarator.type == "function_declarator":
            return True
        if declarator.type not in _DECLARATOR_TYPES:
            return False
        declarator = declarator.child_by_field_name("declarator")
    return False


@dataclass(frozen=True)
class _Emission:
    """One prospective unit: what to call it, and which node delimits it."""

    kind: CodeUnitKind
    symbol: str
    node: Node

    @property
    def declaration(self) -> bool:
        """True for a unit that only declares. Derived from ``kind``, not stored.

        Keeping this a property rather than a flag means the breadcrumb suffix
        and the persisted ``kind`` cannot drift apart.
        """
        return self.kind == PROTOTYPE_KIND


def _emissions(node: Node) -> list[_Emission]:
    """The units ``node`` yields, in source order. Empty for anything ignored."""
    out: list[_Emission] = []

    if node.type == "function_definition":
        name = declarator_name(node.child_by_field_name("declarator"))
        if name:
            out.append(_Emission(FUNCTION_KIND, name, node))
        return out

    if node.type in ("preproc_def", "preproc_function_def"):
        name = _field_name(node)
        if name:
            out.append(_Emission("macro", name, node))
        return out

    if node.type in _RECORD_KINDS:
        name = _field_name(node)
        if name and _has_body(node):
            out.append(_Emission(_RECORD_KINDS[node.type], name, node))
        return out

    if node.type == "type_definition":
        # A named record inside the typedef is a definition in its own right
        # (`typedef struct Foo { ... } FooType;` defines `struct Foo` too).
        for record in _named_records(node):
            name = _field_name(record)
            if name:
                out.append(_Emission(_RECORD_KINDS[record.type], name, record))
        for declarator in node.children_by_field_name("declarator"):
            name = declarator_name(declarator)
            if name:
                out.append(_Emission("typedef", name, node))
        return out

    if node.type == "declaration":
        for record in _named_records(node):
            name = _field_name(record)
            if name:
                out.append(_Emission(_RECORD_KINDS[record.type], name, record))
        if _is_function_declaration(node):
            name = declarator_name(node.child_by_field_name("declarator"))
            if name:
                out.append(_Emission(PROTOTYPE_KIND, name, node))
        # A plain variable declaration has no CodeUnitKind and is not indexed;
        # any annotation on it falls through to a file-scope unit rather than
        # being dropped.
        return out

    return out


def _collect(node: Node, out: list[Node], errors: list[Node]) -> None:
    """Walk ``node``, collecting definition nodes and counting ERROR nodes.

    Definition nodes are not descended into (a function's locals are part of
    the function). Everything else *is* descended into, which is what finds
    definitions inside ``#if`` blocks and recovers them from ERROR regions.
    """
    for child in node.children:
        if child.type == "ERROR" or child.is_missing:
            errors.append(child)
            _collect(child, out, errors)
            continue
        if child.type in _DEFINITION_TYPES:
            out.append(child)
            continue
        _collect(child, out, errors)


def _attached_comment_start(node: Node) -> int:
    """1-based start line of ``node``, extended over a directly attached comment.

    A comment block immediately above a definition (no blank line between) is
    part of it: that is where a large share of the annotations live, and
    separating them from the code they describe would hurt both retrieval and
    the judge's ability to reason about the evidence.
    """
    start = node.start_point[0] + 1
    previous = node.prev_sibling
    while (
        previous is not None
        and previous.type == "comment"
        and previous.end_point[0] + 1 >= start - 1
    ):
        start = previous.start_point[0] + 1
        previous = previous.prev_sibling
    return start


def _end_line(node: Node) -> int:
    """1-based last line of ``node``, not counting a trailing newline.

    A preprocessor directive's node ends at column 0 of the *following* line,
    so taking ``end_point.row`` verbatim would stretch every ``#define`` over
    a line it does not occupy — and ``line_span`` is what the code viewer
    highlights.
    """
    row = node.end_point[0]
    if node.end_point[1] == 0 and row > node.start_point[0]:
        row -= 1
    return row + 1


def _body_is_empty(node: Node) -> bool:
    """True for a definition with nothing in it (``{}``, a valueless #define)."""
    if node.type in ("function_definition", "struct_specifier", "enum_specifier"):
        body = node.child_by_field_name("body")
        if body is None:
            return True
        return not [child for child in body.named_children if child.type != "comment"]
    if node.type == "preproc_def":
        value = node.child_by_field_name("value")
        return value is None or not (value.text or b"").strip()
    return False


def breadcrumb(repo_path: str, symbol: str, *, suffix: str = "") -> str:
    """``// <file> > <symbol>`` — the per-chunk header from spec §4."""
    return BREADCRUMB_TEMPLATE.format(file=Path(repo_path).name, symbol=symbol) + suffix


def _unit_text(lines: Sequence[str], span: tuple[int, int], header: str) -> str:
    body = "\n".join(lines[span[0] - 1 : span[1]])
    return f"{header}\n{body}"


# --------------------------------------------------------------------------
# indexing
# --------------------------------------------------------------------------


def _attach(
    annotations: Iterable[ReqAnnotation],
    spans: Sequence[tuple[int, int]],
) -> tuple[dict[int, list[ReqAnnotation]], list[ReqAnnotation]]:
    """Assign each annotation to a span index, or to the file-scope leftovers.

    Containment wins, innermost first (a named record nested in a typedef is a
    tighter home than the typedef). Failing that, an annotation sitting up to
    :data:`ATTACH_GAP_LINES` above a span belongs to it. Anything left over is
    returned separately — never dropped.
    """
    attached: dict[int, list[ReqAnnotation]] = {}
    leftovers: list[ReqAnnotation] = []
    for annotation in annotations:
        line = annotation.line
        containing = [i for i, (start, end) in enumerate(spans) if start <= line <= end]
        if containing:
            index = min(containing, key=lambda i: (spans[i][1] - spans[i][0], spans[i][0], i))
        else:
            following = [
                i for i, (start, _) in enumerate(spans) if 0 < start - line <= ATTACH_GAP_LINES
            ]
            if not following:
                leftovers.append(annotation)
                continue
            index = min(following, key=lambda i: (spans[i][0], i))
        attached.setdefault(index, []).append(annotation)
    return attached, leftovers


def _cluster(annotations: Sequence[ReqAnnotation]) -> list[list[ReqAnnotation]]:
    """Group file-scope annotations into runs of nearby lines."""
    clusters: list[list[ReqAnnotation]] = []
    for annotation in annotations:
        if clusters and annotation.line - clusters[-1][-1].line <= FILE_CLUSTER_GAP_LINES:
            clusters[-1].append(annotation)
        else:
            clusters.append([annotation])
    return clusters


def index_source(
    text: str,
    repo_path: str,
    *,
    code: CodeConfig,
    git_sha: str,
    project_id: str,
    scan: ScanResult | None = None,
) -> FileIndexResult:
    """Parse one file's source into units with their annotations attached.

    ``repo_path`` is the repo-relative path recorded on every unit (and the
    file name shown in its breadcrumb). ``scan`` is only for tests that want
    to inject annotations; by default the text is scanned here.
    """
    # `split("\n")`, never `splitlines()`: the latter also breaks on \f, \v,
    # \x1c-\x1e, \x85 and the Unicode separators, while tree-sitter's rows and
    # `annotations.line_offsets` count only \n. A form feed — routine page-break
    # punctuation in old C — would otherwise shift every span below it, which is
    # exactly the silent kind of defect a code viewer makes obvious and a test
    # does not.
    lines = text.split("\n")
    tree = c_parser().parse(text.encode("utf-8"))

    definitions: list[Node] = []
    errors: list[Node] = []
    _collect(tree.root_node, definitions, errors)

    # (span, kind, symbol, declaration, node) for every prospective unit,
    # deduplicated on the storage key and put in a total order.
    prospects: dict[tuple[int, int, str, str], tuple[_Emission, tuple[int, int]]] = {}
    for definition in definitions:
        for emission in _emissions(definition):
            if emission.node is definition:
                span = (_attached_comment_start(definition), _end_line(definition))
            else:
                span = (emission.node.start_point[0] + 1, _end_line(emission.node))
            key = (span[0], span[1], emission.kind, emission.symbol)
            prospects.setdefault(key, (emission, span))

    ordered = sorted(prospects.items(), key=lambda item: item[0])
    spans = [span for _, (_, span) in ordered]

    result = scan if scan is not None else scan_annotations(text, code, source=repo_path)
    attached, leftovers = _attach(result.annotations, spans)

    units: list[CodeUnit] = []
    skipped = 0
    for index, (_, (emission, span)) in enumerate(ordered):
        annotations = attached.get(index, [])
        if not annotations and _body_is_empty(emission.node):
            skipped += 1
            continue
        suffix = DECLARATION_SUFFIX if emission.declaration else ""
        units.append(
            CodeUnit(
                repo_path=repo_path,
                language=code.language,
                symbol=emission.symbol,
                kind=emission.kind,
                line_span=span,
                text=_unit_text(lines, span, breadcrumb(repo_path, emission.symbol, suffix=suffix)),
                req_annotations=annotations,
                git_sha=git_sha,
                project_id=project_id,
            )
        )

    file_symbol = Path(repo_path).name
    for cluster in _cluster(leftovers):
        span = (cluster[0].line, cluster[-1].line)
        units.append(
            CodeUnit(
                repo_path=repo_path,
                language=code.language,
                symbol=file_symbol,
                kind=FILE_UNIT_KIND,
                line_span=span,
                text=_unit_text(
                    lines, span, breadcrumb(repo_path, file_symbol, suffix=FILE_SCOPE_SUFFIX)
                ),
                req_annotations=list(cluster),
                git_sha=git_sha,
                project_id=project_id,
            )
        )

    units.sort(key=lambda u: (u.line_span[0], u.line_span[1], u.kind, u.symbol))
    return FileIndexResult(
        repo_path=repo_path,
        units=units,
        annotations=len(result.annotations),
        claimed=len(result.claimed),
        not_claimed=len(result.not_claimed),
        file_scope_annotations=len(leftovers),
        skipped_trivial=skipped,
        error_nodes=len(errors),
        has_error=tree.root_node.has_error,
        warnings=list(result.warnings),
    )


def index_file(
    path: Path,
    root: Path,
    *,
    code: CodeConfig,
    git_sha: str,
    project_id: str,
) -> FileIndexResult:
    """Index one file on disk, recording its path relative to ``root``.

    Undecodable bytes are replaced rather than fatal: a single non-UTF-8 byte
    in a comment must not cost the file's units.
    """
    text = path.read_bytes().decode("utf-8", "replace")
    return index_source(
        text,
        path.relative_to(root).as_posix(),
        code=code,
        git_sha=git_sha,
        project_id=project_id,
    )


def index_repo(
    manifest: ProjectManifest,
    root: Path,
    *,
    files: Sequence[Path] | None = None,
    on_progress: ProgressCallback | None = None,
    on_file: Callable[[FileIndexResult], None] | None = None,
) -> RepoIndexResult:
    """Index every in-scope file of a checked-out snapshot.

    ``files`` defaults to :func:`ingestion.code_fetcher.select_files`, i.e.
    exactly what the manifest's globs select. ``git_sha`` is the manifest's
    pinned SHA — :func:`~ingestion.code_fetcher.fetch_repo` has already proved
    the work tree is at that commit.
    """
    selected = list(files) if files is not None else select_files(root, manifest.code)
    results: list[FileIndexResult] = []
    for done, path in enumerate(selected, start=1):
        result = index_file(
            path,
            root,
            code=manifest.code,
            git_sha=manifest.code.git_sha,
            project_id=manifest.project_id,
        )
        results.append(result)
        if on_file is not None:
            on_file(result)
        if on_progress is not None:
            on_progress(result.repo_path, done, len(selected))
    return RepoIndexResult(files=results)
