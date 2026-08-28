"""Dynamic hyperlinking of identifiers in already-executed Sphinx code output.

Resolves each identifier against the real namespace it executed in, rather than
inferring its type statically. See the README for configuration and usage.
"""

from __future__ import annotations

import ast
import builtins
from collections import Counter
from dataclasses import dataclass
import doctest
from html import escape
from html import unescape
import inspect
import json
from pathlib import Path
import re
import shutil
import sys
from typing import TYPE_CHECKING
from typing import Any

# typing.get_overloads was added in 3.11.
try:
    from typing import get_overloads
except ImportError:  # Python 3.10
    get_overloads = None

from docutils import nodes
from sphinx import addnodes
from sphinx.util import logging as sphinx_logging

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Sequence
    from types import CodeType

    from docutils.parsers.rst.states import RSTState
    from sphinx.application import Sphinx
    from sphinx.environment import BuildEnvironment

_logger = sphinx_logging.getLogger(__name__)

#: ``env`` attribute holding recorded candidates, keyed by docname.
_ENV_ATTR = 'sphinx_autocodelink_records'

#: ``env`` attribute holding the set of docnames hosting an ``.. autocodelink-index::``.
_INDEX_DOCS_ATTR = 'sphinx_autocodelink_index_docs'

#: ``env`` attribute holding each docname's recording category (e.g. ``'Sphinx Gallery'``),
#: for grouping backreferences by where they came from. Untagged docnames have no entry.
_CATEGORY_ATTR = 'sphinx_autocodelink_categories'

#: Per docname, the section anchor each documented name was recorded under.
_ANCHOR_ATTR = 'sphinx_autocodelink_anchors'

#: Display label for a referencing page with no recorded category.
_UNCATEGORIZED_LABEL = 'Documentation'

#: Matches any anchor tag, ours or another extension's.
_ANCHOR_RE = re.compile(r'<a\b[^>]*>.*?</a>', re.DOTALL)

#: Markup a real ``:class:``/``:func:`` cross-reference renders with.
_XREF_OPEN = '<code class="xref py py-obj docutils literal notranslate">'
_XREF_CLOSE = '</code>'

#: Markup a real ``:ref:`` role renders with, bolded explicitly since no theme styles a
#: bare ``<span>``.
_STD_REF_OPEN = '<span class="std std-ref" style="font-weight: bold;">'
_STD_REF_CLOSE = '</span>'

# Pygments token classes: ``n``/``nn``/``nc``/... for names, ``o`` for dots.
_NAME_SPAN = '<span class="n[a-zA-Z]{{0,2}}">{}</span>'
_DOT_SPAN = '<span class="o">.</span>'

#: A call's closing paren: ``)``, or merged ``()`` for a no-arg call.
_CALL_END = r'<span class="p">\(?\)</span>'

#: Replacement making a Pygments name-token class in an escaped fragment match any of them.
_LOOSE_NAME_CLASS = 'class="n[a-zA-Z]{0,2}"'


def _dotted_span_source(parts: tuple[str, ...]) -> str:
    """Build a regex source matching how Pygments is likely to render a dotted chain."""
    return _DOT_SPAN.join(_NAME_SPAN.format(re.escape(part)) for part in parts)


def _name_pattern_source(accessed: str) -> str:
    """Build a regex source matching how Pygments is likely to render ``accessed``."""
    return _dotted_span_source(tuple(accessed.split('.')))


#: Matches one Pygments name-token class (``n``, ``nf``, ``nc``, ...), for loosening.
_NAME_CLASS_RE = re.compile(r'class="n[a-zA-Z]{0,2}"')


def _highlight_fragment(expr: str) -> str | None:
    """Return Pygments' own HTML for ``expr``, or ``None`` if it isn't a single line."""
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import PythonLexer

    try:
        fragment = highlight(expr, PythonLexer(), HtmlFormatter(nowrap=True)).rstrip('\n')
    except Exception:  # noqa: BLE001 -- a fragment that won't lex simply isn't linkable
        return None
    return None if '\n' in fragment else fragment


def _expr_pattern_source(expr: str) -> str | None:
    """Build a regex source matching just ``expr``'s trailing ``.attribute``.

    The receiver goes in a fixed-width lookbehind -- hence escaped strictly, where only
    the trailing attribute is loosened -- leaving its own names free to be linked.
    """
    fragment = _highlight_fragment(expr)
    if fragment is None:
        return None
    index = fragment.rfind(_DOT_SPAN)
    if index <= 0:
        return None
    prefix, trailing = fragment[:index], fragment[index:]
    return f'(?<={re.escape(prefix)}){_NAME_CLASS_RE.sub(_LOOSE_NAME_CLASS, re.escape(trailing))}'


@dataclass(frozen=True)
class _Candidate:
    """One accessed name and the documented names it might resolve to."""

    accessed: str
    candidates: tuple[str, ...]
    #: A call or attribute read, rather than a bare mention (a type hint, an
    #: ``isinstance`` check). Gates the "Used In" entry and its count, never the link.
    counts_as_use: bool = True


@dataclass(frozen=True)
class _CallCandidate:
    """A trailing attribute chain on a call's result, and its candidate names."""

    call_target: str
    trailing: tuple[str, ...]
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class _ExprCandidate:
    """A trailing attribute on an expression no dotted name can address, e.g. a subscript.

    ``expr`` is the whole attribute expression; only its trailing attribute gets linked.
    """

    expr: str
    candidates: tuple[str, ...]


#: Any one recorded reference, whichever shape it takes.
_Record = _Candidate | _CallCandidate | _ExprCandidate


# ---------------------------------------------------------------------------
# Phase 1: collect accessed names, resolve each against the executed namespace.
# ---------------------------------------------------------------------------


def _dotted_name(node: ast.expr) -> str | None:
    """Return ``a.b.c`` for a chain rooted at a plain name, or ``None``."""
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if isinstance(cursor, ast.Name):
        parts.append(cursor.id)
        return '.'.join(reversed(parts))
    return None


class _NameCollector(ast.NodeVisitor):
    """Collect dotted names, and trailing attributes on a call's result."""

    def __init__(self) -> None:
        self.accessed: set[str] = set()
        #: Dotted names called directly somewhere in the source (``pl.add_mesh(...)``).
        self.called: set[str] = set()
        #: e.g. ``('pv.Sphere', ('plot',))`` for ``pv.Sphere().plot``.
        self.call_chains: set[tuple[str, tuple[str, ...]]] = set()
        #: Every call site of each chain, for overload disambiguation.
        self.call_chain_calls: dict[tuple[str, tuple[str, ...]], list[ast.Call]] = {}

    def visit_Name(self, node: ast.Name) -> None:
        """Record a bare name access."""
        self.accessed.add(node.id)

    def visit_Call(self, node: ast.Call) -> None:
        """Record a call's own target as directly called, then keep walking."""
        target = _dotted_name(node.func)
        if target is not None:
            self.called.add(target)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Record a dotted chain rooted at a plain name, or a call's trailing chain."""
        parts = []
        cursor: ast.expr = node
        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
        if isinstance(cursor, ast.Name):
            parts.append(cursor.id)
            self.accessed.add('.'.join(reversed(parts)))
            return
        if isinstance(cursor, ast.Call):
            call_target = _dotted_name(cursor.func)
            if call_target is not None and parts:
                key = (call_target, tuple(reversed(parts)))
                self.call_chains.add(key)
                self.call_chain_calls.setdefault(key, []).append(cursor)
        # e.g. `pv.Sphere().plot` -- keep walking the call's own arguments.
        self.visit(cursor)


def _collect(source: str) -> _NameCollector:
    """Parse ``source`` and return its populated name collector."""
    collector = _NameCollector()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return collector
    collector.visit(tree)
    return collector


def _module_path_candidates(thing: type | Any, method: list[str]) -> Iterator[str]:
    """Yield ``thing``'s qualified name at every module-path truncation depth."""
    qualname = getattr(thing, '__qualname__', None)
    if qualname is None:
        # e.g. functools.partial: isroutine() is true but there's no qualname.
        return
    module = inspect.getmodule(thing)
    if module is None:
        return
    module_parts = module.__name__.split('.')
    for depth in range(len(module_parts), 0, -1):
        yield '.'.join([*module_parts[:depth], qualname, *method])


def _class_candidates(cls: type, method: list[str]) -> list[str]:
    """Return module-path-truncated candidates for ``cls`` and every base class."""
    classes = [cls]
    offset = 0
    while offset < len(classes):
        for base in classes[offset].__bases__:
            if base is not object and base not in classes:
                classes.append(base)
        offset += 1
    return [name for cc in classes for name in _module_path_candidates(cc, method)]


def _namespace_lookup(accessed: str, namespace: dict[str, Any]) -> tuple[Any, list[str]] | None:
    """Return what ``accessed``'s root name is bound to and the attributes after it.

    Only the root name is looked up, as in any real Python namespace; a dotted key is
    never matched.
    """
    root, _, rest = accessed.partition('.')
    if root not in namespace:
        return None
    return namespace[root], rest.split('.') if rest else []


def _candidate_names(accessed: str, namespace: dict[str, Any]) -> list[str]:
    """Return candidate documented names for one dotted name access."""
    found = _namespace_lookup(accessed, namespace)
    if found is None:
        return []
    obj, remainder = found

    if inspect.ismodule(obj) and not remainder:
        return [obj.__name__]

    is_class_attr = False
    method: list[str] = []
    for level in remainder:
        owner = obj
        # type(owner) so a class's metaclass property resolves like an instance's.
        prop = getattr(type(owner), level, None)
        if isinstance(prop, property):
            obj = owner
            is_class_attr, method = True, [level]
            break
        try:
            obj = getattr(obj, level)
        except Exception:  # noqa: BLE001
            break
        if inspect.ismethod(obj):
            obj = owner
            is_class_attr, method = True, [level]
            break

    if inspect.ismodule(obj):
        return [obj.__name__]

    is_class = inspect.isclass(obj)
    if is_class or is_class_attr:
        return _class_candidates(obj if is_class else obj.__class__, method)

    if inspect.isroutine(obj):
        return list(_module_path_candidates(obj, []))

    return list(_module_path_candidates(obj.__class__, []))


def _candidates_for_callable(func: Any) -> list[str]:
    """Return candidate documented names for a callable observed at a real call site.

    Builtins are excluded as noise -- name-based resolution never found them either, so
    linking every ``print`` closes no gap. So are anonymous qualified names
    (``<locals>``, ``<genexpr>``), which are never documented objects.
    """
    if inspect.isclass(func):
        candidates = _class_candidates(func, [])
    elif inspect.ismethod(func):
        name = getattr(func, '__name__', None)
        owner = func.__self__
        if name is None:
            return []
        candidates = _class_candidates(owner if inspect.isclass(owner) else type(owner), [name])
    elif inspect.isroutine(func):
        candidates = list(_module_path_candidates(func, []))
    else:
        return []
    return [name for name in candidates if not name.startswith('builtins.') and '<' not in name]


def _is_attribute_read(accessed: str, namespace: dict[str, Any]) -> bool:
    """Return whether ``accessed`` reads a real value, not bare-naming a class/module/method."""
    found = _namespace_lookup(accessed, namespace)
    if found is None:
        return False
    obj, remainder = found
    if not remainder:
        return False
    for level in remainder:
        if isinstance(getattr(type(obj), level, None), property):
            return True
        try:
            obj = getattr(obj, level)
        except Exception:  # noqa: BLE001 -- arbitrary objects can raise anything
            return False
    return not (inspect.ismodule(obj) or inspect.isclass(obj) or inspect.isroutine(obj))


#: Matches a bare dotted class name (``PolyData``); rejects ``Widget | str``, ``list[int]``.
_SIMPLE_NAME_RE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z')


def _resolve_object(accessed: str, namespace: dict[str, Any]) -> Any | None:
    """Resolve a dotted name to the live object it refers to, or ``None``."""
    found = _namespace_lookup(accessed, namespace)
    if found is None:
        return None
    obj, remainder = found
    for level in remainder:
        try:
            obj = getattr(obj, level)
        except Exception:  # noqa: BLE001, PERF203 -- arbitrary objects can raise anything
            return None
    return obj


def _call_return_type(func: Any, namespace: dict[str, Any]) -> type | None:
    """Return ``func``'s return type, if its annotation names a resolvable class.

    A string annotation is looked up in ``func``'s own module, then every module in
    ``namespace``, then the builtins. A union resolves to its first resolvable member.
    """
    annotation = getattr(func, '__annotations__', {}).get('return')
    if isinstance(annotation, type):
        return annotation
    if not isinstance(annotation, str):
        return None
    namespaces = [getattr(func, '__globals__', {})]
    namespaces.extend(vars(obj) for obj in namespace.values() if inspect.ismodule(obj))
    namespaces.append(vars(builtins))
    for member in annotation.split('|'):
        member = member.strip()
        if not _SIMPLE_NAME_RE.match(member):
            continue
        name = member.rsplit('.', 1)[-1]
        for ns in namespaces:
            candidate = ns.get(name)
            if isinstance(candidate, type):
                return candidate
    return None


#: A call argument that isn't a literal, distinct from one that's absent.
_UNRESOLVED_ARG = object()

#: Matches a ``Literal[...]`` annotation string, with or without its ``typing.`` prefix.
_LITERAL_RE = re.compile(r'\A(?:typing\.)?Literal\[(?P<body>.*)\]\Z')


def _literal_annotation_values(annotation: str) -> set[Any] | None:
    """Return the values a ``Literal[...]`` annotation string allows, or ``None``."""
    match = _LITERAL_RE.match(annotation.strip())
    if match is None:
        return None
    try:
        parsed = ast.literal_eval(f'({match.group("body")},)')
    except (ValueError, SyntaxError):
        return None
    return set(parsed)


def _bound_literal_args(call: ast.Call, sig: inspect.Signature) -> dict[str, Any]:
    """Return ``{param name: value}`` for a call's arguments, by binding position to name.

    A non-literal argument is entered as :data:`_UNRESOLVED_ARG`, not omitted.
    """
    bound: dict[str, Any] = {}
    positional = [
        param
        for param in sig.parameters.values()
        if param.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    for index, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred) or index >= len(positional):
            continue
        try:
            bound[positional[index].name] = ast.literal_eval(arg)
        except ValueError:
            bound[positional[index].name] = _UNRESOLVED_ARG
    for keyword in call.keywords:
        if keyword.arg is None:  # **kwargs
            continue
        try:
            bound[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError:
            bound[keyword.arg] = _UNRESOLVED_ARG
    return bound


def _overload_matches(overload: Any, args: dict[str, Any]) -> bool:
    """Return whether every ``Literal``-typed parameter of ``overload`` accepts ``args``.

    A parameter not passed falls back to the overload's own default; one with no
    ``Literal[...]`` annotation never disqualifies a match.
    """
    sig = inspect.signature(overload)
    for name, param in sig.parameters.items():
        annotation = overload.__annotations__.get(name)
        if not isinstance(annotation, str):
            continue
        values = _literal_annotation_values(annotation)
        if values is None:
            continue
        effective = args.get(name, param.default)
        if effective is inspect.Parameter.empty or effective not in values:
            return False
    return True


def _resolve_via_overloads(
    func: Any, calls: list[ast.Call], namespace: dict[str, Any]
) -> type | None:
    """Return the one return type every recorded call site agrees on, via ``@overload``.

    ``None`` unless every call matches exactly one overload and they all agree, and
    always ``None`` on Python 3.10, which has no :func:`typing.get_overloads`.
    """
    if get_overloads is None:
        return None
    return _match_overloads(func, calls, namespace)


# Reachable only on Python 3.11+, so coverage would flip per-version.
def _match_overloads(  # pragma: no cover
    func: Any, calls: list[ast.Call], namespace: dict[str, Any]
) -> type | None:
    """Do the actual matching for :func:`_resolve_via_overloads`."""
    overloads = get_overloads(func)
    if not overloads or not calls:
        return None
    sig = inspect.signature(func)
    resolved: set[type] = set()
    for call in calls:
        args = _bound_literal_args(call, sig)
        matches = [overload for overload in overloads if _overload_matches(overload, args)]
        if len(matches) != 1:
            return None
        return_type = _call_return_type(matches[0], namespace)
        if return_type is None:
            return None
        resolved.add(return_type)
    return next(iter(resolved)) if len(resolved) == 1 else None


def _call_chain_candidates(
    call_target: str,
    trailing: tuple[str, ...],
    namespace: dict[str, Any],
    calls: list[ast.Call] | None = None,
) -> list[str]:
    """Return candidate documented names for a call's trailing attribute chain.

    ``calls`` narrows an overloaded callable; without them the plain return annotation
    decides.
    """
    func = _resolve_object(call_target, namespace)
    if func is None or not inspect.isroutine(func):
        return []
    return_type = _resolve_via_overloads(func, calls or [], namespace) or _call_return_type(
        func, namespace
    )
    if return_type is None:
        return []
    return _class_candidates(return_type, list(trailing))


def _records_for(source: str, namespace: dict[str, Any]) -> list[_Record]:
    """Return every resolved candidate for the identifiers accessed in ``source``."""
    records: list[_Record] = []
    collected = _collect(source)
    for accessed in sorted(collected.accessed):
        candidates = _candidate_names(accessed, namespace)
        if candidates:
            counts_as_use = accessed in collected.called or _is_attribute_read(accessed, namespace)
            records.append(_Candidate(accessed, tuple(candidates), counts_as_use))
    for call_target, trailing in sorted(collected.call_chains):
        calls = collected.call_chain_calls[(call_target, trailing)]
        candidates = _call_chain_candidates(call_target, trailing, namespace, calls)
        if candidates:
            records.append(_CallCandidate(call_target, trailing, tuple(candidates)))
    return records


def exec_with_local_scopes(
    code: CodeType, namespace: dict[str, Any], filename: str
) -> dict[str, Any]:
    """Execute ``code`` in ``namespace``, and return every local scope seen merged in.

    Runs exactly as ``exec(code, namespace)`` would, ``namespace`` populated the same
    way; only the returned dict differs. Only frames from ``filename`` are captured, and
    a local can shadow a global, or another call's local, of the same name -- resolving
    to the wrong link rather than to none.
    """
    captured: dict[str, Any] = {}
    old_profile = sys.getprofile()

    def _profiler(frame: Any, event: str, arg: Any) -> None:
        if old_profile is not None:
            old_profile(frame, event, arg)
        if event == 'return' and frame.f_code.co_filename == filename:
            captured.update(frame.f_locals)

    sys.setprofile(_profiler)
    try:
        exec(code, namespace)  # noqa: S102 -- caller controls what's compiled here
    finally:
        sys.setprofile(old_profile)

    merged = dict(captured)
    merged.update(namespace)
    return merged


#: Category for code recorded from inside a documented object's own docstring.
DEFAULT_DOCSTRING_EXAMPLE_CATEGORY = 'Docstring Examples'

#: Category :class:`~sphinx_autocodelink.gallery.AutoCodeLinkScraper` tags its pages with.
DEFAULT_GALLERY_CATEGORY = 'Sphinx Gallery'


def is_inside_autodoc_desc(state: RSTState) -> bool:
    """Return whether ``state``, a directive's own ``self.state``, is inside a description."""
    return bool(state.document.settings.env.temp_data.get('object'))


def _is_inside_desc_node(node: nodes.Node) -> bool:
    """Return whether ``node`` is nested inside an object description, by doctree ancestry."""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, addnodes.desc):
            return True
        parent = parent.parent
    return False


def _enclosing_section_id(node: nodes.Node) -> str:
    """Return the id of the nearest section enclosing ``node``, or ``''``."""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, nodes.section) and parent.get('ids'):
            return str(parent['ids'][0])
        parent = parent.parent
    return ''


def _page_code_section_id(doctree: nodes.document) -> str:
    """Return the id of the one section holding every code block on the page.

    Empty unless they all share a section: with code in more than one place there is no
    telling which a record came from. The page's own root section counts as no section,
    since an anchor there points where a plain page link already lands.
    """
    root_ids = {
        anchor_id
        for child in doctree.children
        if isinstance(child, nodes.section)
        for anchor_id in child.get('ids') or ()
    }
    sections = {
        '' if (found := _enclosing_section_id(node)) in root_ids else found
        for node in doctree.findall()
        if isinstance(node, (nodes.literal_block, nodes.doctest_block))
    }
    return sections.pop() if len(sections) == 1 else ''


def _resolve_pending_anchors(app: Sphinx, doctree: nodes.document) -> None:
    """Anchor records made before the page had sections to point at.

    :func:`record_namespace` takes an ``anchor``, but a directive calling it from inside
    a docstring has none to give: autodoc's content is still a detached subtree when the
    directive runs. Those records are anchored here instead, once the assembled page has
    real sections. Page-level rather than per-block, so a page whose code spans several
    sections points every name at the last one.
    """
    env = app.env
    records = getattr(env, _ENV_ATTR, {}).get(env.docname)
    if not records:
        return
    all_anchors: dict[str, dict[str, str]] = getattr(env, _ANCHOR_ATTR, None) or {}
    for_doc = all_anchors.setdefault(env.docname, {})
    if not any(name not in for_doc for record in records for name in record.candidates):
        return
    anchor = _page_code_section_id(doctree)
    if not anchor:
        return
    for record in records:
        for name in record.candidates:
            for_doc.setdefault(name, anchor)
    setattr(env, _ANCHOR_ATTR, all_anchors)


def _record_bare_doctest_blocks(app: Sphinx, doctree: nodes.document) -> None:
    """Execute and record every bare ``>>>`` doctest block on the page.

    Opt-in via ``autocodelink_doctest_blocks``. A block that fails to parse or raises is
    skipped with a warning.
    """
    env = app.env
    if not getattr(app.config, 'autocodelink_doctest_blocks', False):
        return
    docname = env.docname
    filename = f'<{docname}>'
    for index, block in enumerate(doctree.findall(nodes.doctest_block)):
        source = block.astext()
        try:
            code = doctest.script_from_examples(source)
            compiled = compile(code, filename, 'exec')
        except SyntaxError as error:
            _logger.warning(
                'autocodelink: skipping doctest block %d (could not parse: %s)',
                index,
                error,
                location=block,
            )
            continue
        try:
            namespace = exec_with_local_scopes(compiled, {}, filename)
        except Exception as error:  # noqa: BLE001
            _logger.warning(
                'autocodelink: skipping doctest block %d (raised %s: %s)',
                index,
                type(error).__name__,
                error,
                location=block,
            )
            continue
        category = DEFAULT_DOCSTRING_EXAMPLE_CATEGORY if _is_inside_desc_node(block) else ''
        record_namespace(
            env=env,
            docname=docname,
            source=code,
            namespace=namespace,
            category=category,
            anchor=_enclosing_section_id(block),
        )


def record_namespace(
    *,
    env: BuildEnvironment,
    docname: str,
    source: str,
    namespace: dict[str, Any],
    category: str = '',
    state: RSTState | None = None,
    anchor: str = '',
) -> None:
    """Record candidate documented names for every identifier in ``source``.

    ``category`` tags this page for grouping in ``.. autocodelink-index::`` output.
    ``state``, the calling directive's own, defaults it to
    :data:`DEFAULT_DOCSTRING_EXAMPLE_CATEGORY` inside an object description.
    ``anchor`` is the id of the section holding ``source``, so a "Used In" entry can
    link straight to it. A later block wins.
    """
    if not category and state is not None and is_inside_autodoc_desc(state):
        category = DEFAULT_DOCSTRING_EXAMPLE_CATEGORY

    all_records: dict[str, list[_Record]] | None = getattr(env, _ENV_ATTR, None)
    if all_records is None:
        all_records = {}
        setattr(env, _ENV_ATTR, all_records)
    records = _records_for(source, namespace)
    all_records.setdefault(docname, []).extend(records)

    if anchor:
        anchors: dict[str, dict[str, str]] = getattr(env, _ANCHOR_ATTR, None) or {}
        for_doc = anchors.setdefault(docname, {})
        for record in records:
            for name in record.candidates:
                for_doc[name] = anchor
        setattr(env, _ANCHOR_ATTR, anchors)

    if category:
        categories: dict[str, str] = getattr(env, _CATEGORY_ATTR, None) or {}
        categories[docname] = category
        setattr(env, _CATEGORY_ATTR, categories)


def record_namespace_to_disk(
    *,
    directory: str | Path,
    docname: str,
    source: str,
    namespace: dict[str, Any],
    category: str = '',
    extra: Sequence[_Record] = (),
) -> None:
    """Like :func:`record_namespace`, but appended to a file under ``directory``.

    For a process Sphinx's own ``env-merge-info`` never sees. ``extra`` records are
    appended as-is, having been resolved elsewhere.
    """
    records = [*_records_for(source, namespace), *(extra or ())]
    if not records:
        return
    target = Path(directory) / f'{docname}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(target.read_text()) if target.exists() else {'records': []}
    existing['records'].extend(_to_jsonable(r) for r in records)
    if category:
        existing['category'] = category
    target.write_text(json.dumps(existing))


def _to_jsonable(record: _Record) -> dict[str, Any]:
    """Convert one record to a JSON-serializable dict."""
    if isinstance(record, _ExprCandidate):
        return {'expr': record.expr, 'candidates': list(record.candidates)}
    if isinstance(record, _CallCandidate):
        return {
            'call_target': record.call_target,
            'trailing': list(record.trailing),
            'candidates': list(record.candidates),
        }
    return {
        'accessed': record.accessed,
        'candidates': list(record.candidates),
        'counts_as_use': record.counts_as_use,
    }


def _from_jsonable(entry: dict[str, Any]) -> _Record:
    """Convert one JSON dict back to a record."""
    if 'expr' in entry:
        return _ExprCandidate(entry['expr'], tuple(entry['candidates']))
    if 'call_target' in entry:
        return _CallCandidate(
            entry['call_target'], tuple(entry['trailing']), tuple(entry['candidates'])
        )
    return _Candidate(
        entry['accessed'], tuple(entry['candidates']), entry.get('counts_as_use', True)
    )


def _load_disk_records(
    directory: Path,
) -> tuple[dict[str, list[_Record]], dict[str, str]]:
    """Return every docname's records and category, written by :func:`record_namespace_to_disk`."""
    records: dict[str, list[_Record]] = {}
    categories: dict[str, str] = {}
    if not directory.is_dir():
        return records, categories
    for file in directory.rglob('*.json'):
        docname = file.relative_to(directory).with_suffix('').as_posix()
        data = json.loads(file.read_text())
        records[docname] = [_from_jsonable(e) for e in data['records']]
        if data.get('category'):
            categories[docname] = data['category']
    return records, categories


def _clear_disk_records(app: Sphinx) -> None:
    """Clear records left over from a previous build, if disk-based recording is enabled."""
    records_dir = getattr(app.config, 'autocodelink_records_dir', None)
    if records_dir:
        shutil.rmtree(Path(app.srcdir) / records_dir, ignore_errors=True)


def _note_index_doc(env: BuildEnvironment, docname: str) -> None:
    """Record that ``docname`` hosts an ``.. autocodelink-index::`` placeholder."""
    docs: set[str] = getattr(env, _INDEX_DOCS_ATTR, None) or set()
    docs.add(docname)
    setattr(env, _INDEX_DOCS_ATTR, docs)


def _merge_records(
    app: Sphinx,
    env: BuildEnvironment,
    docnames: list[str],
    other: BuildEnvironment,
) -> None:
    """Merge records collected in a parallel-reading worker process."""
    ours = getattr(env, _ENV_ATTR, {})
    theirs = getattr(other, _ENV_ATTR, {})
    ours.update(theirs)
    setattr(env, _ENV_ATTR, ours)

    our_index_docs: set[str] = getattr(env, _INDEX_DOCS_ATTR, set())
    their_index_docs: set[str] = getattr(other, _INDEX_DOCS_ATTR, set())
    setattr(env, _INDEX_DOCS_ATTR, our_index_docs | their_index_docs)

    our_anchors: dict[str, dict[str, str]] = getattr(env, _ANCHOR_ATTR, {})
    our_anchors.update(getattr(other, _ANCHOR_ATTR, {}))
    setattr(env, _ANCHOR_ATTR, our_anchors)

    our_categories: dict[str, str] = getattr(env, _CATEGORY_ATTR, {})
    their_categories: dict[str, str] = getattr(other, _CATEGORY_ATTR, {})
    our_categories.update(their_categories)
    setattr(env, _CATEGORY_ATTR, our_categories)


def _purge_doc(app: Sphinx, env: BuildEnvironment, docname: str) -> None:
    """Drop stale records for a document being re-read."""
    getattr(env, _ENV_ATTR, {}).pop(docname, None)
    getattr(env, _INDEX_DOCS_ATTR, set()).discard(docname)
    getattr(env, _CATEGORY_ATTR, {}).pop(docname, None)
    getattr(env, _ANCHOR_ATTR, {}).pop(docname, None)


# ---------------------------------------------------------------------------
# Phase 2: match candidates against the inventory and rewrite the built HTML.
# ---------------------------------------------------------------------------


def _local_inventory(app: Sphinx) -> dict[str, tuple[str, str]]:
    """Return ``{name: (docname, anchor)}`` for every locally documented Python object."""
    return {
        name: (entry.docname, entry.node_id)
        for name, entry in app.env.domains['py'].objects.items()
    }


def _aliased_names(app: Sphinx) -> set[str]:
    """Return every name registered only as a ``:canonical:`` cross-reference target.

    These lose to a non-aliased name resolving to the same target, which is the one an
    object's own backreferences are keyed by.
    """
    return {name for name, entry in app.env.domains['py'].objects.items() if entry.aliased}


def _intersphinx_inventory(app: Sphinx) -> dict[str, str]:
    """Return ``{name: absolute_url}`` for every intersphinx-mapped object."""
    from sphinx.ext.intersphinx import InventoryAdapter

    urls: dict[str, str] = {}
    for by_objtype in InventoryAdapter(app.env).main_inventory.values():
        for name, item in by_objtype.items():
            urls.setdefault(name, item[2])
    return urls


def _resolve_link(
    candidates: tuple[str, ...],
    *,
    docname: str,
    app: Sphinx,
    local: dict[str, tuple[str, str]],
    external: dict[str, str],
    aliased: frozenset[str] = frozenset(),
) -> tuple[str, str] | None:
    """Return the first candidate's ``(name, url)``, local names taking priority, or ``None``.

    A match in ``aliased`` loses to a later candidate resolving to the same target.
    """
    for i, name in enumerate(candidates):
        if name in local:
            target = local[name]
            best_name = name
            if name in aliased:
                for alt in candidates[i + 1 :]:
                    if local.get(alt) == target and alt not in aliased:
                        best_name = alt
                        break
            target_docname, anchor = local[best_name]
            return best_name, f'{app.builder.get_relative_uri(docname, target_docname)}#{anchor}'
        if name in external:
            return name, external[name]
    return None


def _embed_links(app: Sphinx, exception: Exception | None) -> None:
    """Rewrite built HTML pages with links for every resolved recorded name."""
    if exception is not None or app.builder.format != 'html':
        return

    records: dict[str, list[_Record]] = dict(getattr(app.env, _ENV_ATTR, {}))
    categories: dict[str, str] = dict(getattr(app.env, _CATEGORY_ATTR, {}))
    records_dir = getattr(app.config, 'autocodelink_records_dir', None)
    if records_dir:
        disk_records, disk_categories = _load_disk_records(Path(app.srcdir) / records_dir)
        for docname, records_for_doc in disk_records.items():
            records.setdefault(docname, []).extend(records_for_doc)
        categories.update(disk_categories)
    index_docs: set[str] = set(getattr(app.env, _INDEX_DOCS_ATTR, ()))
    if not records and not index_docs:
        return

    local = _local_inventory(app)
    aliased = frozenset(_aliased_names(app))
    external = _intersphinx_inventory(app)
    backrefs: dict[str, set[str]] = {}
    #: Per target name, how many times each page used it. Accumulates across records,
    #: unlike the link-embedding dedup below.
    usage_counts: dict[str, Counter[str]] = {}

    for docname, candidates in records.items():
        out_file = Path(app.outdir) / (app.builder.get_target_uri(docname))
        if not out_file.exists():
            continue

        # Raw occurrence counts, independent of the dedup below.
        name_occurrences: Counter[str] = Counter()
        call_occurrences: Counter[tuple[str, tuple[str, ...]]] = Counter()
        expr_occurrences: Counter[str] = Counter()
        for candidate in candidates:
            if isinstance(candidate, _ExprCandidate):
                expr_occurrences[candidate.expr] += 1
            elif isinstance(candidate, _CallCandidate):
                call_occurrences[(candidate.call_target, candidate.trailing)] += 1
            elif candidate.counts_as_use:
                name_occurrences[candidate.accessed] += 1

        # Dedup: the same accessed name, call chain or expression can be recorded twice.
        resolved_names: dict[str, str] = {}
        resolved_calls: dict[tuple[str, tuple[str, ...]], str] = {}
        resolved_exprs: dict[str, str] = {}
        for candidate in candidates:
            if isinstance(candidate, _ExprCandidate):
                if candidate.expr in resolved_exprs:
                    continue
                resolved = _resolve_link(
                    candidate.candidates,
                    docname=docname,
                    app=app,
                    local=local,
                    external=external,
                    aliased=aliased,
                )
                if resolved is not None:
                    name, link = resolved
                    resolved_exprs[candidate.expr] = link
                    backrefs.setdefault(name, set()).add(docname)
                    usage_counts.setdefault(name, Counter())[docname] += expr_occurrences[
                        candidate.expr
                    ]
            elif isinstance(candidate, _CallCandidate):
                call_key = (candidate.call_target, candidate.trailing)
                if call_key in resolved_calls:
                    continue
                resolved = _resolve_link(
                    candidate.candidates,
                    docname=docname,
                    app=app,
                    local=local,
                    external=external,
                    aliased=aliased,
                )
                if resolved is not None:
                    name, link = resolved
                    resolved_calls[call_key] = link
                    backrefs.setdefault(name, set()).add(docname)
                    usage_counts.setdefault(name, Counter())[docname] += call_occurrences[call_key]
            else:
                if candidate.accessed in resolved_names:
                    continue
                resolved = _resolve_link(
                    candidate.candidates,
                    docname=docname,
                    app=app,
                    local=local,
                    external=external,
                    aliased=aliased,
                )
                if resolved is not None:
                    name, link = resolved
                    resolved_names[candidate.accessed] = link
                    # A bare mention is linked above, but isn't a "Used In" entry.
                    count = name_occurrences[candidate.accessed]
                    if count:
                        backrefs.setdefault(name, set()).add(docname)
                        usage_counts.setdefault(name, Counter())[docname] += count
        if not resolved_names and not resolved_calls and not resolved_exprs:
            continue

        # One pattern, longest name first (avoids re-wrapping `mesh` inside `mesh.plot`);
        # call chains get a nested `w{i}` group so only the trailing attrs get wrapped.
        group_kind: dict[int, str] = {}
        group_link: dict[int, str] = {}
        sources: list[str] = []
        for i, name in enumerate(sorted(resolved_names, key=len, reverse=True)):
            group_kind[i] = 'name'
            group_link[i] = resolved_names[name]
            sources.append(f'(?P<n{i}>{_name_pattern_source(name)})')
        offset = len(sources)
        for j, key in enumerate(sorted(resolved_calls, key=lambda k: len(k[1]), reverse=True)):
            i = offset + j
            group_kind[i] = 'call'
            group_link[i] = resolved_calls[key]
            _, trailing = key
            sources.append(
                f'(?P<n{i}>{_CALL_END}(?P<w{i}>{_DOT_SPAN}{_dotted_span_source(trailing)}))'
            )
        # An expression's own pattern matches only its trailing attribute, its receiver
        # held in a lookbehind -- so a name inside that receiver still gets its own link.
        for expr in sorted(resolved_exprs, key=len, reverse=True):
            pattern = _expr_pattern_source(expr)
            if pattern is None:
                continue
            i = len(sources)
            group_kind[i] = 'name'
            group_link[i] = resolved_exprs[expr]
            sources.append(f'(?P<n{i}>{pattern})')
        if not sources:
            continue
        combined = re.compile('|'.join(sources))

        html = out_file.read_text(encoding='utf-8')

        # Skip matches already inside an anchor (ours or another extension's).
        already_linked = [m.span() for m in _ANCHOR_RE.finditer(html)]

        def _wrap(
            match: re.Match[str],
            group_kind: dict[int, str] = group_kind,
            group_link: dict[int, str] = group_link,
            already_linked: list[tuple[int, int]] = already_linked,
        ) -> str:
            i = int(match.lastgroup[1:])
            link = group_link[i]
            if group_kind[i] == 'call':
                wrap_start, wrap_end = match.span(f'w{i}')
                if any(start <= wrap_start < end for start, end in already_linked):
                    return match.group(0)
                prefix = match.string[match.start() : wrap_start]
                wrapped = match.string[wrap_start:wrap_end]
                return f'{prefix}<a class="sphinx-autocodelink-a" href="{link}">{wrapped}</a>'
            if any(start <= match.start() < end for start, end in already_linked):
                return match.group(0)
            return f'<a class="sphinx-autocodelink-a" href="{link}">{match.group(0)}</a>'

        out_file.write_text(combined.sub(_wrap, html), encoding='utf-8')

    if index_docs:
        env_anchors: dict[str, dict[str, str]] = getattr(app.env, _ANCHOR_ATTR, {})
        anchors = {
            target: {
                doc: env_anchors[doc][target] for doc in docs if target in env_anchors.get(doc, {})
            }
            for target, docs in backrefs.items()
        }
        _fill_index_placeholders(
            app,
            index_docs,
            backrefs,
            local=local,
            external=external,
            categories=categories,
            usage_counts=usage_counts,
            anchors=anchors,
        )


#: Matches one ``.. autocodelink-index::`` placeholder and its JSON options blob.
_INDEX_PLACEHOLDER_RE = re.compile(
    r'<div class="sphinx-autocodelink-index" data-opts="([^"]*)"></div>'
)

#: Matches a ``:label:``-generated section, removed whole by ``:hide-empty:``.
_BACKREFS_SECTION_RE = re.compile(
    r'<section\b[^>]*\bclass="[^"]*sphinx-autocodelink-backrefs[^"]*"[^>]*>.*?</section>',
    re.DOTALL,
)

#: Pulls a section's ``id`` out of its opening tag, to strip its in-page nav link too.
_SECTION_ID_RE = re.compile(r'\bid="([^"]*)"')


def _index_entry_link(
    name: str,
    *,
    from_docname: str,
    app: Sphinx,
    local: dict[str, tuple[str, str]],
    external: dict[str, str],
) -> str | None:
    """Return ``name``'s own documented URL, relative to ``from_docname``."""
    if name in local:
        target_docname, anchor = local[name]
        return f'{app.builder.get_relative_uri(from_docname, target_docname)}#{anchor}'
    return external.get(name)


def _docname_title(app: Sphinx, docname: str) -> str:
    """Return ``docname``'s page title, or the docname itself if it has none on record."""
    title_node = app.env.titles.get(docname)
    return title_node.astext() if title_node is not None else docname


def _sorted_refs(
    refs: list[str],
    *,
    app: Sphinx,
    usage_counts: dict[str, int],
    show_titles: bool = True,
) -> list[tuple[str, str]]:
    """Return ``(display label, ref)`` pairs in ``autocodelink_sort`` order."""
    pairs = [(_docname_title(app, ref) if show_titles else ref, ref) for ref in refs]
    if getattr(app.config, 'autocodelink_sort', 'alphabetical') == 'frequency':
        return sorted(pairs, key=lambda pair: (-usage_counts.get(pair[1], 0), pair[0]))
    return sorted(pairs)


_COLLAPSE_THRESHOLD = 8
_COLLAPSE_VISIBLE = 5


#: Above this many hidden entries, the overflow list lays out in columns.
_COLUMN_LAYOUT_THRESHOLD = 24


def _render_ref_list(
    refs: list[str],
    *,
    docname: str,
    app: Sphinx,
    show_titles: bool,
    categories: dict[str, str] | None = None,
    usage_counts: dict[str, int] | None = None,
    anchors: dict[str, str] | None = None,
) -> str:
    """Render ``<ul>`` link(s) to ``refs``, relative to ``docname``.

    Ordered by ``autocodelink_sort``, styled by each ref's category, and collapsed
    behind a ``<details>`` toggle past :data:`_COLLAPSE_THRESHOLD` entries.
    """
    categories = categories or {}
    usage_counts = usage_counts or {}
    anchors = anchors or {}
    if getattr(app.config, 'autocodelink_gallery_cards', False):
        from sphinx_autocodelink._gallery_cards import render_gallery_carousel

        gallery_refs = {ref for ref in refs if categories.get(ref) == DEFAULT_GALLERY_CATEGORY}
        if gallery_refs:
            carousel = render_gallery_carousel(
                sorted(gallery_refs), docname=docname, app=app, usage_counts=usage_counts
            )
            other_refs = [ref for ref in refs if ref not in gallery_refs]
            if not other_refs:
                return carousel
            return carousel + _render_ref_list(
                other_refs,
                docname=docname,
                app=app,
                show_titles=show_titles,
                categories=categories,
                usage_counts=usage_counts,
                anchors=anchors,
            )

    show_counts = getattr(app.config, 'autocodelink_show_usage_count', False)
    labeled = _sorted_refs(refs, app=app, usage_counts=usage_counts, show_titles=show_titles)

    def render(entries: list[tuple[str, str]]) -> str:
        """Render a sequence of (label, ref) pairs as ``<li>`` entries."""
        items = []
        for label, ref in entries:
            href = app.builder.get_relative_uri(docname, ref)
            if anchor := anchors.get(ref):
                href = f'{href}#{anchor}'
            text = escape(label)
            category = categories.get(ref)
            if category == DEFAULT_DOCSTRING_EXAMPLE_CATEGORY:
                text = f'{_XREF_OPEN}{text}{_XREF_CLOSE}'
            elif category == DEFAULT_GALLERY_CATEGORY:
                text = f'{_STD_REF_OPEN}{text}{_STD_REF_CLOSE}'
            count_suffix = ''
            if show_counts:
                # Outside the <a> -- this is what explains the ranking, not part of
                # the destination itself, so it shouldn't read (or click) like one.
                count = usage_counts.get(ref, 0)
                uses = 'use' if count == 1 else 'uses'
                count_suffix = (
                    f' <span class="sphinx-autocodelink-usage-count">({count} {uses})</span>'
                )
            items.append(f'<li><a href="{href}">{text}</a>{count_suffix}</li>')
        return ''.join(items)

    if len(labeled) <= _COLLAPSE_THRESHOLD:
        return f'<ul class="sphinx-autocodelink-index">{render(labeled)}</ul>'

    visible, hidden = labeled[:_COLLAPSE_VISIBLE], labeled[_COLLAPSE_VISIBLE:]
    hidden_style = (
        ' style="columns: 16em; column-gap: 1.5em;"'
        if len(hidden) > _COLUMN_LAYOUT_THRESHOLD
        else ''
    )
    return (
        f'<ul class="sphinx-autocodelink-index">{render(visible)}'
        '<li class="sphinx-autocodelink-index-more"><details>'
        f'<summary>{len(hidden)} more</summary>'
        f'<ul class="sphinx-autocodelink-index"{hidden_style}>{render(hidden)}</ul>'
        '</details></li>'
        '</ul>'
    )


def _sorted_categories(
    groups: dict[str, list[str]],
    category_labels: dict[str, str],
    category_order: Sequence[str],
    docname: str,
) -> list[str]:
    """Return ``groups``' own categories in render order (see ``autocodelink_category_order``)."""
    if not category_order:
        return sorted(groups, key=lambda c: category_labels.get(c, c))

    order_index = {category: i for i, category in enumerate(category_order)}
    missing = sorted(c for c in groups if c not in order_index)
    if missing:
        noun = 'category' if len(missing) == 1 else 'categories'
        _logger.warning(
            'autocodelink: %s not in autocodelink_category_order, '
            'sorted alphabetically at the end: %s',
            noun,
            ', '.join(repr(c) for c in missing),
            location=docname,
        )
    return sorted(
        groups,
        key=lambda c: (order_index.get(c, len(category_order)), category_labels.get(c, c)),
    )


def _render_grouped_refs(
    refs: list[str],
    *,
    docname: str,
    app: Sphinx,
    categories: dict[str, str],
    show_titles: bool,
    group: bool,
    usage_counts: dict[str, int] | None = None,
    anchors: dict[str, str] | None = None,
) -> str:
    """Render ``refs`` as one flat list, or grouped by category depending on ``group``."""
    groups: dict[str, list[str]] = {}
    for ref in refs:
        groups.setdefault(categories.get(ref, _UNCATEGORIZED_LABEL), []).append(ref)

    if not group:
        return _render_ref_list(
            refs,
            docname=docname,
            app=app,
            show_titles=show_titles,
            categories=categories,
            usage_counts=usage_counts,
            anchors=anchors,
        )

    category_labels = getattr(app.config, 'autocodelink_category_labels', {})
    category_order = getattr(app.config, 'autocodelink_category_order', ())
    parts = []
    for category in _sorted_categories(groups, category_labels, category_order, docname):
        label = category_labels.get(category, category)
        ref_list = _render_ref_list(
            groups[category],
            docname=docname,
            app=app,
            show_titles=show_titles,
            categories=categories,
            usage_counts=usage_counts,
            anchors=anchors,
        )
        parts.append(
            '<div class="sphinx-autocodelink-index-group">'
            f'<p class="sphinx-autocodelink-index-group-label"><strong>{escape(label)}</strong></p>'
            f'{ref_list}</div>'
        )
    return ''.join(parts)


def _render_index_entry(
    target: str,
    backrefs: dict[str, set[str]],
    *,
    docname: str,
    app: Sphinx,
    categories: dict[str, str],
    show_titles: bool,
    group: bool,
    usage_counts: dict[str, dict[str, int]] | None = None,
    anchors: dict[str, dict[str, str]] | None = None,
) -> str:
    """Render one target name's list of referencing pages, or ``''`` if it has none.

    Excludes ``docname`` itself: an object's own docstring demonstrating that same
    object (e.g. its own Examples section calling it) isn't a genuine cross-reference.
    """
    refs = sorted(ref for ref in backrefs.get(target, ()) if ref != docname)
    if not refs:
        return ''
    return _render_grouped_refs(
        refs,
        docname=docname,
        app=app,
        categories=categories,
        show_titles=show_titles,
        group=group,
        usage_counts=(usage_counts or {}).get(target),
        anchors=(anchors or {}).get(target),
    )


def _render_index_html(
    name: str,
    backrefs: dict[str, set[str]],
    *,
    docname: str,
    app: Sphinx,
    local: dict[str, tuple[str, str]],
    external: dict[str, str],
    categories: dict[str, str],
    hide_empty: bool = False,
    show_titles: bool = True,
    group: bool = True,
    usage_counts: dict[str, dict[str, int]] | None = None,
    anchors: dict[str, dict[str, str]] | None = None,
) -> str:
    """Render one ``.. autocodelink-index::`` placeholder's replacement HTML."""
    if name:
        body = _render_index_entry(
            name,
            backrefs,
            docname=docname,
            app=app,
            categories=categories,
            show_titles=show_titles,
            group=group,
            usage_counts=usage_counts,
            anchors=anchors,
        )
    else:
        body = _render_full_index(
            backrefs,
            docname=docname,
            app=app,
            local=local,
            external=external,
            categories=categories,
            show_titles=show_titles,
            group=group,
            usage_counts=usage_counts,
            anchors=anchors,
        )

    if not body:
        if hide_empty:
            return ''
        return '<p class="sphinx-autocodelink-index-empty">No references found.</p>'
    return body


def _render_full_index(
    backrefs: dict[str, set[str]],
    *,
    docname: str,
    app: Sphinx,
    local: dict[str, tuple[str, str]],
    external: dict[str, str],
    categories: dict[str, str],
    show_titles: bool = True,
    group: bool = True,
    usage_counts: dict[str, dict[str, int]] | None = None,
    anchors: dict[str, dict[str, str]] | None = None,
) -> str:
    """Render the site-wide index: every resolved name and its referencing pages."""
    usage_counts = usage_counts or {}
    entries = []
    for target in sorted(backrefs):
        # Exclude self-references -- see _render_index_entry.
        refs = sorted(ref for ref in backrefs.get(target, ()) if ref != docname)
        if not refs:
            continue
        link = _index_entry_link(
            target, from_docname=docname, app=app, local=local, external=external
        )
        heading = f'<a href="{link}">{escape(target)}</a>' if link else escape(target)
        body = _render_grouped_refs(
            refs,
            docname=docname,
            app=app,
            categories=categories,
            show_titles=show_titles,
            group=group,
            usage_counts=usage_counts.get(target),
            anchors=(anchors or {}).get(target),
        )
        entries.append(f'<dt>{heading}</dt><dd>{body}</dd>')
    if not entries:
        return ''
    return f'<dl class="sphinx-autocodelink-index">{"".join(entries)}</dl>'


def _strip_nav_links_to(html: str, removed_ids: set[str]) -> str:
    """Remove a page's in-page nav entries pointing at ``removed_ids`` sections.

    Themes render their "on this page" nav from the doctree before ``:hide-empty:`` drops a
    section, so a dropped section otherwise leaves a dangling nav link behind.
    """
    for anchor_id in removed_ids:
        html = re.sub(
            rf'<li\b[^>]*>\s*<a\b[^>]*\bhref="#{re.escape(anchor_id)}"[^>]*>.*?</a>\s*</li>',
            '',
            html,
            flags=re.DOTALL,
        )
    return html


def _fill_index_placeholders(
    app: Sphinx,
    index_docs: set[str],
    backrefs: dict[str, set[str]],
    *,
    local: dict[str, tuple[str, str]],
    external: dict[str, str],
    categories: dict[str, str],
    usage_counts: dict[str, dict[str, int]] | None = None,
    anchors: dict[str, dict[str, str]] | None = None,
) -> None:
    """Replace every ``.. autocodelink-index::`` placeholder with its rendered backreferences."""

    def _render_placeholder(match: re.Match[str], docname: str) -> str:
        opts = json.loads(unescape(match.group(1)))
        return _render_index_html(
            opts['name'],
            backrefs,
            docname=docname,
            app=app,
            local=local,
            external=external,
            categories=categories,
            hide_empty=opts['hide_empty'],
            show_titles=opts['titles'],
            group=opts['group'],
            usage_counts=usage_counts,
            anchors=anchors,
        )

    def _render_section(match: re.Match[str], docname: str, removed_ids: set[str]) -> str:
        section_html = match.group(0)
        placeholder = _INDEX_PLACEHOLDER_RE.search(section_html)
        if placeholder is None:  # defensive: no placeholder inside, leave untouched
            return section_html
        opts = json.loads(unescape(placeholder.group(1)))
        rendered = _render_placeholder(placeholder, docname)
        if not rendered and opts['hide_empty']:
            open_tag = section_html[: section_html.index('>') + 1]
            id_match = _SECTION_ID_RE.search(open_tag)
            if id_match is not None:
                removed_ids.add(id_match.group(1))
            return ''  # :hide-empty: and nothing to show -- drop the heading too
        return section_html[: placeholder.start()] + rendered + section_html[placeholder.end() :]

    for docname in index_docs:
        out_file = Path(app.outdir) / app.builder.get_target_uri(docname)
        if not out_file.exists():
            continue
        html = out_file.read_text(encoding='utf-8')
        removed_ids: set[str] = set()

        def _render_section_bound(
            match: re.Match[str], docname: str = docname, removed_ids: set[str] = removed_ids
        ) -> str:
            """Bind ``docname`` and this iteration's ``removed_ids`` for the ``sub`` callback."""
            return _render_section(match, docname, removed_ids)

        # :label: sections first, as one atomic unit; then any plain, unlabeled placeholders.
        html = _BACKREFS_SECTION_RE.sub(_render_section_bound, html)
        html = _INDEX_PLACEHOLDER_RE.sub(lambda m, d=docname: _render_placeholder(m, d), html)
        html = _strip_nav_links_to(html, removed_ids)
        out_file.write_text(html, encoding='utf-8')


def _inject_backref_index(
    app: Sphinx, what: str, name: str, obj: Any, options: dict[str, Any], lines: list[str]
) -> None:
    """Append a hidden-if-empty backreferences index to every non-module docstring."""
    if what == 'module':
        return
    lines.append('')
    lines.append(f'.. autocodelink-index:: {name}')
    lines.append('   :label: Used In')
    lines.append('   :hide-empty:')


def _register_autodoc_hook(app: Sphinx) -> None:
    """Connect the autodoc backrefs hook, once autodoc's own events are registered."""
    if not getattr(app.config, 'autocodelink_autodoc_backrefs', False):
        return
    if 'autodoc-process-docstring' not in app.events.events:
        return
    app.connect('autodoc-process-docstring', _inject_backref_index)


def _wire_gallery_tracing(app: Sphinx, config: Any) -> None:
    """Add :func:`~sphinx_autocodelink.gallery.reset_autocodelink` to ``reset_modules``.

    Runs ahead of Sphinx-Gallery's own ``config-inited`` handler, which is what reads
    ``reset_modules`` into the conf that reaches parallel workers.
    """
    from sphinx_autocodelink.gallery import RESET_AUTOCODELINK
    from sphinx_autocodelink.gallery import _wants_tracing

    gallery_conf = getattr(config, 'sphinx_gallery_conf', None)
    if not _wants_tracing(gallery_conf):
        return
    resets = tuple(gallery_conf.get('reset_modules', ('matplotlib', 'seaborn')))
    if RESET_AUTOCODELINK in resets:
        return
    # By dotted name, which Sphinx-Gallery imports itself, keeping the conf picklable.
    gallery_conf['reset_modules'] = (*resets, RESET_AUTOCODELINK)
    app.connect('build-finished', _stop_gallery_tracing)
    if gallery_conf.get('reset_modules_order', 'before') == 'after':
        _logger.warning(
            "autocodelink: sphinx_gallery_conf['reset_modules_order'] is 'after', so "
            'nothing runs before an example -- gallery examples will resolve their own '
            "top-level scope only. Use 'before' or 'both' to trace them."
        )


def _stop_gallery_tracing(app: Sphinx, exception: Exception | None) -> None:
    """Leave no tracer running past the gallery: nothing calls ``reset_modules`` at the end."""
    from sphinx_autocodelink.gallery import reset_autocodelink

    reset_autocodelink({}, None, 'after')


#: Default for both ``autocodelink_records_dir`` and ``AutoCodeLinkScraper.records_dir``.
DEFAULT_RECORDS_DIR = '_autocodelink_records'


def setup(app: Sphinx) -> dict[str, bool]:
    """Wire up dynamic autolinking.

    Registers the ``.. autocodelink::`` and ``.. autocodelink-index::`` directives and
    the event hooks that resolve and embed links. See the README for every
    ``autocodelink_*`` config value.
    """
    from sphinx_autocodelink._directive import AutoCodeLink
    from sphinx_autocodelink._directive import AutoCodeLinkIndex

    app.connect('builder-inited', _clear_disk_records)
    app.connect('config-inited', _wire_gallery_tracing, priority=5)
    app.connect('env-merge-info', _merge_records)
    app.connect('env-purge-doc', _purge_doc)
    # Priority > 500 so other extensions' embedding runs first and ours can skip it.
    app.connect('build-finished', _embed_links, priority=900)
    app.connect('builder-inited', _register_autodoc_hook)
    app.connect('doctree-read', _record_bare_doctest_blocks)
    # after other doctree-read handlers, so page-restructuring transforms have run
    app.connect('doctree-read', _resolve_pending_anchors, priority=900)
    app.add_config_value('autocodelink_records_dir', DEFAULT_RECORDS_DIR, rebuild='html')
    app.add_config_value('autocodelink_autodoc_backrefs', False, rebuild='html')
    app.add_config_value('autocodelink_category_labels', {}, rebuild='html')
    app.add_config_value('autocodelink_category_order', (), rebuild='html', types=(list, tuple))
    app.add_config_value('autocodelink_doctest_blocks', False, rebuild='html')
    app.add_config_value('autocodelink_sort', 'alphabetical', rebuild='html')
    app.add_config_value('autocodelink_show_usage_count', False, rebuild='html')
    app.add_config_value('autocodelink_gallery_cards', False, rebuild='html')
    app.add_directive('autocodelink', AutoCodeLink)
    app.add_directive('autocodelink-index', AutoCodeLinkIndex)
    return {'parallel_read_safe': True, 'parallel_write_safe': True}
