"""Unit tests for sphinx_autocodelink internals, without a full Sphinx build."""

from __future__ import annotations

import ast
from collections import namedtuple
import dataclasses
from enum import Enum
import functools
import inspect
import re
import sys
import types
from types import SimpleNamespace
from typing import Literal
from typing import overload

from docutils import nodes
import pytest
from sphinx import addnodes

import sphinx_autocodelink as autolink
from sphinx_autocodelink._tracing import monitoring_available

# typing.get_overloads was added in 3.11 -- see sphinx_autocodelink's own import guard.
try:
    from typing import get_overloads
except ImportError:  # Python 3.10
    get_overloads = None

needs_get_overloads = pytest.mark.skipif(
    get_overloads is None, reason='typing.get_overloads added in Python 3.11'
)

needs_monitoring = pytest.mark.skipif(
    not monitoring_available(), reason='sys.monitoring added in Python 3.12'
)


def test_accessed_names_syntax_error():
    assert autolink._collect('def bad(:\n').accessed == set()


def test_accessed_names_call_chain_not_rooted_in_name():
    # `.plot` on a call result has nothing to look up, but the inner `pv.Sphere` does.
    assert autolink._collect('pv.Sphere().plot()').accessed == {'pv.Sphere'}


def test_accessed_names_bare_name():
    assert autolink._collect('x').accessed == {'x'}


def test_dotted_name_not_rooted_in_name():
    call_node = ast.parse('a()', mode='eval').body
    assert autolink._dotted_name(call_node) is None


def test_module_path_candidates_no_module():
    class Foo:
        pass

    Foo.__module__ = 'nonexistent_test_module_xyz'
    assert list(autolink._module_path_candidates(Foo, [])) == []


def test_module_path_candidates_no_qualname():
    import functools

    # functools.partial: isroutine() is true, but it has no __qualname__ of its own.
    partial_instance = functools.partial(print)
    assert list(autolink._module_path_candidates(partial_instance, [])) == []


def test_candidate_names_unresolvable():
    assert autolink._candidate_names('totally_undefined_name', {}) == []


def test_candidate_names_bound_method():
    class Widget:
        def draw(self):
            return None

    namespace = {'widget': Widget()}
    candidates = autolink._candidate_names('widget.draw', namespace)
    assert any(c.endswith('Widget.draw') for c in candidates)


def test_candidate_names_base_class():
    class Base:
        def draw(self):
            return None

    class Derived(Base):
        pass

    namespace = {'obj': Derived()}
    candidates = autolink._candidate_names('obj.draw', namespace)
    assert any(c.endswith('Base.draw') for c in candidates)


def test_candidate_names_property():
    class Widget:
        @property
        def name(self):
            return 'widget'

    namespace = {'widget': Widget()}
    candidates = autolink._candidate_names('widget.name.upper', namespace)
    assert any(c.endswith('Widget.name') for c in candidates)


def test_candidate_names_metaclass_property():
    # A property on the metaclass (e.g. an enum's classproperty) is invoked when accessed
    # on the class itself, so it must be caught the same way an instance property is.
    class Meta(type):
        @property
        def dimension_map(cls):
            return {0: frozenset()}

    class CellType(metaclass=Meta):
        pass

    namespace = {'CellType': CellType}
    candidates = autolink._candidate_names('CellType.dimension_map', namespace)
    assert any(c.endswith('CellType.dimension_map') for c in candidates)


def test_candidate_names_getattr_raises():
    # object() has no .nonexistent attribute -- like a variable reassigned mid-script.
    namespace = {'x': object()}
    assert autolink._candidate_names('x.nonexistent.deep', namespace) == list(
        autolink._module_path_candidates(object, [])
    )


def test_candidate_names_module_reexported_function():
    # A function documented under the internal module it's actually defined in, not
    # the package that re-exports it -- e.g. pyvista.examples.load_uniform, defined
    # in (and documented at) pyvista.examples.examples.
    internal = types.ModuleType('fakepkg.internal')

    def load_uniform():
        return None

    load_uniform.__module__ = 'fakepkg.internal'
    load_uniform.__qualname__ = 'load_uniform'
    internal.load_uniform = load_uniform
    pkg = types.ModuleType('fakepkg')
    pkg.load_uniform = load_uniform
    pkg.internal = internal

    sys.modules['fakepkg.internal'] = internal
    sys.modules['fakepkg'] = pkg
    try:
        candidates = autolink._candidate_names('pkg.load_uniform', {'pkg': pkg})
    finally:
        del sys.modules['fakepkg.internal']
        del sys.modules['fakepkg']

    assert candidates == ['fakepkg.internal.load_uniform', 'fakepkg.load_uniform']


def test_candidate_names_module_attribute_is_module():
    sub = types.ModuleType('fakepkg.sub')
    pkg = types.ModuleType('fakepkg')
    pkg.sub = sub

    assert autolink._candidate_names('pkg.sub', {'pkg': pkg}) == ['fakepkg.sub']


def test_candidate_names_bare_module():
    pkg = types.ModuleType('fakepkg')
    assert autolink._candidate_names('pkg', {'pkg': pkg}) == ['fakepkg']


def test_call_chains_no_intermediate_variable():
    assert autolink._collect('pv.Sphere().plot()').call_chains == {('pv.Sphere', ('plot',))}


def test_call_chains_bound_method():
    assert autolink._collect('mesh.copy().plot()').call_chains == {('mesh.copy', ('plot',))}


def test_call_chains_multi_attribute_trailing():
    chains = autolink._collect('pv.Sphere().points.size').call_chains
    assert chains == {('pv.Sphere', ('points', 'size'))}


def test_resolve_object():
    def target():
        return None

    ns = SimpleNamespace(target=target)
    assert autolink._resolve_object('ns.target', {'ns': ns}) is target
    assert autolink._resolve_object('ns.nonexistent', {'ns': ns}) is None
    assert autolink._resolve_object('undefined', {}) is None


class _FallbackNamespaceReturnType:
    """Used only by test_call_return_type_resolves_via_fallback_namespace."""


def test_call_return_type_resolves_via_fallback_namespace():
    # The annotation isn't in make_thing's own globals (as if imported only under
    # TYPE_CHECKING there), but is on another module already in the namespace.
    exec_ns: dict = {}
    exec(  # noqa: S102 -- constructs a function with controlled __globals__
        compile(
            'def make_thing() -> _FallbackNamespaceReturnType:\n    return None\n',
            '<fake>',
            'exec',
        ),
        exec_ns,
    )
    make_thing = exec_ns['make_thing']

    assert '_FallbackNamespaceReturnType' not in make_thing.__globals__
    this_module = sys.modules[__name__]
    assert (
        autolink._call_return_type(make_thing, {'this_module': this_module})
        is _FallbackNamespaceReturnType
    )


def test_call_return_type_union_with_no_resolvable_member():
    def make_widget_or_gadget() -> Widget | Gadget:  # noqa: F821
        return ''

    assert autolink._call_return_type(make_widget_or_gadget, {}) is None


def test_call_return_type_resolves_builtin():
    # Under `from __future__ import annotations` (this file's own convention, and
    # pyvista's), the annotation is a lazy string -- `str` itself is never looked up by
    # Python, so it has to be found in the builtins fallback rather than the live object.
    def make_string() -> str:
        return ''

    assert autolink._call_return_type(make_string, {}) is str


class _CallReturnTypeUnionMember:
    """Used only by test_call_return_type_resolves_first_resolvable_union_member."""


def test_call_return_type_resolves_first_resolvable_union_member():
    # `T | str` is pyvista's own `download_*(load: bool = True)` pattern: the dataset
    # class comes first, a bare filename second, and the class is what should win.
    def make_thing_or_string() -> _CallReturnTypeUnionMember | str:
        return ''

    assert autolink._call_return_type(make_thing_or_string, {}) is _CallReturnTypeUnionMember


def test_call_return_type_no_annotation():
    def plain():
        return None

    assert autolink._call_return_type(plain, {}) is None


def test_call_return_type_already_a_type():
    def make_thing():
        return None

    # e.g. a function defined without `from __future__ import annotations`.
    make_thing.__annotations__['return'] = str
    assert autolink._call_return_type(make_thing, {}) is str


def test_call_return_type_unresolvable_name():
    def make_thing() -> NonexistentClassXYZ:  # noqa: F821
        return None

    assert autolink._call_return_type(make_thing, {}) is None


class _CallChainCandidatesReturnType:
    def method(self) -> None:
        """Do nothing."""


def test_call_chain_candidates():
    def make_thing() -> _CallChainCandidatesReturnType:
        return _CallChainCandidatesReturnType()

    candidates = autolink._call_chain_candidates(
        'make_thing', ('method',), {'make_thing': make_thing}
    )
    assert any(c.endswith('_CallChainCandidatesReturnType.method') for c in candidates)


def test_call_chain_candidates_union_return_type():
    # e.g. `examples.download_lucy().triangulate()`: the downloader's `T | str` toggle
    # return type must not sink linking the method called directly on its result.
    def download_thing() -> _CallChainCandidatesReturnType | str:
        return _CallChainCandidatesReturnType()

    candidates = autolink._call_chain_candidates(
        'download_thing', ('method',), {'download_thing': download_thing}
    )
    assert any(c.endswith('_CallChainCandidatesReturnType.method') for c in candidates)


def _parse_call(source: str) -> ast.Call:
    """Parse a single call expression, for the overload-matching helpers below."""
    return ast.parse(source, mode='eval').body


def test_literal_annotation_values_single():
    assert autolink._literal_annotation_values('Literal[True]') == {True}


def test_literal_annotation_values_multiple():
    assert autolink._literal_annotation_values("Literal['a', 'b']") == {'a', 'b'}


def test_literal_annotation_values_typing_prefix():
    assert autolink._literal_annotation_values('typing.Literal[False]') == {False}


def test_literal_annotation_values_not_a_literal():
    assert autolink._literal_annotation_values('PolyData') is None


def test_literal_annotation_values_unclosed_bracket():
    assert autolink._literal_annotation_values('Literal[') is None


def test_literal_annotation_values_body_not_literal_evaluable():
    # Matches the `Literal[...]` shape, but its body is a name reference, not a literal.
    assert autolink._literal_annotation_values('Literal[some_name]') is None


def test_bound_literal_args_positional():
    def f(load=True): ...

    call = _parse_call('f(False)')
    assert autolink._bound_literal_args(call, inspect.signature(f)) == {'load': False}


def test_bound_literal_args_positional_non_literal_marked_unresolved():
    def f(load=True): ...

    call = _parse_call('f(some_variable)')
    assert autolink._bound_literal_args(call, inspect.signature(f)) == {
        'load': autolink._UNRESOLVED_ARG
    }


def test_bound_literal_args_keyword():
    def f(load=True): ...

    call = _parse_call('f(load=False)')
    assert autolink._bound_literal_args(call, inspect.signature(f)) == {'load': False}


def test_bound_literal_args_non_literal_marked_unresolved():
    def f(load=True): ...

    call = _parse_call('f(load=some_variable)')
    assert autolink._bound_literal_args(call, inspect.signature(f)) == {
        'load': autolink._UNRESOLVED_ARG
    }


def test_bound_literal_args_ignores_star_args():
    def f(load=True): ...

    call = _parse_call('f(*args, **kwargs)')
    assert autolink._bound_literal_args(call, inspect.signature(f)) == {}


@overload
def _overload_true(load: Literal[True] = True) -> int: ...
@overload
def _overload_true(load: Literal[False]) -> str: ...
def _overload_true(load=True):
    return 1 if load else 'a'


@needs_get_overloads
def test_overload_matches_via_explicit_literal():
    matches = [
        ov
        for ov in get_overloads(_overload_true)
        if autolink._overload_matches(ov, {'load': False})
    ]
    assert len(matches) == 1
    assert matches[0].__annotations__['return'] == 'str'


@needs_get_overloads
def test_overload_matches_via_default():
    # `load` wasn't passed at all -- falls back to the True-overload's own default.
    matches = [ov for ov in get_overloads(_overload_true) if autolink._overload_matches(ov, {})]
    assert len(matches) == 1
    assert matches[0].__annotations__['return'] == 'int'


@needs_get_overloads
def test_overload_matches_none_for_unresolved_arg():
    # A non-literal argument can't be checked against either overload's Literal, so
    # neither is ruled in -- but neither is ruled out by name-only presence either.
    matches = [
        ov
        for ov in get_overloads(_overload_true)
        if autolink._overload_matches(ov, {'load': autolink._UNRESOLVED_ARG})
    ]
    assert matches == []


def test_overload_matches_ignores_unannotated_param():
    def overload_no_annotation(load=True) -> int: ...

    assert autolink._overload_matches(overload_no_annotation, {'load': False})


def test_overload_matches_ignores_non_literal_annotation():
    def overload_bool_annotation(load: bool = True) -> int: ...

    assert autolink._overload_matches(overload_bool_annotation, {'load': False})


@needs_get_overloads
def test_resolve_via_overloads_unresolvable_matched_return_type():
    @overload
    def make_thing(load: Literal[True] = True) -> NonexistentClassXYZ: ...  # noqa: F821
    @overload
    def make_thing(load: Literal[False]) -> str: ...
    def make_thing(load=True):
        return None if load else ''

    return_type = autolink._resolve_via_overloads(make_thing, [_parse_call('make_thing()')], {})
    assert return_type is None


@needs_get_overloads
def test_resolve_via_overloads_single_call_site():
    return_type = autolink._resolve_via_overloads(
        _overload_true, [_parse_call('_overload_true(load=False)')], {}
    )
    assert return_type is str


@needs_get_overloads
def test_resolve_via_overloads_call_omits_argument():
    return_type = autolink._resolve_via_overloads(
        _overload_true, [_parse_call('_overload_true()')], {}
    )
    assert return_type is int


@needs_get_overloads
def test_resolve_via_overloads_agreeing_call_sites():
    calls = [_parse_call('_overload_true(load=False)'), _parse_call('_overload_true(False)')]
    assert autolink._resolve_via_overloads(_overload_true, calls, {}) is str


@needs_get_overloads
def test_resolve_via_overloads_disagreeing_call_sites():
    calls = [_parse_call('_overload_true(load=False)'), _parse_call('_overload_true()')]
    assert autolink._resolve_via_overloads(_overload_true, calls, {}) is None


@needs_get_overloads
def test_resolve_via_overloads_non_literal_argument():
    calls = [_parse_call('_overload_true(load=some_variable)')]
    assert autolink._resolve_via_overloads(_overload_true, calls, {}) is None


def test_resolve_via_overloads_no_overloads_registered():
    def plain(load=True):
        return load

    calls = [_parse_call('plain(load=False)')]
    assert autolink._resolve_via_overloads(plain, calls, {}) is None


def test_resolve_via_overloads_no_calls():
    assert autolink._resolve_via_overloads(_overload_true, [], {}) is None


class _OverloadChainCandidatesReturnType:
    def method(self) -> None:
        """Do nothing."""


@overload
def _download_thing(load: Literal[True] = True) -> _OverloadChainCandidatesReturnType: ...
@overload
def _download_thing(load: Literal[False]) -> str: ...
def _download_thing(load=True):
    return _OverloadChainCandidatesReturnType() if load else 'a'


@needs_get_overloads
def test_call_chain_candidates_uses_matching_overload():
    # The default -- `load` omitted entirely -- should resolve to the dataset-returning
    # overload, not just the first-listed union member (which happens to agree here, but
    # only because of the fallback -- see the next test for where that would go wrong).
    calls = [_parse_call('download_thing()')]
    candidates = autolink._call_chain_candidates(
        'download_thing', ('method',), {'download_thing': _download_thing}, calls
    )
    assert any(c.endswith('_OverloadChainCandidatesReturnType.method') for c in candidates)


@needs_get_overloads
def test_call_chain_candidates_uses_non_default_overload():
    # `load=False` returns `str`, not the dataset type a naive first-union-member guess
    # would pick -- `.method` isn't a `str` attribute, so no candidate should claim it is.
    calls = [_parse_call('download_thing(load=False)')]
    candidates = autolink._call_chain_candidates(
        'download_thing', ('method',), {'download_thing': _download_thing}, calls
    )
    assert not any(c.endswith('_OverloadChainCandidatesReturnType.method') for c in candidates)
    assert any(c.endswith('str.method') for c in candidates)


def test_call_chain_candidates_unresolvable_target():
    assert autolink._call_chain_candidates('undefined', ('plot',), {}) == []


def test_call_chain_candidates_unresolvable_return_type():
    def make_thing():
        return None

    candidates = autolink._call_chain_candidates(
        'make_thing', ('attr',), {'make_thing': make_thing}
    )
    assert candidates == []


class _LocalScopeThing:
    def method(self) -> None:
        """Do nothing."""


def test_exec_with_local_scopes_resolves_helper_function_locals():
    # `thing` only ever exists inside `helper`'s own local scope -- never at module level --
    # exactly the case a plain exec() can't resolve (see #contour_labels/anatomical_groups.py).
    code = 'def helper():\n    thing = make_thing()\n    thing.method()\nhelper()\n'
    namespace = {'make_thing': lambda: _LocalScopeThing()}
    filename = '<test>'
    resolved = autolink.exec_with_local_scopes(compile(code, filename, 'exec'), namespace, filename)

    assert isinstance(resolved['thing'], _LocalScopeThing)
    # the passed-in namespace itself still executes normally, unaffected by tracing.
    assert 'thing' not in namespace
    assert 'helper' in namespace


def test_exec_with_local_scopes_ignores_library_internals():
    # a local variable inside a call into ANOTHER file must not leak into resolution --
    # only frames compiled from `filename` are captured.
    def library_call():
        unrelated_local = object()  # noqa: F841

    code = 'library_call()\n'
    namespace = {'library_call': library_call}
    filename = '<test>'
    resolved = autolink.exec_with_local_scopes(compile(code, filename, 'exec'), namespace, filename)

    assert 'unrelated_local' not in resolved


def test_exec_with_local_scopes_namespace_wins_over_captured_locals():
    # if a local shares a name with something in the passed-in namespace, namespace wins.
    code = 'def helper():\n    shared = 1\nhelper()\n'
    namespace = {'shared': 'global-value'}
    filename = '<test>'
    resolved = autolink.exec_with_local_scopes(compile(code, filename, 'exec'), namespace, filename)

    assert resolved['shared'] == 'global-value'


def test_exec_with_local_scopes_restores_prior_profile_function():
    def sentinel_profiler(frame, event, arg):
        pass

    old_profile = sys.getprofile()
    sys.setprofile(sentinel_profiler)
    try:
        filename = '<test>'
        autolink.exec_with_local_scopes(compile('x = 1\n', filename, 'exec'), {}, filename)
        assert sys.getprofile() is sentinel_profiler
    finally:
        sys.setprofile(old_profile)


class _RecordNamespaceReturnType:
    def method(self) -> None:
        """Do nothing."""


def test_record_namespace():
    def make_thing() -> _RecordNamespaceReturnType:
        return _RecordNamespaceReturnType()

    env = SimpleNamespace()
    autolink.record_namespace(
        env=env,
        docname='index',
        source='make_thing().method',
        namespace={'make_thing': make_thing},
    )
    records = getattr(env, autolink._ENV_ATTR)
    call_candidates = [r for r in records['index'] if isinstance(r, autolink._CallCandidate)]
    assert call_candidates == [
        autolink._CallCandidate(
            'make_thing', ('method',), ('test_autocodelink._RecordNamespaceReturnType.method',)
        )
    ]


def test_is_inside_autodoc_desc_true_when_object_is_set():
    state = SimpleNamespace(
        document=SimpleNamespace(
            settings=SimpleNamespace(env=SimpleNamespace(temp_data={'object': 'pkg.thing'}))
        )
    )
    assert autolink.is_inside_autodoc_desc(state) is True


def test_is_inside_autodoc_desc_false_when_object_is_empty():
    state = SimpleNamespace(
        document=SimpleNamespace(
            settings=SimpleNamespace(env=SimpleNamespace(temp_data={'object': ''}))
        )
    )
    assert autolink.is_inside_autodoc_desc(state) is False


def test_record_namespace_state_sets_docstring_example_category_when_unset():
    env = SimpleNamespace()
    state = SimpleNamespace(
        document=SimpleNamespace(
            settings=SimpleNamespace(env=SimpleNamespace(temp_data={'object': 'pkg.thing'}))
        )
    )
    autolink.record_namespace(env=env, docname='api', source='x', namespace={'x': 1}, state=state)
    assert (
        getattr(env, autolink._CATEGORY_ATTR)['api'] == autolink.DEFAULT_DOCSTRING_EXAMPLE_CATEGORY
    )


def test_record_namespace_state_does_not_override_explicit_category():
    env = SimpleNamespace()
    state = SimpleNamespace(
        document=SimpleNamespace(
            settings=SimpleNamespace(env=SimpleNamespace(temp_data={'object': 'pkg.thing'}))
        )
    )
    autolink.record_namespace(
        env=env, docname='api', source='x', namespace={'x': 1}, category='Tutorials', state=state
    )
    assert getattr(env, autolink._CATEGORY_ATTR)['api'] == 'Tutorials'


def test_is_inside_desc_node_true_when_a_desc_ancestor_exists():
    desc = addnodes.desc()
    block = nodes.doctest_block()
    desc += block
    assert autolink._is_inside_desc_node(block) is True


def test_is_inside_desc_node_false_with_no_desc_ancestor():
    section = nodes.section()
    block = nodes.doctest_block()
    section += block
    assert autolink._is_inside_desc_node(block) is False


class _Widget:
    """A minimal stand-in with a real ``@property``, for counts_as_use tests."""

    @property
    def name(self) -> str:
        """Return a constant name."""
        return 'widget'

    def draw(self) -> None:
        """Do nothing."""


def test_records_for_call_site_counts_as_use():
    records = autolink._records_for('Widget()\n', {'Widget': _Widget})
    (record,) = [r for r in records if r.accessed == 'Widget']
    assert record.counts_as_use is True


def test_records_for_method_call_counts_as_use():
    records = autolink._records_for('w.draw()\n', {'w': _Widget()})
    (record,) = [r for r in records if r.accessed == 'w.draw']
    assert record.counts_as_use is True


def test_records_for_property_read_counts_as_use():
    records = autolink._records_for('w.name\n', {'w': _Widget()})
    (record,) = [r for r in records if r.accessed == 'w.name']
    assert record.counts_as_use is True


def test_records_for_bare_mention_does_not_count_as_use():
    # Neither called nor a property read -- e.g. `isinstance(w, Widget)`, or a bare
    # variable passed around without being invoked or having a property read off it.
    records = autolink._records_for('print(w)\n', {'w': _Widget()})
    (record,) = [r for r in records if r.accessed == 'w']
    assert record.counts_as_use is False


class _Color(Enum):
    """A minimal Enum, for counts_as_use tests covering enum-member access."""

    RED = 1
    BLUE = 2


def test_records_for_enum_member_access_counts_as_use():
    # e.g. `pv.CellType.HEXAHEDRON` -- pulling a member off a class is a real usage.
    records = autolink._records_for('Color.RED\n', {'Color': _Color})
    (record,) = [r for r in records if r.accessed == 'Color.RED']
    assert record.counts_as_use is True


def test_records_for_bare_class_reference_does_not_count_as_use():
    # e.g. `isinstance(x, pv.CellType)` -- naming the class itself, not a member.
    records = autolink._records_for('print(Color)\n', {'Color': _Color})
    (record,) = [r for r in records if r.accessed == 'Color']
    assert record.counts_as_use is False


def test_records_for_uncalled_method_reference_does_not_count_as_use():
    records = autolink._records_for('callback = w.draw\n', {'w': _Widget()})
    (record,) = [r for r in records if r.accessed == 'w.draw']
    assert record.counts_as_use is False


def test_records_for_uncalled_staticmethod_reference_does_not_count_as_use():
    records = autolink._records_for('callback = Widget.draw\n', {'Widget': _Widget})
    (record,) = [r for r in records if r.accessed == 'Widget.draw']
    assert record.counts_as_use is False


class _Sized:
    """A minimal stand-in with a ``cached_property`` and a plain instance attribute."""

    def __init__(self) -> None:
        self.label = 'sized'

    @functools.cached_property
    def area(self) -> int:
        """Return a constant area."""
        return 42


def test_records_for_cached_property_read_counts_as_use():
    # functools.cached_property doesn't subclass property -- a different code path.
    records = autolink._records_for('s.area\n', {'s': _Sized()})
    (record,) = [r for r in records if r.accessed == 's.area']
    assert record.counts_as_use is True


def test_records_for_plain_instance_attribute_counts_as_use():
    records = autolink._records_for('s.label\n', {'s': _Sized()})
    (record,) = [r for r in records if r.accessed == 's.label']
    assert record.counts_as_use is True


def test_records_for_namedtuple_field_counts_as_use():
    point = namedtuple('Point', ['x', 'y'])(1, 2)
    records = autolink._records_for('p.x\n', {'p': point})
    (record,) = [r for r in records if r.accessed == 'p.x']
    assert record.counts_as_use is True


def test_records_for_module_constant_counts_as_use():
    mod = types.ModuleType('fake_mod')
    mod.CONST = 3.14
    records = autolink._records_for('mod.CONST\n', {'mod': mod})
    (record,) = [r for r in records if r.accessed == 'mod.CONST']
    assert record.counts_as_use is True


def test_records_for_bare_submodule_reference_does_not_count_as_use():
    parent = types.ModuleType('fake_parent')
    parent.sub = types.ModuleType('fake_parent.sub')
    records = autolink._records_for('print(parent.sub)\n', {'parent': parent})
    (record,) = [r for r in records if r.accessed == 'parent.sub']
    assert record.counts_as_use is False


@dataclasses.dataclass
class _Point:
    """A minimal dataclass, for counts_as_use tests covering dataclass fields."""

    x: int
    y: int = 0

    @property
    def magnitude(self) -> float:
        """Return the distance from the origin."""
        return (self.x**2 + self.y**2) ** 0.5


def test_records_for_dataclass_field_counts_as_use():
    records = autolink._records_for('p.x\n', {'p': _Point(1, 2)})
    (record,) = [r for r in records if r.accessed == 'p.x']
    assert record.counts_as_use is True


def test_records_for_dataclass_property_counts_as_use():
    records = autolink._records_for('p.magnitude\n', {'p': _Point(1, 2)})
    (record,) = [r for r in records if r.accessed == 'p.magnitude']
    assert record.counts_as_use is True


def _doctree_with_doctest_blocks(*sources):
    """Return a document whose children are ``doctest_block`` nodes, one per source."""
    document = nodes.document(settings=SimpleNamespace(), reporter=SimpleNamespace())
    for source in sources:
        document += nodes.doctest_block(source, source)
    return document


def test_record_bare_doctest_blocks_disabled_by_default():
    doctree = _doctree_with_doctest_blocks('>>> x = 1\n')
    env = SimpleNamespace(docname='index')
    app = SimpleNamespace(env=env, config=SimpleNamespace())
    autolink._record_bare_doctest_blocks(app, doctree)
    assert getattr(env, autolink._ENV_ATTR, None) is None


def test_record_bare_doctest_blocks_records_identifiers():
    doctree = _doctree_with_doctest_blocks('>>> import re\n>>> pattern = re.compile("x")\n')
    env = SimpleNamespace(docname='index')
    app = SimpleNamespace(env=env, config=SimpleNamespace(autocodelink_doctest_blocks=True))
    autolink._record_bare_doctest_blocks(app, doctree)
    records = getattr(env, autolink._ENV_ATTR)['index']
    assert any(r.accessed == 're.compile' for r in records if isinstance(r, autolink._Candidate))


def test_record_bare_doctest_blocks_tags_docstring_example_category():
    desc = addnodes.desc()
    block = nodes.doctest_block('>>> 1 + 1\n', '>>> 1 + 1\n')
    desc += block
    document = nodes.document(settings=SimpleNamespace(), reporter=SimpleNamespace())
    document += desc
    env = SimpleNamespace(docname='index')
    app = SimpleNamespace(env=env, config=SimpleNamespace(autocodelink_doctest_blocks=True))
    autolink._record_bare_doctest_blocks(app, document)
    assert (
        getattr(env, autolink._CATEGORY_ATTR)['index']
        == autolink.DEFAULT_DOCSTRING_EXAMPLE_CATEGORY
    )


def test_record_bare_doctest_blocks_skips_unparseable_block():
    doctree = _doctree_with_doctest_blocks('>>> this is not )( valid python\n')
    env = SimpleNamespace(docname='index')
    app = SimpleNamespace(env=env, config=SimpleNamespace(autocodelink_doctest_blocks=True))
    autolink._record_bare_doctest_blocks(app, doctree)
    assert getattr(env, autolink._ENV_ATTR, None) is None


def test_record_bare_doctest_blocks_skips_a_raising_block():
    doctree = _doctree_with_doctest_blocks('>>> raise ValueError("boom")\n')
    env = SimpleNamespace(docname='index')
    app = SimpleNamespace(env=env, config=SimpleNamespace(autocodelink_doctest_blocks=True))
    autolink._record_bare_doctest_blocks(app, doctree)
    assert getattr(env, autolink._ENV_ATTR, None) is None


def test_record_bare_doctest_blocks_each_block_gets_a_fresh_namespace():
    # A name bound in one block isn't visible to a later, separate block: the second
    # block's reference to `shared` raises NameError and is skipped, so only the first
    # block's own assignment is recorded.
    doctree = _doctree_with_doctest_blocks('>>> shared = 1\n', '>>> shared + 1\n')
    env = SimpleNamespace(docname='index')
    app = SimpleNamespace(env=env, config=SimpleNamespace(autocodelink_doctest_blocks=True))
    autolink._record_bare_doctest_blocks(app, doctree)
    records = getattr(env, autolink._ENV_ATTR)['index']
    # Bare name, not called and not a property read -- doesn't count as a "use".
    assert records == [autolink._Candidate('shared', ('builtins.int',), counts_as_use=False)]


def test_record_namespace_to_disk_no_records(tmp_path):
    autolink.record_namespace_to_disk(
        directory=tmp_path, docname='index', source='x = 1', namespace={}
    )
    assert list(tmp_path.iterdir()) == []


def test_record_namespace_to_disk_and_load(tmp_path):
    def make_thing() -> _RecordNamespaceReturnType:
        return _RecordNamespaceReturnType()

    autolink.record_namespace_to_disk(
        directory=tmp_path,
        docname='examples/plot_thing',
        source='make_thing().method',
        namespace={'make_thing': make_thing},
    )
    loaded, _ = autolink._load_disk_records(tmp_path)
    call_candidates = [
        r for r in loaded['examples/plot_thing'] if isinstance(r, autolink._CallCandidate)
    ]
    assert call_candidates == [
        autolink._CallCandidate(
            'make_thing', ('method',), ('test_autocodelink._RecordNamespaceReturnType.method',)
        )
    ]


def test_record_namespace_to_disk_appends(tmp_path):
    autolink.record_namespace_to_disk(
        directory=tmp_path, docname='index', source='x', namespace={'x': _RecordNamespaceReturnType}
    )
    autolink.record_namespace_to_disk(
        directory=tmp_path, docname='index', source='x', namespace={'x': _RecordNamespaceReturnType}
    )
    loaded, _ = autolink._load_disk_records(tmp_path)
    assert len(loaded['index']) == 2


def test_record_namespace_to_disk_with_category(tmp_path):
    autolink.record_namespace_to_disk(
        directory=tmp_path,
        docname='examples/plot_thing',
        source='x',
        namespace={'x': _RecordNamespaceReturnType},
        category='Sphinx Gallery',
    )
    _, categories = autolink._load_disk_records(tmp_path)
    assert categories == {'examples/plot_thing': 'Sphinx Gallery'}


def test_load_disk_records_missing_directory(tmp_path):
    assert autolink._load_disk_records(tmp_path / 'does-not-exist') == ({}, {})


def test_clear_disk_records_disabled():
    app = SimpleNamespace(config=SimpleNamespace(), srcdir='/nonexistent')
    assert autolink._clear_disk_records(app) is None


def test_clear_disk_records_removes_directory(tmp_path):
    records_dir = tmp_path / 'src' / '_autocodelink_records'
    records_dir.mkdir(parents=True)
    (records_dir / 'index.json').write_text('[]')
    app = SimpleNamespace(
        config=SimpleNamespace(autocodelink_records_dir='_autocodelink_records'),
        srcdir=str(tmp_path / 'src'),
    )
    autolink._clear_disk_records(app)
    assert not records_dir.exists()


def test_merge_records():
    env = SimpleNamespace()
    other = SimpleNamespace()
    setattr(other, autolink._ENV_ATTR, {'doc1': ['x']})
    autolink._merge_records(None, env, [], other)
    assert getattr(env, autolink._ENV_ATTR) == {'doc1': ['x']}


def test_purge_doc():
    env = SimpleNamespace()
    setattr(env, autolink._ENV_ATTR, {'doc1': ['x'], 'doc2': ['y']})
    autolink._purge_doc(None, env, 'doc1')
    assert getattr(env, autolink._ENV_ATTR) == {'doc2': ['y']}


def test_setup():
    connected = {}
    priorities = {}
    config_values = {}
    directives = {}

    class FakeApp:
        def connect(self, event, handler, priority=500):
            connected.setdefault(event, []).append(handler)
            priorities[(event, handler)] = priority

        def add_config_value(self, name, default, rebuild, types=()):
            config_values[name] = (default, rebuild, types)

        def add_directive(self, name, directive):
            directives[name] = directive

    result = autolink.setup(FakeApp())

    assert connected == {
        'builder-inited': [autolink._clear_disk_records, autolink._register_autodoc_hook],
        'config-inited': [autolink._wire_gallery_tracing],
        'env-merge-info': [autolink._merge_records],
        'env-purge-doc': [autolink._purge_doc],
        'build-finished': [autolink._embed_links],
        'doctree-read': [autolink._record_bare_doctest_blocks],
    }
    # runs after other build-finished handlers at Sphinx's default priority (500) -- see
    # setup()'s docstring for why (Sphinx-Gallery's reference_url embedding, notably).
    assert priorities[('build-finished', autolink._embed_links)] == 900
    assert config_values == {
        'autocodelink_records_dir': (autolink.DEFAULT_RECORDS_DIR, 'html', ()),
        'autocodelink_autodoc_backrefs': (False, 'html', ()),
        'autocodelink_category_labels': ({}, 'html', ()),
        'autocodelink_category_order': ((), 'html', (list, tuple)),
        'autocodelink_doctest_blocks': (False, 'html', ()),
        'autocodelink_sort': ('alphabetical', 'html', ()),
        'autocodelink_show_usage_count': (False, 'html', ()),
        'autocodelink_gallery_cards': (False, 'html', ()),
    }
    assert directives.keys() == {'autocodelink', 'autocodelink-index'}
    assert result == {'parallel_read_safe': True, 'parallel_write_safe': True}


def test_intersphinx_inventory():
    env = SimpleNamespace(
        intersphinx_cache={},
        intersphinx_inventory={
            'py:function': {
                'external.thing': ('external', '1.0', 'https://example.invalid/thing.html', '-'),
            },
        },
        intersphinx_named_inventory={},
    )
    app = SimpleNamespace(env=env)
    assert autolink._intersphinx_inventory(app) == {
        'external.thing': 'https://example.invalid/thing.html',
    }


def test_aliased_names():
    objects = {
        'pkg.Thing': SimpleNamespace(aliased=False),
        'pkg.mod.Thing': SimpleNamespace(aliased=True),
        'pkg.other': SimpleNamespace(aliased=False),
    }
    app = SimpleNamespace(env=SimpleNamespace(domains={'py': SimpleNamespace(objects=objects)}))
    assert autolink._aliased_names(app) == {'pkg.mod.Thing'}


def test_resolve_link_external():
    resolved = autolink._resolve_link(
        ('external.thing',),
        docname='index',
        app=None,
        local={},
        external={'external.thing': 'https://example.invalid/thing.html'},
    )
    assert resolved == ('external.thing', 'https://example.invalid/thing.html')


def test_resolve_link_prefers_a_non_aliased_name_for_the_same_target():
    # Both names point at the same page; the non-aliased one is what the object is
    # documented under, and what its backreferences are keyed by.
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    resolved = autolink._resolve_link(
        ('pkg.mod.Thing', 'pkg.Thing'),
        docname='index',
        app=app,
        local={'pkg.mod.Thing': ('api', 'pkg.Thing'), 'pkg.Thing': ('api', 'pkg.Thing')},
        external={},
        aliased=frozenset({'pkg.mod.Thing'}),
    )
    assert resolved == ('pkg.Thing', 'api.html#pkg.Thing')


def test_resolve_link_keeps_an_aliased_name_with_no_non_aliased_alternative():
    # If every candidate resolving to this target is aliased, there's nothing better to
    # fall back to -- return it rather than nothing.
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    resolved = autolink._resolve_link(
        ('pkg.mod.Thing',),
        docname='index',
        app=app,
        local={'pkg.mod.Thing': ('api', 'pkg.mod.Thing')},
        external={},
        aliased=frozenset({'pkg.mod.Thing'}),
    )
    assert resolved == ('pkg.mod.Thing', 'api.html#pkg.mod.Thing')


def test_resolve_link_does_not_prefer_a_non_aliased_name_at_a_different_target():
    # A later candidate resolving to a genuinely different page (e.g. a base-class
    # fallback) must not be preferred just for being non-aliased -- only an alternative
    # name for the exact same target is interchangeable with an aliased match.
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    resolved = autolink._resolve_link(
        ('pkg.mod.Thing', 'pkg.Base'),
        docname='index',
        app=app,
        local={'pkg.mod.Thing': ('api', 'pkg.mod.Thing'), 'pkg.Base': ('other', 'pkg.Base')},
        external={},
        aliased=frozenset({'pkg.mod.Thing'}),
    )
    assert resolved == ('pkg.mod.Thing', 'api.html#pkg.mod.Thing')


def test_render_ref_list_shows_all_entries_at_or_under_the_threshold():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    refs = [f'page{i}' for i in range(autolink._COLLAPSE_THRESHOLD)]
    html = autolink._render_ref_list(refs, docname='index', app=app, show_titles=False)
    assert html.count('<li>') == autolink._COLLAPSE_THRESHOLD
    assert '<details' not in html


def test_render_ref_list_collapses_past_the_threshold():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    refs = [f'page{i}' for i in range(autolink._COLLAPSE_THRESHOLD + 1)]
    html = autolink._render_ref_list(refs, docname='index', app=app, show_titles=False)
    assert html.count('<li>') == autolink._COLLAPSE_THRESHOLD + 1
    hidden = autolink._COLLAPSE_THRESHOLD + 1 - autolink._COLLAPSE_VISIBLE
    assert f'<summary>{hidden} more</summary>' in html
    before_details = html.split('<details', 1)[0]
    assert before_details.count('<li>') == autolink._COLLAPSE_VISIBLE


def test_strip_nav_links_to_removes_matching_li():
    html = (
        '<ul><li class="toc-h2"><a class="nav-link" href="#autocodelink-pkg-thing">'
        'Used In</a></li><li class="toc-h2"><a href="#other">Other</a></li></ul>'
    )
    result = autolink._strip_nav_links_to(html, {'autocodelink-pkg-thing'})
    assert 'Used In' not in result
    assert '<a href="#other">Other</a>' in result


def test_strip_nav_links_to_ignores_unrelated_ids():
    html = '<li><a href="#unrelated">Unrelated</a></li>'
    assert autolink._strip_nav_links_to(html, {'autocodelink-pkg-thing'}) == html


def test_fill_index_placeholders_drops_dangling_nav_link_for_hidden_section(tmp_path):
    # Mimics a theme (e.g. pydata-sphinx-theme) that bakes an "on this page" nav from the
    # doctree before :hide-empty: drops the body section -- the nav link must go too.
    html = (
        '<html><body>'
        '<nav><ul><li class="toc-h2 nav-item toc-entry">'
        '<a class="reference internal nav-link" href="#autocodelink-pkg-unused">'
        'Used In</a></li></ul></nav>'
        '<section class="sphinx-autocodelink-backrefs" id="autocodelink-pkg-unused">'
        '<h2>Used In</h2>'
        '<div class="sphinx-autocodelink-index" data-opts="'
        '{&quot;name&quot;: &quot;pkg.unused&quot;, &quot;hide_empty&quot;: true, '
        '&quot;titles&quot;: true, &quot;group&quot;: &quot;auto&quot;}"></div>'
        '</section>'
        '</body></html>'
    )
    out_file = tmp_path / 'api.html'
    out_file.write_text(html, encoding='utf-8')

    app = SimpleNamespace(
        outdir=str(tmp_path),
        builder=SimpleNamespace(get_target_uri=lambda docname: f'{docname}.html'),
    )
    autolink._fill_index_placeholders(app, {'api'}, {}, local={}, external={}, categories={})

    result = out_file.read_text(encoding='utf-8')
    assert 'sphinx-autocodelink-backrefs' not in result
    assert 'Used In' not in result


def test_render_ref_list_more_toggle_is_its_own_list_item():
    # A sibling <li> picks up the same indentation/spacing as the other entries from
    # whatever list styling the theme already applies -- no bespoke CSS to keep in sync.
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    refs = [f'page{i}' for i in range(autolink._COLLAPSE_THRESHOLD + 1)]
    html = autolink._render_ref_list(refs, docname='index', app=app, show_titles=False)
    assert '<li class="sphinx-autocodelink-index-more"><details>' in html


def test_render_ref_list_no_column_layout_below_the_column_threshold():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    refs = [f'page{i}' for i in range(autolink._COLLAPSE_THRESHOLD + 1)]
    html = autolink._render_ref_list(refs, docname='index', app=app, show_titles=False)
    assert 'columns:' not in html


def test_render_ref_list_columns_past_the_column_threshold():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    refs = [
        f'page{i}'
        for i in range(autolink._COLLAPSE_VISIBLE + autolink._COLUMN_LAYOUT_THRESHOLD + 1)
    ]
    html = autolink._render_ref_list(refs, docname='index', app=app, show_titles=False)
    assert 'columns: 16em;' in html


def test_render_grouped_refs_bolds_the_group_label():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_category_labels={}),
    )
    html = autolink._render_grouped_refs(
        ['a', 'b'],
        docname='index',
        app=app,
        categories={'a': 'Docstring Examples', 'b': 'Guides'},
        show_titles=False,
        group_mode='always',
    )
    assert '<p class="sphinx-autocodelink-index-group-label"><strong>Docstring Examples' in html


def test_render_grouped_refs_sorts_by_the_renamed_label_not_the_category():
    # Unrenamed, alphabetical order would be 'Docstring Examples', 'Documentation',
    # 'Sphinx Gallery' -- but renamed to 'Zeta', 'Alpha', 'Beta', the *renamed* order
    # ('Alpha', 'Beta', 'Zeta') is what must show up.
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(
            autocodelink_category_labels={
                'Docstring Examples': 'Zeta',
                'Documentation': 'Alpha',
                'Sphinx Gallery': 'Beta',
            }
        ),
    )
    html = autolink._render_grouped_refs(
        ['a', 'b', 'c'],
        docname='index',
        app=app,
        categories={'a': 'Docstring Examples', 'b': 'Documentation', 'c': 'Sphinx Gallery'},
        show_titles=False,
        group_mode='always',
    )
    labels = re.findall(r'<strong>([^<]*)</strong>', html)
    assert labels == ['Alpha', 'Beta', 'Zeta']


def test_sorted_categories_defaults_to_alphabetical_by_label():
    groups = {'Sphinx Gallery': ['c'], 'Docstring Examples': ['a'], 'Documentation': ['b']}
    result = autolink._sorted_categories(groups, {}, (), 'index')
    assert result == ['Docstring Examples', 'Documentation', 'Sphinx Gallery']


def test_sorted_categories_respects_explicit_order():
    groups = {'Sphinx Gallery': ['c'], 'Docstring Examples': ['a'], 'Documentation': ['b']}
    order = ('Docstring Examples', 'Documentation', 'Sphinx Gallery')
    result = autolink._sorted_categories(groups, {}, order, 'index')
    assert result == list(order)


def test_sorted_categories_gallery_last_via_partial_order():
    groups = {'Sphinx Gallery': ['c'], 'Docstring Examples': ['a'], 'Documentation': ['b']}
    order = ('Docstring Examples', 'Documentation')
    result = autolink._sorted_categories(groups, {}, order, 'index')
    assert result == ['Docstring Examples', 'Documentation', 'Sphinx Gallery']


def test_sorted_categories_warns_about_a_category_missing_from_the_order(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        autolink._logger, 'warning', lambda *args, **kwargs: warnings.append((args, kwargs))
    )
    groups = {'Sphinx Gallery': ['c'], 'Docstring Examples': ['a']}
    autolink._sorted_categories(groups, {}, ('Docstring Examples',), 'index')
    assert len(warnings) == 1
    assert "'Sphinx Gallery'" in warnings[0][0][2]
    assert warnings[0][1]['location'] == 'index'


def test_sorted_categories_no_warning_when_order_is_complete(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        autolink._logger, 'warning', lambda *args, **kwargs: warnings.append((args, kwargs))
    )
    groups = {'Sphinx Gallery': ['c'], 'Docstring Examples': ['a']}
    autolink._sorted_categories(groups, {}, ('Docstring Examples', 'Sphinx Gallery'), 'index')
    assert warnings == []


def test_render_grouped_refs_sorts_by_explicit_category_order():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(
            autocodelink_category_labels={},
            autocodelink_category_order=('Sphinx Gallery', 'Docstring Examples'),
        ),
    )
    html = autolink._render_grouped_refs(
        ['a', 'b'],
        docname='index',
        app=app,
        categories={'a': 'Docstring Examples', 'b': 'Sphinx Gallery'},
        show_titles=False,
        group_mode='always',
    )
    labels = re.findall(r'<strong>([^<]*)</strong>', html)
    assert labels == ['Sphinx Gallery', 'Docstring Examples']


def test_render_grouped_refs_threads_usage_counts_into_each_group():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_category_labels={}, autocodelink_sort='frequency'),
    )
    html = autolink._render_grouped_refs(
        ['a', 'b', 'c'],
        docname='index',
        app=app,
        categories={'a': 'Docstring Examples', 'b': 'Docstring Examples', 'c': 'Documentation'},
        show_titles=False,
        group_mode='always',
        usage_counts={'a': 1, 'b': 9},
    )
    # 'Docstring Examples' sorts before 'Documentation' alphabetically either way -- the
    # real signal here is 'b' (usage 9) landing before 'a' (usage 1) within that group.
    order = re.findall(r'href="([abc])\.html"', html)
    assert order == ['b', 'a', 'c']


def test_render_ref_list_wraps_a_docstring_example_entry_as_a_cross_reference():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    html = autolink._render_ref_list(
        ['a'],
        docname='index',
        app=app,
        show_titles=False,
        categories={'a': autolink.DEFAULT_DOCSTRING_EXAMPLE_CATEGORY},
    )
    assert (
        '<a href="a.html"><code class="xref py py-obj docutils literal notranslate">a</code></a>'
        in html
    )


def test_render_ref_list_wraps_a_page_style_entry_as_a_ref_role():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    html = autolink._render_ref_list(
        ['a'], docname='index', app=app, show_titles=False, categories={'a': 'Sphinx Gallery'}
    )
    assert html == (
        '<ul class="sphinx-autocodelink-index"><li><a href="a.html">'
        f'{autolink._STD_REF_OPEN}a{autolink._STD_REF_CLOSE}</a></li></ul>'
    )


def test_render_ref_list_leaves_an_uncategorized_entry_plain():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    html = autolink._render_ref_list(['a'], docname='index', app=app, show_titles=False)
    assert html == '<ul class="sphinx-autocodelink-index"><li><a href="a.html">a</a></li></ul>'


def test_render_ref_list_leaves_a_custom_category_entry_plain():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_gallery_cards=False),
    )
    html = autolink._render_ref_list(
        ['a'], docname='index', app=app, show_titles=False, categories={'a': 'Tutorials'}
    )
    assert html == '<ul class="sphinx-autocodelink-index"><li><a href="a.html">a</a></li></ul>'


def _no_doctree(_docname):
    """Stand in for env.get_doctree when no doctree is needed.

    render_gallery_carousel falls back to the page's title (already supplied) if this raises.
    """
    msg = 'no doctree'
    raise Exception(msg)  # noqa: TRY002 -- matches an arbitrary env failure


def _gallery_app(*, gallery_cards):
    """Build a fake app with just enough of env/builder for render_gallery_carousel to run.

    ``srcdir`` points nowhere real -- ``_thumbnail_source_path`` globs it looking for a
    thumbnail file, and a nonexistent directory just glob()s to nothing, same as a real
    one this page's example was never actually rendered in. These tests are about
    _render_ref_list's own gallery-cards routing, not thumbnail resolution.
    """
    return SimpleNamespace(
        srcdir='/nonexistent-test-srcdir',
        builder=SimpleNamespace(
            get_relative_uri=lambda _from, to: f'{to}.html',
            get_target_uri=lambda docname: f'{docname}.html',
        ),
        env=SimpleNamespace(
            titles={'a': nodes.title('A', 'A')}, images={}, get_doctree=_no_doctree
        ),
        config=SimpleNamespace(autocodelink_gallery_cards=gallery_cards),
    )


def test_render_ref_list_gallery_cards_disabled_by_default_uses_plain_list():
    app = _gallery_app(gallery_cards=False)
    html = autolink._render_ref_list(
        ['a'], docname='index', app=app, show_titles=False, categories={'a': 'Sphinx Gallery'}
    )
    assert 'sd-cards-carousel' not in html
    assert html.startswith('<ul class="sphinx-autocodelink-index">')


def test_render_ref_list_gallery_cards_enabled_renders_carousel():
    app = _gallery_app(gallery_cards=True)
    html = autolink._render_ref_list(
        ['a'], docname='index', app=app, show_titles=False, categories={'a': 'Sphinx Gallery'}
    )
    assert 'sd-cards-carousel' in html
    assert '<ul' not in html


def test_render_ref_list_gallery_cards_mixed_categories():
    app = _gallery_app(gallery_cards=True)
    app.env.titles['b'] = nodes.title('B', 'B')
    html = autolink._render_ref_list(
        ['a', 'b'],
        docname='index',
        app=app,
        show_titles=True,
        categories={'a': 'Sphinx Gallery', 'b': 'Documentation'},
    )
    assert 'sd-cards-carousel' in html
    assert '<ul class="sphinx-autocodelink-index"><li><a href="b.html">B</a></li></ul>' in html


def test_render_ref_list_gallery_cards_no_gallery_entries_unaffected():
    app = _gallery_app(gallery_cards=True)
    html = autolink._render_ref_list(
        ['a'], docname='index', app=app, show_titles=False, categories={'a': 'Documentation'}
    )
    assert 'sd-cards-carousel' not in html
    assert html == '<ul class="sphinx-autocodelink-index"><li><a href="a.html">a</a></li></ul>'


def test_render_ref_list_sort_alphabetical_by_default():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(),
    )
    html = autolink._render_ref_list(['b', 'a', 'c'], docname='index', app=app, show_titles=False)
    order = re.findall(r'href="([abc])\.html"', html)
    assert order == ['a', 'b', 'c']


def test_render_ref_list_sort_frequency_ranks_by_usage_count():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='frequency'),
    )
    html = autolink._render_ref_list(
        ['a', 'b', 'c'],
        docname='index',
        app=app,
        show_titles=False,
        usage_counts={'a': 1, 'b': 5, 'c': 3},
    )
    order = re.findall(r'href="([abc])\.html"', html)
    assert order == ['b', 'c', 'a']


def test_render_ref_list_sort_frequency_shows_usage_count():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='frequency', autocodelink_show_usage_count=True),
    )
    html = autolink._render_ref_list(
        ['a', 'b'],
        docname='index',
        app=app,
        show_titles=False,
        usage_counts={'a': 1, 'b': 5},
    )
    # Outside the <a> -- not part of the link itself.
    assert (
        '<a href="a.html">a</a> <span class="sphinx-autocodelink-usage-count">(1 use)</span>'
        in html
    )
    assert (
        '<a href="b.html">b</a> <span class="sphinx-autocodelink-usage-count">(5 uses)</span>'
        in html
    )


def test_render_ref_list_sort_frequency_shows_zero_for_an_uncounted_ref():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='frequency', autocodelink_show_usage_count=True),
    )
    html = autolink._render_ref_list(
        ['a'], docname='index', app=app, show_titles=False, usage_counts={}
    )
    assert '(0 uses)' in html


def test_render_ref_list_sort_alphabetical_shows_no_usage_count():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='alphabetical'),
    )
    html = autolink._render_ref_list(
        ['a'], docname='index', app=app, show_titles=False, usage_counts={'a': 5}
    )
    assert 'sphinx-autocodelink-usage-count' not in html
    assert html == '<ul class="sphinx-autocodelink-index"><li><a href="a.html">a</a></li></ul>'


def test_render_ref_list_sort_alphabetical_shows_usage_count_when_enabled():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(
            autocodelink_sort='alphabetical', autocodelink_show_usage_count=True
        ),
    )
    html = autolink._render_ref_list(
        ['b', 'a'], docname='index', app=app, show_titles=False, usage_counts={'a': 1, 'b': 5}
    )
    # Sort stays alphabetical -- 'a' still before 'b' -- but counts still show.
    order = re.findall(r'href="([ab])\.html"', html)
    assert order == ['a', 'b']
    assert '<span class="sphinx-autocodelink-usage-count">(1 use)</span>' in html
    assert '<span class="sphinx-autocodelink-usage-count">(5 uses)</span>' in html


def test_render_ref_list_sort_frequency_hides_usage_count_when_disabled():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='frequency'),
    )
    html = autolink._render_ref_list(
        ['a', 'b'], docname='index', app=app, show_titles=False, usage_counts={'a': 1, 'b': 5}
    )
    # Ranking still applies -- 'b' (5 uses) before 'a' (1 use) -- counts just aren't shown.
    order = re.findall(r'href="([ab])\.html"', html)
    assert order == ['b', 'a']
    assert 'sphinx-autocodelink-usage-count' not in html


def test_render_ref_list_sort_frequency_ties_broken_alphabetically():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='frequency'),
    )
    html = autolink._render_ref_list(
        ['c', 'a', 'b'],
        docname='index',
        app=app,
        show_titles=False,
        usage_counts={'a': 2, 'b': 2, 'c': 2},
    )
    order = re.findall(r'href="([abc])\.html"', html)
    assert order == ['a', 'b', 'c']


def test_render_ref_list_sort_frequency_missing_count_sinks_to_bottom():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='frequency'),
    )
    html = autolink._render_ref_list(
        ['a', 'b'], docname='index', app=app, show_titles=False, usage_counts={'a': 1}
    )
    order = re.findall(r'href="([ab])\.html"', html)
    assert order == ['a', 'b']


def test_render_ref_list_sort_frequency_no_counts_falls_back_to_alphabetical():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='frequency'),
    )
    html = autolink._render_ref_list(['b', 'a'], docname='index', app=app, show_titles=False)
    order = re.findall(r'href="([ab])\.html"', html)
    assert order == ['a', 'b']


def test_render_ref_list_sort_frequency_orders_the_collapse_split():
    app = SimpleNamespace(
        builder=SimpleNamespace(get_relative_uri=lambda _from, to: f'{to}.html'),
        config=SimpleNamespace(autocodelink_sort='frequency'),
    )
    refs = [f'page{i}' for i in range(autolink._COLLAPSE_THRESHOLD + 1)]
    # Reverse of index order, so the most-used page is last alphabetically but should
    # still land in the *visible* (not collapsed) section under frequency sort.
    usage_counts = {ref: len(refs) - i for i, ref in enumerate(refs)}
    html = autolink._render_ref_list(
        refs, docname='index', app=app, show_titles=False, usage_counts=usage_counts
    )
    before_details = html.split('<details', 1)[0]
    visible = re.findall(r'href="(page\d+)\.html"', before_details)
    assert visible == refs[: autolink._COLLAPSE_VISIBLE]


def test_embed_links_skips_on_exception():
    app = SimpleNamespace(builder=SimpleNamespace(format='html'))
    assert autolink._embed_links(app, Exception('build failed')) is None


def test_embed_links_skips_non_html_builder():
    app = SimpleNamespace(builder=SimpleNamespace(format='text'))
    assert autolink._embed_links(app, None) is None


def test_embed_links_no_records():
    app = SimpleNamespace(
        builder=SimpleNamespace(format='html'), env=SimpleNamespace(), config=SimpleNamespace()
    )
    assert autolink._embed_links(app, None) is None


def _fake_env():
    return SimpleNamespace(
        intersphinx_cache={},
        intersphinx_inventory={},
        intersphinx_named_inventory={},
        domains={'py': SimpleNamespace(objects={})},
    )


def _fake_app(env, tmp_path):
    return SimpleNamespace(
        builder=SimpleNamespace(
            format='html',
            get_target_uri=lambda docname: f'{docname}.html',
            get_relative_uri=lambda _from, to: to,
        ),
        outdir=str(tmp_path),
        srcdir=str(tmp_path),
        env=env,
        config=SimpleNamespace(autocodelink_records_dir=None),
    )


def test_embed_links_missing_output_file(tmp_path):
    env = _fake_env()
    setattr(env, autolink._ENV_ATTR, {'missing_doc': [autolink._Candidate('name', ('x.Foo',))]})
    app = _fake_app(env, tmp_path)
    assert autolink._embed_links(app, None) is None


def test_embed_links_no_resolved_candidates(tmp_path):
    out_file = tmp_path / 'exists_doc.html'
    out_file.write_text('<html><body><span class="n">name</span></body></html>')

    env = _fake_env()
    setattr(env, autolink._ENV_ATTR, {'exists_doc': [autolink._Candidate('name', ('x.Foo',))]})
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)
    assert out_file.read_text() == '<html><body><span class="n">name</span></body></html>'


def test_embed_links_call_chain(tmp_path):
    # `pv.Sphere().plot()`, syntax-highlighted.
    html = (
        '<pre><span class="n">pv</span><span class="o">.</span><span class="n">Sphere</span>'
        '<span class="p">()</span><span class="o">.</span><span class="n">plot</span>'
        '<span class="p">()</span></pre>'
    )
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    env.domains['py'].objects['pyvista.PolyData.plot'] = SimpleNamespace(
        docname='api', node_id='pyvista.PolyData.plot', aliased=False
    )
    setattr(
        env,
        autolink._ENV_ATTR,
        {'index': [autolink._CallCandidate('pv.Sphere', ('plot',), ('pyvista.PolyData.plot',))]},
    )
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)
    result = out_file.read_text()

    # only `.plot` is linked -- `pv.Sphere()` and the trailing `()` stay outside.
    assert '<span class="n">Sphere</span></a>' not in result
    assert (
        '<a class="sphinx-autocodelink-a" href="api#pyvista.PolyData.plot">'
        '<span class="o">.</span><span class="n">plot</span></a>' in result
    )
    assert re.search(r'<a\b[^>]*><a\b', result) is None


def test_embed_links_merges_disk_records(tmp_path):
    html = '<pre><span class="n">mesh</span></pre>'
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    env.domains['py'].objects['test_autocodelink._RecordNamespaceReturnType'] = SimpleNamespace(
        docname='api', node_id='test_autocodelink._RecordNamespaceReturnType', aliased=False
    )
    records_dir = tmp_path / 'records'
    autolink.record_namespace_to_disk(
        directory=records_dir,
        docname='index',
        source='mesh',
        namespace={'mesh': _RecordNamespaceReturnType()},
    )
    app = _fake_app(env, tmp_path)
    app.config.autocodelink_records_dir = 'records'
    autolink._embed_links(app, None)
    result = out_file.read_text()

    assert 'href="api#test_autocodelink._RecordNamespaceReturnType"' in result


def test_embed_links_call_chain_and_plain_candidate_coexist(tmp_path):
    html = (
        '<pre><span class="n">pv</span><span class="o">.</span><span class="n">Sphere</span>'
        '<span class="p">()</span><span class="o">.</span><span class="n">plot</span>'
        '<span class="p">()</span></pre>'
    )
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    env.domains['py'].objects['pyvista.PolyData.plot'] = SimpleNamespace(
        docname='api', node_id='pyvista.PolyData.plot', aliased=False
    )
    env.domains['py'].objects['pyvista.Sphere'] = SimpleNamespace(
        docname='api2', node_id='pyvista.Sphere', aliased=False
    )
    setattr(
        env,
        autolink._ENV_ATTR,
        {
            'index': [
                autolink._CallCandidate('pv.Sphere', ('plot',), ('pyvista.PolyData.plot',)),
                autolink._Candidate('pv.Sphere', ('pyvista.Sphere',)),
            ]
        },
    )
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)
    result = out_file.read_text()

    assert 'href="api2#pyvista.Sphere"' in result
    assert 'href="api#pyvista.PolyData.plot"' in result
    assert re.search(r'<a\b[^>]*><a\b', result) is None


def test_embed_links_call_chain_dedup(tmp_path):
    html = (
        '<pre><span class="n">pv</span><span class="o">.</span><span class="n">Sphere</span>'
        '<span class="p">()</span><span class="o">.</span><span class="n">plot</span>'
        '<span class="p">()</span></pre>'
    )
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    env.domains['py'].objects['pyvista.PolyData.plot'] = SimpleNamespace(
        docname='api', node_id='pyvista.PolyData.plot', aliased=False
    )
    # recorded twice, e.g. referenced from two documented functions on the same page.
    setattr(
        env,
        autolink._ENV_ATTR,
        {
            'index': [
                autolink._CallCandidate('pv.Sphere', ('plot',), ('pyvista.PolyData.plot',)),
                autolink._CallCandidate('pv.Sphere', ('plot',), ('pyvista.PolyData.plot',)),
            ]
        },
    )
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)
    result = out_file.read_text()

    assert result.count('href="api#pyvista.PolyData.plot"') == 1
    assert re.search(r'<a\b[^>]*><a\b', result) is None


def test_embed_links_call_chain_already_linked(tmp_path):
    # Sphere()...plot already wrapped in one anchor, e.g. by another extension --
    # unlike our own wrap, the `)` stays adjacent to `.plot`, so the pattern still
    # matches, and the already-linked check has to catch it instead.
    html = (
        '<pre><span class="n">pv</span><span class="o">.</span>'
        '<a class="other-extension-a" href="somewhere">'
        '<span class="n">Sphere</span><span class="p">()</span>'
        '<span class="o">.</span><span class="n">plot</span></a><span class="p">()</span></pre>'
    )
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    env.domains['py'].objects['pyvista.PolyData.plot'] = SimpleNamespace(
        docname='api', node_id='pyvista.PolyData.plot', aliased=False
    )
    setattr(
        env,
        autolink._ENV_ATTR,
        {'index': [autolink._CallCandidate('pv.Sphere', ('plot',), ('pyvista.PolyData.plot',))]},
    )
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)
    assert out_file.read_text() == html


def test_embed_links_call_chain_unresolvable(tmp_path):
    html = '<pre><span class="n">x</span></pre>'
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    setattr(
        env,
        autolink._ENV_ATTR,
        {'index': [autolink._CallCandidate('x', ('plot',), ('nowhere.Foo.plot',))]},
    )
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)
    assert out_file.read_text() == html


def test_embed_links_zero_use_candidate_omitted_from_used_in(tmp_path):
    # `index` only ever mentions `pkg.thing` bare (not called, not a property read) --
    # e.g. a type hint or an `isinstance` check -- so it shouldn't show up in "Used In",
    # even though the mention is still resolvable and still gets its own in-source link.
    (tmp_path / 'index.html').write_text('<pre><span class="n">thing</span></pre>')
    api_html = (
        '<html><body>'
        '<section class="sphinx-autocodelink-backrefs" id="autocodelink-pkg-thing">'
        '<h2>Used In</h2>'
        '<div class="sphinx-autocodelink-index" data-opts="'
        '{&quot;name&quot;: &quot;pkg.thing&quot;, &quot;hide_empty&quot;: false, '
        '&quot;titles&quot;: false, &quot;group&quot;: &quot;auto&quot;}"></div>'
        '</section>'
        '</body></html>'
    )
    (tmp_path / 'api.html').write_text(api_html)

    env = _fake_env()
    env.domains['py'].objects['pkg.thing'] = SimpleNamespace(
        docname='api', node_id='pkg.thing', aliased=False
    )
    setattr(
        env,
        autolink._ENV_ATTR,
        {'index': [autolink._Candidate('thing', ('pkg.thing',), counts_as_use=False)]},
    )
    setattr(env, autolink._INDEX_DOCS_ATTR, {'api'})
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)

    assert 'href="index"' not in (tmp_path / 'api.html').read_text()
    # The bare mention in `index.html` itself is still linked -- only the "Used In"
    # list membership (and the frequency count) are gated on real usage.
    assert 'href="api#pkg.thing"' in (tmp_path / 'index.html').read_text()


def test_embed_links_enum_member_access_appears_in_used_in(tmp_path):
    # A page referencing a target only via enum-member access (`Color.RED`) still counts.
    (tmp_path / 'index.html').write_text('<pre><span class="n">Color</span></pre>')
    target = f'{_Color.__module__}.{_Color.__qualname__}'
    api_html = (
        '<html><body>'
        f'<section class="sphinx-autocodelink-backrefs" id="autocodelink-{target}">'
        '<h2>Used In</h2>'
        '<div class="sphinx-autocodelink-index" data-opts="'
        f'{{&quot;name&quot;: &quot;{target}&quot;, &quot;hide_empty&quot;: false, '
        '&quot;titles&quot;: false, &quot;group&quot;: &quot;auto&quot;}"></div>'
        '</section>'
        '</body></html>'
    )
    (tmp_path / 'api.html').write_text(api_html)

    env = _fake_env()
    env.domains['py'].objects[target] = SimpleNamespace(
        docname='api', node_id=target, aliased=False
    )
    setattr(
        env,
        autolink._ENV_ATTR,
        {'index': autolink._records_for('Color.RED\n', {'Color': _Color})},
    )
    setattr(env, autolink._INDEX_DOCS_ATTR, {'api'})
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)

    assert 'href="index"' in (tmp_path / 'api.html').read_text()


def test_embed_links_name_dedup(tmp_path):
    html = '<pre><span class="n">mesh</span></pre>'
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    env.domains['py'].objects['pkg.mesh'] = SimpleNamespace(
        docname='api', node_id='pkg.mesh', aliased=False
    )
    setattr(
        env,
        autolink._ENV_ATTR,
        {
            'index': [
                autolink._Candidate('mesh', ('pkg.mesh',)),
                autolink._Candidate('mesh', ('pkg.mesh',)),
            ]
        },
    )
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)
    result = out_file.read_text()

    assert result.count('href="api#pkg.mesh"') == 1


def test_embed_links_name_already_linked(tmp_path):
    html = (
        '<pre><a class="other-extension-a" href="somewhere"><span class="n">mesh</span></a></pre>'
    )
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    env.domains['py'].objects['pkg.mesh'] = SimpleNamespace(
        docname='api', node_id='pkg.mesh', aliased=False
    )
    setattr(env, autolink._ENV_ATTR, {'index': [autolink._Candidate('mesh', ('pkg.mesh',))]})
    app = _fake_app(env, tmp_path)
    autolink._embed_links(app, None)
    assert out_file.read_text() == html


def test_render_full_index_empty():
    result = autolink._render_full_index(
        {}, docname='index', app=None, local={}, external={}, categories={}
    )
    assert result == ''


def test_docname_title_present():
    app = SimpleNamespace(
        env=SimpleNamespace(titles={'index': SimpleNamespace(astext=lambda: 'Index')})
    )
    assert autolink._docname_title(app, 'index') == 'Index'


def test_docname_title_missing_falls_back_to_docname():
    app = SimpleNamespace(env=SimpleNamespace(titles={}))
    assert autolink._docname_title(app, 'index') == 'index'


def test_render_index_entry_excludes_self_reference(tmp_path):
    # a docstring's own Examples section calling the very thing it documents
    # is not a genuine cross-reference to itself.
    app = _fake_app(_fake_env(), tmp_path)
    backrefs = {'pkg.thing': {'pkg.thing', 'other'}}
    result = autolink._render_index_entry(
        'pkg.thing',
        backrefs,
        docname='pkg.thing',
        app=app,
        categories={},
        show_titles=False,
        group_mode='auto',
    )
    assert 'pkg.thing' not in result
    assert 'href="other"' in result


def test_render_index_entry_self_reference_only_is_empty(tmp_path):
    app = _fake_app(_fake_env(), tmp_path)
    backrefs = {'pkg.thing': {'pkg.thing'}}
    result = autolink._render_index_entry(
        'pkg.thing',
        backrefs,
        docname='pkg.thing',
        app=app,
        categories={},
        show_titles=False,
        group_mode='auto',
    )
    assert result == ''


def test_render_index_entry_slices_usage_counts_by_target(tmp_path):
    app = _fake_app(_fake_env(), tmp_path)
    app.config.autocodelink_sort = 'frequency'
    backrefs = {'pkg.thing': {'a', 'b'}, 'pkg.other': {'a', 'b'}}
    # pkg.thing and pkg.other disagree on which of a/b is more used -- proves the count
    # actually gets sliced per target, not shared/mixed up across them.
    usage_counts = {'pkg.thing': {'a': 1, 'b': 9}, 'pkg.other': {'a': 9, 'b': 1}}
    result = autolink._render_index_entry(
        'pkg.thing',
        backrefs,
        docname='index',
        app=app,
        categories={},
        show_titles=False,
        group_mode='auto',
        usage_counts=usage_counts,
    )
    assert re.findall(r'href="([ab])"', result) == ['b', 'a']


def test_render_full_index_excludes_self_reference(tmp_path):
    app = _fake_app(_fake_env(), tmp_path)
    backrefs = {'pkg.thing': {'pkg.thing', 'other'}}
    result = autolink._render_full_index(
        backrefs,
        docname='pkg.thing',
        app=app,
        local={},
        external={},
        categories={},
        show_titles=False,
    )
    # 'pkg.thing' appears once, as the <dt> heading -- not again as a referencing-page link.
    assert result.count('pkg.thing') == 1
    assert 'href="other"' in result


def test_inject_backref_index_skips_module():
    lines = []
    autolink._inject_backref_index(None, 'module', 'pkg', None, {}, lines)
    assert lines == []


def test_inject_backref_index_appends_directive():
    lines = ['existing docstring line']
    autolink._inject_backref_index(None, 'function', 'pkg.thing', None, {}, lines)
    assert lines[-3:] == [
        '.. autocodelink-index:: pkg.thing',
        '   :label: Used In',
        '   :hide-empty:',
    ]


def test_register_autodoc_hook_disabled_by_default():
    connected = {}

    class FakeApp:
        config = SimpleNamespace(autocodelink_autodoc_backrefs=False)
        events = SimpleNamespace(events={'autodoc-process-docstring': ''})

        def connect(self, event, handler):
            connected[event] = handler

    autolink._register_autodoc_hook(FakeApp())
    assert connected == {}


def test_register_autodoc_hook_skips_when_autodoc_unavailable():
    connected = {}

    class FakeApp:
        config = SimpleNamespace(autocodelink_autodoc_backrefs=True)
        events = SimpleNamespace(events={})

        def connect(self, event, handler):
            connected[event] = handler

    autolink._register_autodoc_hook(FakeApp())
    assert connected == {}


def test_register_autodoc_hook_connects_when_enabled_and_available():
    connected = {}

    class FakeApp:
        config = SimpleNamespace(autocodelink_autodoc_backrefs=True)
        events = SimpleNamespace(events={'autodoc-process-docstring': ''})

        def connect(self, event, handler):
            connected[event] = handler

    autolink._register_autodoc_hook(FakeApp())
    assert connected == {'autodoc-process-docstring': autolink._inject_backref_index}


class _Owner:
    """A class whose members exercise every shape of observed callable."""

    def method(self):
        """Return nothing."""

    @classmethod
    def class_method(cls):
        """Return nothing."""


def _plain_function():
    """Return nothing."""


def test_candidates_for_callable():
    assert autolink._candidates_for_callable(_Owner)[0].endswith('_Owner')
    assert autolink._candidates_for_callable(_Owner().method)[0].endswith('_Owner.method')
    assert autolink._candidates_for_callable(_Owner.class_method)[0].endswith('_Owner.class_method')
    assert autolink._candidates_for_callable(_plain_function)[0].endswith('_plain_function')


def test_candidates_for_callable_skips_builtins_and_the_unnameable():
    assert autolink._candidates_for_callable(len) == []
    assert autolink._candidates_for_callable('not callable at all') == []
    assert autolink._candidates_for_callable(functools.partial(_plain_function)) == []


class _NamelessCallable:
    """A callable with no ``__name__``, for the method that can't be named."""

    def __call__(self):
        """Return nothing."""


def test_candidates_for_a_method_with_no_name():
    assert autolink._candidates_for_callable(types.MethodType(_NamelessCallable(), _Owner())) == []


def test_expr_pattern_source_matches_only_the_trailing_attribute():
    pattern = autolink._expr_pattern_source("reg['a'].render")
    html = (
        '<span class="n">reg</span><span class="p">[</span><span class="s1">&#39;a&#39;</span>'
        '<span class="p">]</span><span class="o">.</span><span class="n">render</span>'
    )
    match = re.search(pattern, html.replace('&#39;', "'"))
    assert match.group() == '<span class="o">.</span><span class="n">render</span>'


def test_expr_pattern_source_of_something_with_no_trailing_attribute():
    assert autolink._expr_pattern_source('reg') is None
    assert autolink._expr_pattern_source('.render') is None


def test_expr_pattern_source_of_a_multi_line_expression():
    assert autolink._expr_pattern_source('reg[\n    "a"\n].render') is None


def test_highlight_fragment_that_will_not_lex(monkeypatch):
    import pygments

    def boom(*args, **kwargs):
        raise ValueError

    monkeypatch.setattr(pygments, 'highlight', boom)
    assert autolink._highlight_fragment('anything') is None


def test_expr_candidate_round_trips_through_json():
    record = autolink._ExprCandidate("reg['a'].render", ('pkg.Widget.render',))
    assert autolink._from_jsonable(autolink._to_jsonable(record)) == record


@needs_monitoring
def test_wire_gallery_tracing_adds_the_reset_hook():
    from sphinx_autocodelink.gallery import RESET_AUTOCODELINK
    from sphinx_autocodelink.gallery import AutoCodeLinkScraper

    connected = []
    app = SimpleNamespace(connect=lambda event, handler: connected.append(event))
    conf = {'image_scrapers': (AutoCodeLinkScraper(),)}
    config = SimpleNamespace(sphinx_gallery_conf=conf)
    autolink._wire_gallery_tracing(app, config)
    assert conf['reset_modules'] == ('matplotlib', 'seaborn', RESET_AUTOCODELINK)
    assert connected == ['build-finished']

    # already there: left exactly as it is, not appended twice
    autolink._wire_gallery_tracing(app, config)
    assert conf['reset_modules'].count(RESET_AUTOCODELINK) == 1


def test_wire_gallery_tracing_without_a_gallery():
    autolink._wire_gallery_tracing(None, SimpleNamespace())


def test_stop_gallery_tracing_leaves_no_tracer_running():
    autolink._stop_gallery_tracing(None, None)
    from sphinx_autocodelink import gallery as sg_gallery

    assert sg_gallery._TRACER is None or not sg_gallery._TRACER.active


@needs_monitoring
def test_wire_gallery_tracing_warns_when_nothing_runs_before_an_example(monkeypatch):
    from sphinx_autocodelink.gallery import AutoCodeLinkScraper

    warnings = []
    monkeypatch.setattr(autolink._logger, 'warning', lambda *args: warnings.append(args))
    conf = {
        'image_scrapers': (AutoCodeLinkScraper(),),
        'reset_modules': (),
        'reset_modules_order': 'after',
    }
    app = SimpleNamespace(connect=lambda event, handler: None)
    autolink._wire_gallery_tracing(app, SimpleNamespace(sphinx_gallery_conf=conf))
    assert len(warnings) == 1
