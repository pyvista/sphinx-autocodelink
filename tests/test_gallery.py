"""Unit tests for sphinx_autocodelink.gallery, without a full Sphinx-Gallery build."""

from __future__ import annotations

from types import SimpleNamespace

from sphinx_autocodelink import gallery as sg_gallery


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
