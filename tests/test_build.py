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
    uls = re.findall(r'<ul class="sphinx-autocodelink-index">.*?</ul>', refs, re.DOTALL)

    # full index: pkg.thing heading links to its docs, and lists every referencing page.
    assert 'href="api.html#pkg.thing"' in dl
    for page in ('index.html', 'auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in dl

    # filtered index (`pkg.thing`) repeats the same referencing pages.
    assert len(uls) == 1
    for page in ('index.html', 'auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in uls[0]

    # filtered index for a name with no references.
    assert 'No references found.' in refs
