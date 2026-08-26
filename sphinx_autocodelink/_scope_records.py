"""Turn traced scopes and call sites into records, retaining nothing that was traced.

:mod:`sphinx_autocodelink._tracing` reports two kinds of observation about a
running example: a scope (a function frame of the example's own code, with its
real locals) and a call site (with the callable the interpreter actually
invoked). Both are turned into plain string records here, inside the callback,
so nothing the example built -- a plotter, a mesh, anything holding a native
resource -- is still referenced once the callback returns.

Between them the two cover what an example's top-level namespace alone cannot:
a scope resolves the identifiers of a helper function against that helper's own
locals, and an observed call resolves a receiver no dotted name can address
(``dataset['label_map'].contour_labels()``) against the real callable.
"""

from __future__ import annotations

import ast
import copy
from typing import TYPE_CHECKING
from typing import Any

from sphinx_autocodelink import _call_chain_candidates
from sphinx_autocodelink import _Candidate
from sphinx_autocodelink import _candidate_names
from sphinx_autocodelink import _candidates_for_callable
from sphinx_autocodelink import _dotted_name
from sphinx_autocodelink import _ExprCandidate
from sphinx_autocodelink import _records_for

if TYPE_CHECKING:
    from types import CodeType
    from types import FrameType

    from sphinx_autocodelink import _Record

#: Most records one traced example may contribute, as a backstop against a
#: pathological example (a deep recursion of distinct scopes, a generated
#: script) growing this without bound.
MAX_RECORDS = 5000


class _PruneNested(ast.NodeTransformer):
    """Replace nested definitions with a no-op: each runs in its own traced scope."""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        """Drop a nested ``def``, without descending into it."""
        return ast.Pass()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        """Drop a nested ``async def``, without descending into it."""
        return ast.Pass()

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        """Drop a nested class, without descending into it."""
        return ast.Pass()

    def visit_Lambda(self, node: ast.Lambda) -> ast.AST:
        """Drop a nested ``lambda``, without descending into it."""
        return ast.Constant(value=None)


class _SourceIndex:
    """One traced file's parsed source, indexed by what the tracer reports about it."""

    def __init__(self, source: str) -> None:
        """Index every definition by name and line, and every call by its exact position."""
        self.source = source
        self.functions: dict[tuple[str, int], ast.AST] = {}
        self.calls: dict[tuple[int, int, int, int], ast.Call] = {}
        #: Calls per start line, for the fallback below when an exact position misses.
        self.calls_by_line: dict[int, list[ast.Call]] = {}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                # A decorated function's code object reports the first decorator's
                # line as its own, not the `def`'s -- so both are keys here.
                for lineno in {node.lineno, *(d.lineno for d in node.decorator_list)}:
                    self.functions.setdefault((node.name, lineno), node)
            elif isinstance(node, ast.Lambda):
                self.functions.setdefault(('<lambda>', node.lineno), node)
            elif isinstance(node, ast.Call) and node.end_lineno is not None:
                key = (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset or 0)
                self.calls[key] = node
                self.calls_by_line.setdefault(node.lineno, []).append(node)


#: Parsed source per traced filename, and instruction positions per traced code object.
#: Both are cleared between examples (:func:`clear_caches`); code objects hold no
#: example data, only constants, so caching them for one example retains nothing live.
_INDEXES: dict[str, _SourceIndex | None] = {}
_POSITIONS: dict[CodeType, list[Any]] = {}


def clear_caches() -> None:
    """Drop everything cached for the example that just finished."""
    _INDEXES.clear()
    _POSITIONS.clear()


def _index_for(filename: str) -> _SourceIndex | None:
    """Return ``filename``'s parsed index, or ``None`` if it can't be read or parsed."""
    if filename not in _INDEXES:
        try:
            with open(filename, encoding='utf-8') as file:
                index: _SourceIndex | None = _SourceIndex(file.read())
        except (OSError, SyntaxError, ValueError):
            index = None
        _INDEXES[filename] = index
    return _INDEXES[filename]


def _scope_source(node: ast.AST) -> str | None:
    """Return just the statements that run in ``node``'s own scope, as source.

    Nested definitions are pruned rather than included: each gets its own scope
    reported separately, so leaving them in would resolve their identifiers a
    second time -- against the wrong namespace, and double-counting whatever
    did resolve in both.
    """
    if isinstance(node, ast.Lambda):
        body: list[ast.stmt] = [ast.Expr(value=node.body)]
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        body = list(node.body)
    else:  # pragma: no cover -- nothing else is indexed as a scope
        return None
    pruned = [_PruneNested().visit(copy.deepcopy(statement)) for statement in body]
    try:
        return ast.unparse(ast.Module(body=pruned, type_ignores=[]))
    except Exception:  # noqa: BLE001 -- an unparseable scope simply isn't recordable
        return None


def scope_records(code: CodeType, frame: FrameType) -> list[_Record]:
    """Return records for one returning frame, resolved against that frame's own scope.

    The module scope is skipped: Sphinx-Gallery's own per-block recording
    already covers it, against the same globals.
    """
    if code.co_name == '<module>':
        return []
    index = _index_for(code.co_filename)
    if index is None:
        return []
    node = index.functions.get((code.co_name, code.co_firstlineno))
    if node is None:
        return []
    source = _scope_source(node)
    if source is None:
        return []
    return _records_for(source, {**frame.f_globals, **frame.f_locals})


def _call_node(code: CodeType, instruction_offset: int) -> tuple[_SourceIndex, ast.Call] | None:
    """Return the ``ast.Call`` at ``instruction_offset``, by exact source position."""
    index = _index_for(code.co_filename)
    if index is None:
        return None
    positions = _POSITIONS.get(code)
    if positions is None:
        # co_positions() is 3.11+; nothing calls this below 3.12 anyway (no sys.monitoring).
        get_positions = getattr(code, 'co_positions', None)
        if get_positions is None:  # pragma: no cover -- Python 3.10 only
            return None
        positions = _POSITIONS[code] = list(get_positions())
    try:
        lineno, end_lineno, col, end_col = positions[instruction_offset // 2]
    except (IndexError, TypeError, ValueError):
        return None
    if lineno is None or end_lineno is None:
        return None
    node = index.calls.get((lineno, col or 0, end_lineno, end_col or 0))
    if node is None:
        # Column offsets are reported per instruction, which future bytecode changes
        # could shift; a line with exactly one call on it is unambiguous without them.
        on_line = index.calls_by_line.get(lineno, ())
        node = on_line[0] if len(on_line) == 1 else None
    return None if node is None else (index, node)


def _call_chain(node: ast.expr) -> tuple[str, tuple[str, ...], ast.Call] | None:
    """Return ``(call target, trailing attrs, the call)`` for ``pv.Sphere().plot``."""
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if not isinstance(cursor, ast.Call):
        return None
    target = _dotted_name(cursor.func)
    return None if target is None else (target, tuple(reversed(parts)), cursor)


def call_records(
    code: CodeType, instruction_offset: int, func: Any, frame: FrameType
) -> list[_Record]:
    """Return records for one observed call site, from the callable actually invoked.

    A call the source-and-namespace resolution already reaches is left alone --
    a receiver addressable by a dotted name in the calling frame, or a chain off
    a call whose return type resolves. The scope (or block) recording covering
    that frame produces the same record, and recording it here as well would
    count one use twice.
    """
    found = _call_node(code, instruction_offset)
    if found is None:
        return []
    index, node = found
    candidates = _candidates_for_callable(func)
    if not candidates:
        return []
    namespace = {**frame.f_globals, **frame.f_locals}
    dotted = _dotted_name(node.func)
    if dotted is not None:
        if _candidate_names(dotted, namespace):
            return []
        return [_Candidate(dotted, tuple(candidates))]
    if not isinstance(node.func, ast.Attribute):
        return []
    chain = _call_chain(node.func)
    if chain is not None and _call_chain_candidates(chain[0], chain[1], namespace, [chain[2]]):
        return []
    expr = ast.get_source_segment(index.source, node.func)
    return [] if expr is None else [_ExprCandidate(expr, tuple(candidates))]


class ExampleRecorder:
    """Accumulates one example's traced records, as strings, for the scraper to drain."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.records: list[_Record] = []

    def on_scope(self, code: CodeType, frame: FrameType) -> None:
        """Record everything the returning frame's own scope resolves."""
        self._add(scope_records(code, frame))

    def on_call(self, code: CodeType, instruction_offset: int, func: Any, frame: FrameType) -> None:
        """Record what the call site resolves that no name in scope could."""
        self._add(call_records(code, instruction_offset, func, frame))

    def _add(self, records: list[_Record]) -> None:
        """Append ``records``, up to :data:`MAX_RECORDS`."""
        if len(self.records) < MAX_RECORDS:
            self.records.extend(records)

    def drain(self) -> list[_Record]:
        """Return and clear everything recorded since the last drain."""
        records, self.records = self.records, []
        return records
