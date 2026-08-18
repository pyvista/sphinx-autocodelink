"""End-to-end builds of the tinypages fixture: the extension without pyvista."""

from __future__ import annotations

from pathlib import Path
import re
import shutil

from sphinx.application import Sphinx

TINYPAGES = Path(__file__).parent / 'tinypages'


def _build(tmp_path, *, parallel=1, confoverrides=None):
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
        confoverrides=confoverrides,
    )
    app.build()
    return outdir, (outdir / 'index.html').read_text()


def test_standalone_directive_links_without_pyvista(tmp_path):
    _, result = _build(tmp_path)
    assert 'sphinx-autocodelink-a' in result
    assert 'href="api.html#pkg.thing"' in result


def test_directive_resolves_identifiers_local_to_a_helper_function(tmp_path):
    # `local_ref` in index.rst's second `.. autocodelink::` block only ever exists inside
    # its own helper function's local scope -- a plain exec() couldn't resolve it.
    _, result = _build(tmp_path)
    assert (
        '<a class="sphinx-autocodelink-a" href="api.html#pkg.thing">'
        '<span class="n">local_ref</span><span class="o">.</span>'
        '<span class="n">thing</span></a>' in result
    )


def test_sphinx_gallery_scraper_links_survives_joblib_workers(tmp_path):
    # `parallel=2` forces Sphinx-Gallery to hand examples to separate joblib
    # worker processes -- the whole reason records go to disk instead of `env`.
    outdir, _ = _build(tmp_path, parallel=2)
    for name in ('plot_thing', 'plot_other'):
        example_html = (outdir / 'auto_examples' / f'{name}.html').read_text()
        assert 'href="../api.html#pkg.thing"' in example_html


def _block_after(html: str, marker: str) -> str:
    """Return the HTML between one paragraph's ``marker`` text and the next ``<p>`` (or end)."""
    start = html.index(marker) + len(marker)
    rest = html[start:]
    end = rest.index('<p>') if '<p>' in rest else rest.index('</section>')
    return rest[:end]


def test_autocodelink_index(tmp_path):
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    dl = re.search(r'<dl class="sphinx-autocodelink-index">.*?</dl>', refs, re.DOTALL).group()

    # full index: pkg.thing heading links to its docs, and lists every referencing page.
    assert 'href="api.html#pkg.thing"' in dl
    for page in ('index.html', 'auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in dl

    # default grouping (2 categories apply to pkg.thing: 'Documentation' for the
    # directive-recorded index page, 'Sphinx Gallery' for the scraper-recorded examples)
    # -- and titles, not docnames, by default.
    grouped = _block_after(refs, 'since 2 categories apply):')
    groups = dict(
        re.findall(
            r'<div class="sphinx-autocodelink-index-group">'
            r'<p class="sphinx-autocodelink-index-group-label">([^<]*)</p>'
            r'(<ul class="sphinx-autocodelink-index">.*?</ul>)</div>',
            grouped,
            re.DOTALL,
        )
    )
    assert set(groups) == {'Documentation', 'Sphinx Gallery'}
    assert '<a href="index.html">Index</a>' in groups['Documentation']
    assert (
        '<a href="auto_examples/plot_thing.html">Plotting a thing</a>' in groups['Sphinx Gallery']
    )
    assert (
        '<a href="auto_examples/plot_other.html">Plotting another thing</a>'
        in groups['Sphinx Gallery']
    )

    # :group: never forces one flat list despite the same 2 categories applying.
    forced_flat = _block_after(refs, 'forced flat despite 2 categories applying:')
    assert 'sphinx-autocodelink-index-group' not in forced_flat
    assert '<a href="index.html">Index</a>' in forced_flat
    assert '<a href="auto_examples/plot_thing.html">Plotting a thing</a>' in forced_flat

    # :no-titles: shows docnames instead.
    no_titles = _block_after(refs, 'docnames instead of titles:')
    assert '<a href="index.html">index</a>' in no_titles
    assert '<a href="auto_examples/plot_thing.html">auto_examples/plot_thing</a>' in no_titles

    # filtered index for a name with no references.
    assert 'No references found.' in refs


def test_autocodelink_category_labels_renames_group_headings(tmp_path):
    # renames the *display* label only -- grouping itself still goes by the real,
    # unrenamed category, so this must still land the exact same 2 groups as the
    # default-labels case above.
    outdir, _ = _build(
        tmp_path,
        confoverrides={
            'autocodelink_category_labels': {
                'Sphinx Gallery': 'Gallery Examples',
                'Documentation': 'API Docs',
            }
        },
    )
    refs = (outdir / 'refs.html').read_text()
    grouped = _block_after(refs, 'since 2 categories apply):')
    labels = re.findall(r'<p class="sphinx-autocodelink-index-group-label">([^<]*)</p>', grouped)
    assert set(labels) == {'Gallery Examples', 'API Docs'}


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
    assert '<a href="index.html">Index</a>' in section.group()  # titles, not docnames

    # pkg.unused has no references: nothing appended at all, not even "No references found."
    unused_dd = re.search(r'id="pkg\.unused">.*?</dd>', api, re.DOTALL).group()
    assert 'sphinx-autocodelink-backrefs' not in unused_dd
    assert 'No references found' not in unused_dd
