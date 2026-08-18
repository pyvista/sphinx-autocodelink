"""Unit tests for sphinx_autocodelink internals, without a full Sphinx build."""

from __future__ import annotations

import ast
import re
import sys
import types
from types import SimpleNamespace

import sphinx_autocodelink as autolink


def test_accessed_names_syntax_error():
    assert autolink._accessed_names('def bad(:\n') == set()


def test_accessed_names_call_chain_not_rooted_in_name():
    # `.plot` on a call result has nothing to look up, but the inner `pv.Sphere` does.
    assert autolink._accessed_names('pv.Sphere().plot()') == {'pv.Sphere'}


def test_accessed_names_bare_name():
    assert autolink._accessed_names('x') == {'x'}


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
    assert autolink._call_chains('pv.Sphere().plot()') == {('pv.Sphere', ('plot',))}


def test_call_chains_bound_method():
    assert autolink._call_chains('mesh.copy().plot()') == {('mesh.copy', ('plot',))}


def test_call_chains_multi_attribute_trailing():
    assert autolink._call_chains('pv.Sphere().points.size') == {('pv.Sphere', ('points', 'size'))}


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


def test_call_return_type_rejects_complex_annotation():
    def make_widget_or_string() -> Widget | str:  # noqa: F821
        return ''

    assert autolink._call_return_type(make_widget_or_string, {}) is None


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
            'make_thing', ('method',), ('test_autolink._RecordNamespaceReturnType.method',)
        )
    ]


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
            'make_thing', ('method',), ('test_autolink._RecordNamespaceReturnType.method',)
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

        def add_config_value(self, name, default, rebuild):
            config_values[name] = (default, rebuild)

        def add_directive(self, name, directive):
            directives[name] = directive

    result = autolink.setup(FakeApp())
    from sphinx_autocodelink.gallery import _install_gallery_tracing

    assert connected == {
        'builder-inited': [autolink._clear_disk_records, autolink._register_autodoc_hook],
        'env-merge-info': [autolink._merge_records],
        'env-purge-doc': [autolink._purge_doc],
        'build-finished': [autolink._embed_links],
        'config-inited': [_install_gallery_tracing],
    }
    # runs after other build-finished handlers at Sphinx's default priority (500) -- see
    # setup()'s docstring for why (Sphinx-Gallery's reference_url embedding, notably).
    assert priorities[('build-finished', autolink._embed_links)] == 900
    # after Sphinx-Gallery's own fill_gallery_conf_defaults (priority 10).
    assert priorities[('config-inited', _install_gallery_tracing)] == 20
    assert config_values == {
        'autocodelink_records_dir': (autolink.DEFAULT_RECORDS_DIR, 'html'),
        'autocodelink_autodoc_backrefs': (False, 'html'),
        'autocodelink_category_labels': ({}, 'html'),
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


def test_resolve_link_external():
    resolved = autolink._resolve_link(
        ('external.thing',),
        docname='index',
        app=None,
        local={},
        external={'external.thing': 'https://example.invalid/thing.html'},
    )
    assert resolved == ('external.thing', 'https://example.invalid/thing.html')


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
        docname='api', node_id='pyvista.PolyData.plot'
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
    env.domains['py'].objects['test_autolink._RecordNamespaceReturnType'] = SimpleNamespace(
        docname='api', node_id='test_autolink._RecordNamespaceReturnType'
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

    assert 'href="api#test_autolink._RecordNamespaceReturnType"' in result


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
        docname='api', node_id='pyvista.PolyData.plot'
    )
    env.domains['py'].objects['pyvista.Sphere'] = SimpleNamespace(
        docname='api2', node_id='pyvista.Sphere'
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
        docname='api', node_id='pyvista.PolyData.plot'
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
        docname='api', node_id='pyvista.PolyData.plot'
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


def test_embed_links_name_dedup(tmp_path):
    html = '<pre><span class="n">mesh</span></pre>'
    out_file = tmp_path / 'index.html'
    out_file.write_text(html)

    env = _fake_env()
    env.domains['py'].objects['pkg.mesh'] = SimpleNamespace(docname='api', node_id='pkg.mesh')
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
    env.domains['py'].objects['pkg.mesh'] = SimpleNamespace(docname='api', node_id='pkg.mesh')
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
        '   :label: Used in',
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
