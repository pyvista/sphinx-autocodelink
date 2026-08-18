"""End-to-end builds of the tinypages fixture: the extension without pyvista."""

from __future__ import annotations

from pathlib import Path
import re
import shutil

from sphinx.application import Sphinx

TINYPAGES = Path(__file__).parent / 'tinypages'


def _build(tmp_path, *, parallel=1):
    """Build a fresh copy of the tinypages fixture; return the outdir and index.html text."""
    srcdir = tmp_path / 'src'
    shutil.copytree(TINYPAGES, srcdir)
    outdir = tmp_path / 'out'
    doctreedir = tmp_path / 'doctrees'
    app = Sphinx(
        srcdir=str(srcdir),
        confdir=str(srcdir),
        outdir=str(outdir),
        doctreedir=str(doctreedir),
        buildername='html',
        parallel=parallel,
    )
    app.build()
    return outdir, (outdir / 'index.html').read_text()


def test_standalone_directive_links_without_pyvista(tmp_path):
    _, result = _build(tmp_path)
    assert 'sphinx-autocodelink-a' in result
    assert 'href="api.html#pkg.thing"' in result


def test_sphinx_gallery_scraper_links_survives_joblib_workers(tmp_path):
    # `parallel=2` forces Sphinx-Gallery to hand examples to separate joblib
    # worker processes -- the whole reason records go to disk instead of `env`.
    outdir, _ = _build(tmp_path, parallel=2)
    for name in ('plot_thing', 'plot_other'):
        example_html = (outdir / 'auto_examples' / f'{name}.html').read_text()
        assert 'href="../api.html#pkg.thing"' in example_html


def test_autocodelink_index(tmp_path):
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    dl = re.search(r'<dl class="sphinx-autocodelink-index">.*?</dl>', refs, re.DOTALL).group()

    # full index: pkg.thing heading links to its docs, and lists every referencing page.
    assert 'href="api.html#pkg.thing"' in dl
    for page in ('index.html', 'auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in dl

    # default grouping (2 categories apply to pkg.thing: 'Other' for the directive-recorded
    # index page, 'Sphinx Gallery' for the scraper-recorded examples).
    groups = re.findall(
        r'<div class="sphinx-autocodelink-index-group">'
        r'<p class="sphinx-autocodelink-index-group-label">([^<]*)</p>'
        r'(<ul class="sphinx-autocodelink-index">.*?</ul>)</div>',
        refs,
        re.DOTALL,
    )
    by_label = dict(groups)
    assert set(by_label) == {'Other', 'Sphinx Gallery'}
    assert 'href="index.html"' in by_label['Other']
    for page in ('auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in by_label['Sphinx Gallery']

    # :group: never forces one flat list despite the same 2 categories applying: the ungrouped
    # <ul> that follows the grouped <div>s, before the next paragraph.
    after_groups = refs.split('</div><p>', 1)[1]
    forced_flat = re.search(r'<ul class="sphinx-autocodelink-index">.*?</ul>', after_groups)
    assert forced_flat is not None
    assert 'sphinx-autocodelink-index-group' not in forced_flat.group()
    for page in ('index.html', 'auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in forced_flat.group()

    # filtered index for a name with no references.
    assert 'No references found.' in refs


def test_autodoc_backrefs(tmp_path):
    outdir, _ = _build(tmp_path)
    api = (outdir / 'api.html').read_text()

    # pkg.thing is referenced elsewhere: a real, hoistable section, not just a raw block.
    section = re.search(
        r'<section class="sphinx-autocodelink-backrefs"[^>]*>.*?</section>', api, re.DOTALL
    )
    assert section is not None
    assert '<h2>Used in' in section.group()
    for page in ('index.html', 'auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in section.group()

    # pkg.unused has no references: nothing appended at all, not even "No references found."
    unused_dd = re.search(r'id="pkg\.unused">.*?</dd>', api, re.DOTALL).group()
    assert 'sphinx-autocodelink-backrefs' not in unused_dd
    assert 'No references found' not in unused_dd
