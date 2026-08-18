"""Unit tests for sphinx_autocodelink.gallery, without a full Sphinx-Gallery build."""

from __future__ import annotations

from types import SimpleNamespace

from sphinx_autocodelink import gallery as sg_gallery


def test_scraper_records_without_tracing(tmp_path):
    scraper = sg_gallery.AutoCodeLinkScraper(trace_locals=False)
    block = SimpleNamespace(content='x')
    block_vars = {
        'target_file': str(tmp_path / 'auto_examples' / 'plot_thing.py'),
        'example_globals': {'x': 1},
    }
    gallery_conf = {'src_dir': str(tmp_path)}

    result = scraper(block, block_vars, gallery_conf)

    assert result == ''
    records_file = tmp_path / sg_gallery.DEFAULT_RECORDS_DIR / 'auto_examples' / 'plot_thing.json'
    assert records_file.exists()


def test_scraper_merges_traced_locals(tmp_path):
    sg_gallery._LAST_TRACED_LOCALS.clear()
    sg_gallery._LAST_TRACED_LOCALS['local_only'] = object()

    scraper = sg_gallery.AutoCodeLinkScraper()
    block = SimpleNamespace(content='local_only')
    block_vars = {
        'target_file': str(tmp_path / 'auto_examples' / 'plot_thing.py'),
        'example_globals': {},
    }
    gallery_conf = {'src_dir': str(tmp_path)}

    scraper(block, block_vars, gallery_conf)

    # consumed and cleared, so the next block doesn't see stale data.
    assert sg_gallery._LAST_TRACED_LOCALS == {}


def _exec_callable(code: str, filename: str = '<test-example>'):
    """Build a callable that ``exec()``s ``code`` -- Sphinx-Gallery's ``show_memory`` contract."""
    compiled = compile(code, filename, 'exec')
    namespace: dict = {}

    def _run():
        exec(compiled, namespace)  # noqa: S102 -- test-controlled code

    return _run


def test_trace_call_captures_locals_and_returns_result():
    code = 'def helper():\n    local = 42\n    return local\nresult = helper()\n'

    captured, result = sg_gallery._trace_call(_exec_callable(code))

    assert captured['local'] == 42
    assert result is None  # func() itself returns nothing, matching _exec_once


def test_call_memory_with_tracing_matches_sphinx_gallery_contract():
    sg_gallery._LAST_TRACED_LOCALS.clear()
    code = "def helper():\n    local = 'value'\n    return local\nhelper()\n"

    mem_max, result = sg_gallery._call_memory_with_tracing(_exec_callable(code))

    assert mem_max == 0.0
    assert result is None
    assert sg_gallery._LAST_TRACED_LOCALS['local'] == 'value'


def test_trace_call_memory_composes_with_existing_show_memory():
    sg_gallery._LAST_TRACED_LOCALS.clear()
    calls = []

    def my_show_memory(func):
        calls.append('called')
        return 1.5, func()

    wrapped = sg_gallery.trace_call_memory(my_show_memory)
    code = "def helper():\n    local = 'traced'\n    return local\nhelper()\n"

    mem_max, result = wrapped(_exec_callable(code))

    assert calls == ['called']
    assert mem_max == 1.5
    assert result is None
    assert sg_gallery._LAST_TRACED_LOCALS['local'] == 'traced'


def test_install_gallery_tracing_noop_without_sphinx_gallery():
    config = SimpleNamespace()  # no sphinx_gallery_conf attribute at all
    assert sg_gallery._install_gallery_tracing(None, config) is None


def test_install_gallery_tracing_noop_without_scraper():
    config = SimpleNamespace(sphinx_gallery_conf={'image_scrapers': ('matplotlib',)})
    sg_gallery._install_gallery_tracing(None, config)
    assert 'show_memory' not in config.sphinx_gallery_conf


def test_install_gallery_tracing_noop_when_trace_locals_disabled():
    scraper = sg_gallery.AutoCodeLinkScraper(trace_locals=False)
    config = SimpleNamespace(sphinx_gallery_conf={'image_scrapers': (scraper,)})
    sg_gallery._install_gallery_tracing(None, config)
    assert 'show_memory' not in config.sphinx_gallery_conf


def test_install_gallery_tracing_installs_when_free():
    scraper = sg_gallery.AutoCodeLinkScraper()
    config = SimpleNamespace(sphinx_gallery_conf={'image_scrapers': (scraper,)})
    sg_gallery._install_gallery_tracing(None, config)
    assert config.sphinx_gallery_conf['show_memory'] is sg_gallery._call_memory_with_tracing


def test_install_gallery_tracing_backs_off_when_show_memory_already_set():
    scraper = sg_gallery.AutoCodeLinkScraper()

    def existing_show_memory(func):
        return 0.0, func()

    config = SimpleNamespace(
        sphinx_gallery_conf={'image_scrapers': (scraper,), 'show_memory': existing_show_memory}
    )
    sg_gallery._install_gallery_tracing(None, config)
    assert config.sphinx_gallery_conf['show_memory'] is existing_show_memory
