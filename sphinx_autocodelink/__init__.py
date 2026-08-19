"""Dynamic hyperlinking of identifiers in already-executed Sphinx code output.

Resolves each identifier against the real namespace it executed in, rather
than inferring its type statically (cf. sphinx-codeautolink). Never executes
anything itself, by default: a consumer that already executes example code
for its own purposes (e.g. to render a figure) calls :func:`record_namespace`
with the resulting namespace; call :func:`setup` from that consumer's own
Sphinx ``setup(app)`` to wire up link embedding. The one opt-in exception is
``autocodelink_doctest_blocks`` -- see :func:`setup`.

Limitations: a call with no intermediate variable (``pv.Sphere().plot()``) only
resolves its trailing attribute when the call's return annotation is a plain,
resolvable class name. Root identifiers that only ever exist inside a script's
own helper functions (never in its top-level namespace) need
:func:`exec_with_local_scopes` instead of a plain ``exec()`` to resolve.
"""

from __future__ import annotations

import ast
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

from docutils import nodes
from sphinx import addnodes
from sphinx.util import logging as sphinx_logging

if TYPE_CHECKING:
    from collections.abc import Iterator
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

#: Display label for a referencing page with no recorded category -- i.e. everything
#: that isn't :class:`sphinx_autocodelink.gallery.AutoCodeLinkScraper`'s own Sphinx-Gallery
#: integration: the standalone ``.. autocodelink::`` directive used bare, or a third-party
#: extension calling :func:`record_namespace` directly (e.g. a ``.. plot::``-style
#: directive executing a docstring's own Examples section).
_UNCATEGORIZED_LABEL = 'Documentation'

#: Matches any anchor tag, ours or another extension's.
_ANCHOR_RE = re.compile(r'<a\b[^>]*>.*?</a>', re.DOTALL)

#: Wraps a "Used In" entry's own text in the same ``<code class="xref ...">`` markup a real
#: ``:class:``/``:func:``/etc. cross-reference renders with, so a theme's own styling for those
#: (bold, a distinct color from a plain page link) applies here too -- for an entry that's
#: itself another documented object's own page (the "Docstring Examples" category), which is
#: exactly what a real xref would point at.
_XREF_OPEN = '<code class="xref py py-obj docutils literal notranslate">'
_XREF_CLOSE = '</code>'

#: Wraps a "Used In" entry's own text in the same ``<span class="std std-ref">`` markup a real
#: ``:ref:`` role renders with, plus an explicit bold weight: unlike a ``:class:``/``:func:``/
#: etc. cross-reference (rendered as ``<code>``, which most themes already bold on their own,
#: from a generic "code inside a link" rule -- no ``:ref:``-specific styling needed), a plain
#: ``<span>`` gets no such rule anywhere, in any theme checked so far. Without forcing it, a
#: ``:ref:``-style entry would end up visually identical to an uncategorized plain link, losing
#: the distinction the category itself draws. For :data:`DEFAULT_GALLERY_CATEGORY`
#: specifically: a real, structured page with a real anchor, same as what a ``:ref:`` points
#: at -- unlike an uncategorized or custom-tagged entry, which is just "some page" with
#: nothing that specific to point at, and stays a plain link.
_STD_REF_OPEN = '<span class="std std-ref" style="font-weight: bold;">'
_STD_REF_CLOSE = '</span>'

# Pygments token classes: ``n``/``nn``/``nc``/... for names, ``o`` for dots.
_NAME_SPAN = '<span class="n[a-zA-Z]{{0,2}}">{}</span>'
_DOT_SPAN = '<span class="o">.</span>'

#: A call's closing paren: ``)``, or merged ``()`` for a no-arg call.
_CALL_END = r'<span class="p">\(?\)</span>'


def _dotted_span_source(parts: tuple[str, ...]) -> str:
    """Build a regex source matching how Pygments is likely to render a dotted chain."""
    return _DOT_SPAN.join(_NAME_SPAN.format(re.escape(part)) for part in parts)


def _name_pattern_source(accessed: str) -> str:
    """Build a regex source matching how Pygments is likely to render ``accessed``."""
    return _dotted_span_source(tuple(accessed.split('.')))


@dataclass(frozen=True)
class _Candidate:
    """One accessed name and the documented names it might resolve to."""

    accessed: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class _CallCandidate:
    """A trailing attribute chain on a call's result, and its candidate names."""

    call_target: str
    trailing: tuple[str, ...]
    candidates: tuple[str, ...]


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
        #: e.g. ``('pv.Sphere', ('plot',))`` for ``pv.Sphere().plot``.
        self.call_chains: set[tuple[str, tuple[str, ...]]] = set()

    def visit_Name(self, node: ast.Name) -> None:
        """Record a bare name access."""
        self.accessed.add(node.id)

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
                self.call_chains.add((call_target, tuple(reversed(parts))))
        # e.g. `pv.Sphere().plot` -- keep walking the call's own arguments.
        self.visit(cursor)


def _accessed_names(source: str) -> set[str]:
    """Return every dotted name accessed in ``source``, or none on a parse error."""
    return _collect(source).accessed


def _call_chains(source: str) -> set[tuple[str, tuple[str, ...]]]:
    """Return every ``(call target, trailing attrs)`` pair, or none on a parse error."""
    return _collect(source).call_chains


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


def _candidate_names(accessed: str, namespace: dict[str, Any]) -> list[str]:
    """Return candidate documented names for one dotted name access.

    Tries every prefix of ``accessed`` against ``namespace``; the longest match
    wins, then walks the remaining attributes on that live object.
    """
    parts = accessed.split('.')
    for split in range(len(parts)):
        head = '.'.join(parts[: split + 1])
        if head not in namespace:
            continue
        obj = namespace[head]
        remainder = parts[split + 1 :]

        if inspect.ismodule(obj) and not remainder:
            return [obj.__name__]

        is_class_attr = False
        method: list[str] = []
        for level in remainder:
            owner = obj
            prop = getattr(type(owner), level, None) if not inspect.isclass(owner) else None
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
            # obj is itself a (sub)module (e.g. pv.examples) -- nothing below applies.
            return [obj.__name__]

        is_class = inspect.isclass(obj)
        if is_class or is_class_attr:
            return _class_candidates(obj if is_class else obj.__class__, method)

        if inspect.isroutine(obj):
            return list(_module_path_candidates(obj, []))

        return list(_module_path_candidates(obj.__class__, []))
    return []


#: Matches a bare dotted class name (``PolyData``); rejects ``Widget | str``, ``list[int]``.
_SIMPLE_NAME_RE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z')


def _resolve_object(accessed: str, namespace: dict[str, Any]) -> Any | None:
    """Resolve a dotted name to the live object it refers to, or ``None``."""
    parts = accessed.split('.')
    for split in range(len(parts)):
        head = '.'.join(parts[: split + 1])
        if head not in namespace:
            continue
        obj = namespace[head]
        for level in parts[split + 1 :]:
            try:
                obj = getattr(obj, level)
            except Exception:  # noqa: BLE001, PERF203 -- arbitrary objects can raise anything
                return None
        return obj
    return None


def _call_return_type(func: Any, namespace: dict[str, Any]) -> type | None:
    """Return ``func``'s return type, if its annotation names one plain, resolvable class.

    Checked against ``func``'s own module first, then every module already in
    ``namespace`` -- covers aliases ``func``'s module only imports under ``TYPE_CHECKING``.
    """
    annotation = getattr(func, '__annotations__', {}).get('return')
    if isinstance(annotation, type):
        return annotation
    if not isinstance(annotation, str) or not _SIMPLE_NAME_RE.match(annotation):
        return None
    name = annotation.rsplit('.', 1)[-1]
    namespaces = [getattr(func, '__globals__', {})]
    namespaces.extend(vars(obj) for obj in namespace.values() if inspect.ismodule(obj))
    for ns in namespaces:
        candidate = ns.get(name)
        if isinstance(candidate, type):
            return candidate
    return None


def _call_chain_candidates(
    call_target: str, trailing: tuple[str, ...], namespace: dict[str, Any]
) -> list[str]:
    """Return candidate documented names for a call's trailing attribute chain."""
    func = _resolve_object(call_target, namespace)
    if func is None or not inspect.isroutine(func):
        return []
    return_type = _call_return_type(func, namespace)
    if return_type is None:
        return []
    return _class_candidates(return_type, list(trailing))


def _records_for(source: str, namespace: dict[str, Any]) -> list[_Candidate | _CallCandidate]:
    """Return every resolved candidate for the identifiers accessed in ``source``."""
    records: list[_Candidate | _CallCandidate] = []
    collected = _collect(source)
    for accessed in sorted(collected.accessed):
        candidates = _candidate_names(accessed, namespace)
        if candidates:
            records.append(_Candidate(accessed, tuple(candidates)))
    for call_target, trailing in sorted(collected.call_chains):
        candidates = _call_chain_candidates(call_target, trailing, namespace)
        if candidates:
            records.append(_CallCandidate(call_target, trailing, tuple(candidates)))
    return records


def exec_with_local_scopes(
    code: CodeType, namespace: dict[str, Any], filename: str
) -> dict[str, Any]:
    """Execute ``code`` in ``namespace``, and return every local scope seen merged in.

    Traces the execution and captures every one of the script's own function
    calls' own locals, merging them in underneath ``namespace`` (which still
    wins on name conflicts). Only frames compiled from ``filename`` are
    captured. A local name bound in one of the script's own calls can shadow
    what globals (or a *different* call) bound under the same name, since
    everything captured is merged into one flat namespace rather than kept
    scope-by-scope.

    ``code`` is executed in ``namespace`` exactly as a plain ``exec(code,
    namespace)`` would -- ``namespace`` itself is unaffected by the tracing,
    only the returned dict differs.

    Uses ``sys.setprofile()`` rather than ``sys.settrace()``.
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


#: Suggested category (see :func:`record_namespace`) for code recorded from inside an
#: autodoc-documented object's own docstring (e.g. its Examples section), as opposed to a
#: hand-written page. Applied automatically when ``state`` is passed and no ``category`` is.
DEFAULT_DOCSTRING_EXAMPLE_CATEGORY = 'Docstring Examples'

#: Default category (see :func:`record_namespace`) :class:`~sphinx_autocodelink.gallery.
#: AutoCodeLinkScraper` tags every page it records.
DEFAULT_GALLERY_CATEGORY = 'Sphinx Gallery'


def is_inside_autodoc_desc(state: RSTState) -> bool:
    """Return whether ``state``'s directive is nested inside an object description.

    ``state`` is a directive's own ``self.state``, from anywhere in
    ``docutils.parsers.rst.Directive.run()``.
    """
    return bool(state.document.settings.env.temp_data.get('object'))


def _is_inside_desc_node(node: nodes.Node) -> bool:
    """Return whether ``node`` is nested inside an object description, by doctree ancestry.

    A ``doctree-read`` transform has no directive ``state`` to hand :func:`is_inside_autodoc_desc`
    -- but by then the whole page's tree, autodoc-documented docstrings included, is fully
    assembled, so walking up for an ``addnodes.desc`` ancestor is reliable here (unlike at
    directive-run time, when a docstring's own content is still a detached subtree).
    """
    parent = node.parent
    while parent is not None:
        if isinstance(parent, addnodes.desc):
            return True
        parent = parent.parent
    return False


def _record_bare_doctest_blocks(app: Sphinx, doctree: nodes.document) -> None:
    """Execute and record every bare ``>>>`` doctest block on the page.

    Opt-in via ``autocodelink_doctest_blocks`` -- see :func:`setup` for what this means and
    the risk it carries before enabling it. Unlike every other recording path in this
    extension, this one runs code the page's author never explicitly marked as executable
    (no ``.. autocodelink::``, no host directive calling :func:`record_namespace` itself) --
    purely because it happens to look like a doctest session. A block that fails to parse or
    raises while running (elided/pseudo-code, one relying on state from a separate block, one
    needing external resources) is skipped with a warning, not treated as a build failure.
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
            # Arbitrary code the page's author wrote, not this extension's own -- any
            # failure here must be skipped, never allowed to fail the build.
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
            env=env, docname=docname, source=code, namespace=namespace, category=category
        )


def record_namespace(
    *,
    env: BuildEnvironment,
    docname: str,
    source: str,
    namespace: dict[str, Any],
    category: str = '',
    state: RSTState | None = None,
) -> None:
    """Record candidate documented names for every identifier in ``source``.

    ``category`` optionally tags where this recording came from (e.g. ``'Sphinx
    Gallery'``), for grouping in ``.. autocodelink-index::`` output. Untagged pages
    display under a generic "Documentation" bucket when grouped.

    ``state`` (the calling directive's own ``self.state``) sets ``category`` to
    :data:`DEFAULT_DOCSTRING_EXAMPLE_CATEGORY` when it isn't already set and
    :func:`is_inside_autodoc_desc` is true for it.
    """
    if not category and state is not None and is_inside_autodoc_desc(state):
        category = DEFAULT_DOCSTRING_EXAMPLE_CATEGORY

    all_records: dict[str, list[_Candidate | _CallCandidate]] | None = getattr(env, _ENV_ATTR, None)
    if all_records is None:
        all_records = {}
        setattr(env, _ENV_ATTR, all_records)
    all_records.setdefault(docname, []).extend(_records_for(source, namespace))

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
) -> None:
    """Like :func:`record_namespace`, but appended to a file under ``directory``.

    For recording from a process Sphinx's own ``env-merge-info`` never sees
    -- e.g. Sphinx-Gallery's own parallel (joblib) example workers.
    """
    records = _records_for(source, namespace)
    if not records:
        return
    target = Path(directory) / f'{docname}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(target.read_text()) if target.exists() else {'records': []}
    existing['records'].extend(_to_jsonable(r) for r in records)
    if category:
        existing['category'] = category
    target.write_text(json.dumps(existing))


def _to_jsonable(record: _Candidate | _CallCandidate) -> dict[str, Any]:
    """Convert one record to a JSON-serializable dict."""
    if isinstance(record, _CallCandidate):
        return {
            'call_target': record.call_target,
            'trailing': list(record.trailing),
            'candidates': list(record.candidates),
        }
    return {'accessed': record.accessed, 'candidates': list(record.candidates)}


def _from_jsonable(entry: dict[str, Any]) -> _Candidate | _CallCandidate:
    """Convert one JSON dict back to a record."""
    if 'call_target' in entry:
        return _CallCandidate(
            entry['call_target'], tuple(entry['trailing']), tuple(entry['candidates'])
        )
    return _Candidate(entry['accessed'], tuple(entry['candidates']))


def _load_disk_records(
    directory: Path,
) -> tuple[dict[str, list[_Candidate | _CallCandidate]], dict[str, str]]:
    """Return every docname's records and category, written by :func:`record_namespace_to_disk`."""
    records: dict[str, list[_Candidate | _CallCandidate]] = {}
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

    our_categories: dict[str, str] = getattr(env, _CATEGORY_ATTR, {})
    their_categories: dict[str, str] = getattr(other, _CATEGORY_ATTR, {})
    our_categories.update(their_categories)
    setattr(env, _CATEGORY_ATTR, our_categories)


def _purge_doc(app: Sphinx, env: BuildEnvironment, docname: str) -> None:
    """Drop stale records for a document being re-read."""
    getattr(env, _ENV_ATTR, {}).pop(docname, None)
    getattr(env, _INDEX_DOCS_ATTR, set()).discard(docname)
    getattr(env, _CATEGORY_ATTR, {}).pop(docname, None)


# ---------------------------------------------------------------------------
# Phase 2: once the whole site's objects are known, match candidates against
# the real inventory and rewrite the already-built HTML.
# ---------------------------------------------------------------------------


def _local_inventory(app: Sphinx) -> dict[str, tuple[str, str]]:
    """Return ``{name: (docname, anchor)}`` for every locally documented Python object."""
    return {
        name: (entry.docname, entry.node_id)
        for name, entry in app.env.domains['py'].objects.items()
    }


def _aliased_names(app: Sphinx) -> set[str]:
    """Return every name registered only as a ``:canonical:`` cross-reference target.

    Sphinx auto-adds one of these -- pointing at the same page -- for an object documented
    under a shorter public alias than the module it's actually defined in (autodoc sets
    ``:canonical:`` whenever the two differ). The alias is what the object is actually
    documented under, and what its own docstring's backreferences are keyed by, so an
    aliased name should lose to a non-aliased one resolving to the same target.
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

    A class registers under more than one name in Sphinx's own object inventory -- e.g. its
    full defining-module path alongside its short public name -- both pointing at the exact
    same page and anchor. If the first match found is one of ``aliased`` (Sphinx's own
    ``:canonical:`` cross-reference, not the name the object is actually documented under),
    prefers a later candidate resolving to that same target that isn't -- an aliased name
    would silently fail to match how the object's own docstring looks up its
    backreferences, which is always keyed by the non-aliased name.
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

    records: dict[str, list[_Candidate | _CallCandidate]] = dict(getattr(app.env, _ENV_ATTR, {}))
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

    for docname, candidates in records.items():
        out_file = Path(app.outdir) / (app.builder.get_target_uri(docname))
        if not out_file.exists():
            continue

        # Dedup: the same accessed name or call chain can be recorded multiple times.
        resolved_names: dict[str, str] = {}
        resolved_calls: dict[tuple[str, tuple[str, ...]], str] = {}
        for candidate in candidates:
            if isinstance(candidate, _CallCandidate):
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
                    backrefs.setdefault(name, set()).add(docname)
        if not resolved_names and not resolved_calls:
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
        _fill_index_placeholders(
            app, index_docs, backrefs, local=local, external=external, categories=categories
        )


#: Matches one ``.. autocodelink-index::`` placeholder, ``data-name`` empty for the full index.
#: Placeholder options travel as one JSON blob (rather than growing positional data-*
#: attributes indefinitely): {"name": str, "hide_empty": bool, "group": "auto"|"always"|"never",
#: "titles": bool}.
_INDEX_PLACEHOLDER_RE = re.compile(
    r'<div class="sphinx-autocodelink-index" data-opts="([^"]*)"></div>'
)

#: Matches a ``:label:``-generated section (title + one placeholder), for the ``:hide-empty:``
#: case: removed as a whole -- title included -- rather than just its placeholder, so an
#: auto-injected "Used In" heading never sits over nothing. Assumes no directive nests
#: another section inside it, which none of ours do.
_BACKREFS_SECTION_RE = re.compile(
    r'<section\b[^>]*\bclass="[^"]*sphinx-autocodelink-backrefs[^"]*"[^>]*>.*?</section>',
    re.DOTALL,
)

#: Pulls a section's own ``id`` out of its opening tag, so a dropped ``:hide-empty:`` section
#: can also be found (and removed) in the page's separately-rendered in-page nav.
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


_COLLAPSE_THRESHOLD = 8
_COLLAPSE_VISIBLE = 5


#: Above this many hidden entries, the overflow list lays out in columns instead of one
#: long single-file scroll -- skimmable rather than a wall of links to nowhere.
_COLUMN_LAYOUT_THRESHOLD = 24


def _render_ref_list(
    refs: list[str],
    *,
    docname: str,
    app: Sphinx,
    show_titles: bool,
    categories: dict[str, str] | None = None,
) -> str:
    """Render ``<ul>`` link(s) to ``refs``, relative to ``docname``, sorted by display text.

    An entry recorded under :data:`DEFAULT_DOCSTRING_EXAMPLE_CATEGORY` -- itself another
    documented object's own page -- renders like a real ``:class:``/``:func:``/etc.
    cross-reference would. One under :data:`DEFAULT_GALLERY_CATEGORY` renders like a real
    ``:ref:`` instead, since that's exactly what it is: a specific, structured page with a
    real anchor, not an object. Anything else (a hand-written page, an uncategorized or
    custom-tagged one) is a plain page link -- there's no similarly specific real target to
    point at, just "some page, somewhere in the docs".

    Lists longer than ``_COLLAPSE_THRESHOLD`` show only the first ``_COLLAPSE_VISIBLE`` entries,
    with the rest tucked behind a ``<details>`` toggle rendered as one more ``<li>`` -- so it
    picks up the same indentation and spacing as its sibling entries for free, from whatever
    list styling the theme already applies, rather than trying to replicate it.
    """
    categories = categories or {}
    labeled = sorted((_docname_title(app, ref) if show_titles else ref, ref) for ref in refs)

    def render(entries: list[tuple[str, str]]) -> str:
        """Render a sequence of (label, ref) pairs as ``<li>`` entries."""
        items = []
        for label, ref in entries:
            href = app.builder.get_relative_uri(docname, ref)
            text = escape(label)
            category = categories.get(ref)
            if category == DEFAULT_DOCSTRING_EXAMPLE_CATEGORY:
                text = f'{_XREF_OPEN}{text}{_XREF_CLOSE}'
            elif category == DEFAULT_GALLERY_CATEGORY:
                text = f'{_STD_REF_OPEN}{text}{_STD_REF_CLOSE}'
            items.append(f'<li><a href="{href}">{text}</a></li>')
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


def _render_grouped_refs(
    refs: list[str],
    *,
    docname: str,
    app: Sphinx,
    categories: dict[str, str],
    show_titles: bool,
    group_mode: str,
) -> str:
    """Render ``refs`` as one flat list, or grouped by category depending on ``group_mode``."""
    groups: dict[str, list[str]] = {}
    for ref in refs:
        groups.setdefault(categories.get(ref, _UNCATEGORIZED_LABEL), []).append(ref)

    should_group = group_mode == 'always' or (group_mode != 'never' and len(groups) > 1)
    if not should_group:
        return _render_ref_list(
            refs, docname=docname, app=app, show_titles=show_titles, categories=categories
        )

    category_labels = getattr(app.config, 'autocodelink_category_labels', {})
    parts = []
    # Sorted by each group's own *displayed* label, not its underlying category string --
    # a renamed category (autocodelink_category_labels) must sort into place among the
    # names readers actually see, not the internal ones they never do.
    for category in sorted(groups, key=lambda c: category_labels.get(c, c)):
        label = category_labels.get(category, category)
        ref_list = _render_ref_list(
            groups[category],
            docname=docname,
            app=app,
            show_titles=show_titles,
            categories=categories,
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
    group_mode: str,
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
        group_mode=group_mode,
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
    group_mode: str = 'auto',
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
            group_mode=group_mode,
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
            group_mode=group_mode,
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
    group_mode: str = 'auto',
) -> str:
    """Render the site-wide index: every resolved name and its referencing pages."""
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
            group_mode=group_mode,
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
            group_mode=opts['group'],
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
    """Connect the autodoc backrefs hook once every extension's own events are registered.

    ``builder-inited`` fires after every extension's ``setup()`` has run, so this is the
    first point ``autodoc-process-docstring`` reliably exists if autodoc is used at all --
    regardless of whether autodoc (or numpydoc, which depends on it) is listed before or
    after this extension in ``extensions``.
    """
    if not getattr(app.config, 'autocodelink_autodoc_backrefs', False):
        return
    if 'autodoc-process-docstring' not in app.events.events:
        return
    app.connect('autodoc-process-docstring', _inject_backref_index)


#: Default value of the ``autocodelink_records_dir`` config value, and of
#: :class:`sphinx_autocodelink.gallery.AutoCodeLinkScraper`'s ``records_dir`` -- matching
#: defaults mean disk-based recording works without configuring either explicitly.
DEFAULT_RECORDS_DIR = '_autocodelink_records'


def setup(app: Sphinx) -> dict[str, bool]:
    """Wire up dynamic autolinking.

    Registers the ``.. autocodelink::`` and ``.. autocodelink-index::``
    directives and the event hooks that resolve and embed links --
    everything needed to use this extension on its own. A consumer that
    already executes code for its own purposes can instead (or additionally)
    call :func:`record_namespace` directly and call this from its own
    ``setup(app)``.

    Each code source is opt-in by use, not by configuration: the
    ``.. autocodelink::`` directive only affects blocks that use it, and
    :class:`sphinx_autocodelink.gallery.AutoCodeLinkScraper` only records
    when added to a ``sphinx_gallery_conf['image_scrapers']``.

    ``autocodelink_autodoc_backrefs`` (default ``False``) appends a "Used
    in" backreferences index to every autodoc-documented object's own
    docstring, via ``autodoc-process-docstring``. Objects with no
    references get nothing appended, not an empty "No references found."

    ``autocodelink_category_labels`` (default ``{}``) renames a recorded
    category's own display label in grouped ``.. autocodelink-index::``
    output, e.g. ``{'Sphinx Gallery': 'Gallery Examples'}`` -- without
    changing the category string itself, which is what ``:category:``,
    :class:`~sphinx_autocodelink.gallery.AutoCodeLinkScraper`'s own
    ``category``, and :func:`record_namespace` calls are actually matched
    and grouped by. A category with no entry displays under its own name
    unchanged; that includes ``'Documentation'``, the default for anything
    recorded with no category at all.

    ``autocodelink_doctest_blocks`` (default ``False``) is the one exception to "opt-in by
    use": once enabled, *every* bare ``>>>`` doctest block anywhere in the docs -- in a
    docstring's Examples section, in a hand-written page, anywhere -- is executed and its
    identifiers recorded, with no ``.. autocodelink::`` needed on any of them individually.
    This is the only way this extension ever executes code on its own initiative, rather
    than observing code something else already executes for its own purposes (a host
    directive, Sphinx-Gallery). Understand what that means before enabling it:

    - It runs code the page's author never marked as runnable, purely because it looks
      like a doctest session -- including in third-party docstrings pulled in via
      ``autodoc`` from dependencies you may not have fully read.
    - A failing block (elided/pseudo-code, one relying on a variable from a separate
      block, one needing a resource that isn't there at build time) is skipped with a
      warning rather than failing the build, but it still *ran* first, with whatever
      side effects that entailed, before the failure surfaced.
    - Each block executes in its own fresh namespace -- a later block cannot see a name
      bound by an earlier one, even within the same docstring's Examples section.
    """
    from sphinx_autocodelink._directive import AutoCodeLink
    from sphinx_autocodelink._directive import AutoCodeLinkIndex

    app.connect('builder-inited', _clear_disk_records)
    app.connect('env-merge-info', _merge_records)
    app.connect('env-purge-doc', _purge_doc)
    # Priority > 500 (Sphinx's default): run after other build-finished handlers, e.g.
    # Sphinx-Gallery's own `reference_url`-driven link embedding, which does not check
    # for spans already inside an anchor -- running after it lets our own such check
    # (which does) skip whatever it already wrapped, instead of nesting inside it.
    app.connect('build-finished', _embed_links, priority=900)
    app.connect('builder-inited', _register_autodoc_hook)
    app.connect('doctree-read', _record_bare_doctest_blocks)
    app.add_config_value('autocodelink_records_dir', DEFAULT_RECORDS_DIR, rebuild='html')
    app.add_config_value('autocodelink_autodoc_backrefs', False, rebuild='html')
    app.add_config_value('autocodelink_category_labels', {}, rebuild='html')
    app.add_config_value('autocodelink_doctest_blocks', False, rebuild='html')
    app.add_directive('autocodelink', AutoCodeLink)
    app.add_directive('autocodelink-index', AutoCodeLinkIndex)
    return {'parallel_read_safe': True, 'parallel_write_safe': True}
