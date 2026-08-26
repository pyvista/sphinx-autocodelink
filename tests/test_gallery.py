"""Unit tests for sphinx_autocodelink.gallery, without a full Sphinx-Gallery build."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import sphinx_autocodelink as autolink
from sphinx_autocodelink import gallery as sg_gallery

needs_monitoring = pytest.mark.skipif(
    not sg_gallery.monitoring_available(), reason='sys.monitoring added in Python 3.12'
)


def test_scraper_records(tmp_path):
    scraper = sg_gallery.AutoCodeLinkScraper()
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


@pytest.fixture(autouse=True)
def _stop_tracing():
    """Leave no tracer running, whatever a test starts."""
    yield
    sg_gallery.reset_autocodelink({}, None, 'before')


def test_scraper_writes_traced_records_too(tmp_path):
    scraper = sg_gallery.AutoCodeLinkScraper()
    record = autolink._ExprCandidate("reg['a'].render", ('pkg.Widget.render',))
    sg_gallery._RECORDER.records = [record]
    block = SimpleNamespace(content='')
    block_vars = {
        'target_file': str(tmp_path / 'auto_examples' / 'plot_traced.py'),
        'example_globals': {},
    }

    scraper(block, block_vars, {'src_dir': str(tmp_path)})

    written = tmp_path / sg_gallery.DEFAULT_RECORDS_DIR / 'auto_examples' / 'plot_traced.json'
    assert json.loads(written.read_text())['records'] == [
        {'expr': "reg['a'].render", 'candidates': ['pkg.Widget.render']}
    ]
    # drained, so the next block doesn't write them a second time
    assert sg_gallery._RECORDER.records == []


@needs_monitoring
def test_reset_starts_tracing_before_an_example_and_stops_after():
    sg_gallery.reset_autocodelink({}, 'plot_thing.py', 'before')
    assert sg_gallery._TRACER.active

    sg_gallery.reset_autocodelink({}, 'plot_thing.py', 'after')
    assert not sg_gallery._TRACER.active

    # entering a directory of examples reports no example at all
    sg_gallery.reset_autocodelink({}, 'plot_thing.py', 'before')
    sg_gallery.reset_autocodelink({}, None, 'before')
    assert not sg_gallery._TRACER.active


def test_report_failure_warns_once(monkeypatch):
    warnings = []
    monkeypatch.setattr(sg_gallery._logger, 'warning', lambda *args: warnings.append(args))
    monkeypatch.setattr(sg_gallery, '_REPORTED_FAILURE', False)
    sg_gallery._report_failure(RuntimeError('boom'))
    sg_gallery._report_failure(RuntimeError('boom again'))
    assert len(warnings) == 1


@needs_monitoring
def test_wants_tracing():
    scraper = sg_gallery.AutoCodeLinkScraper()
    assert sg_gallery._wants_tracing({'image_scrapers': (scraper,)})
    # a lone scraper, not in a sequence
    assert sg_gallery._wants_tracing({'image_scrapers': scraper})
    assert not sg_gallery._wants_tracing({'image_scrapers': ('matplotlib',)})
    assert not sg_gallery._wants_tracing(
        {'image_scrapers': (sg_gallery.AutoCodeLinkScraper(trace=False),)}
    )
    assert not sg_gallery._wants_tracing({})
    assert not sg_gallery._wants_tracing(None)


def test_wants_tracing_needs_monitoring(monkeypatch):
    monkeypatch.setattr(sg_gallery, 'monitoring_available', lambda: False)
    conf = {'image_scrapers': (sg_gallery.AutoCodeLinkScraper(),)}
    assert not sg_gallery._wants_tracing(conf)
