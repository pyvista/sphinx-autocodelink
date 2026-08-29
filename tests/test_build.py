"""End-to-end builds of the tinypages fixture: the extension without pyvista."""

from __future__ import annotations

from pathlib import Path
import re
import shutil

import pytest
from sphinx.application import Sphinx

from sphinx_autocodelink._tracing import monitoring_available

TINYPAGES = Path(__file__).parent / 'tinypages'

#: Matches sphinx_autocodelink._STD_REF_OPEN.
STD_REF = '<span class="std std-ref" style="font-weight: bold;">'

needs_monitoring = pytest.mark.skipif(
    not monitoring_available(), reason='sys.monitoring added in Python 3.12'
)


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


def test_decorator_usage_links(tmp_path):
    _, result = _build(tmp_path)
    # the leading ``@`` renders inside the name's own span, and the link wraps it
    assert '<a class="sphinx-autocodelink-a" href="api.html#pkg.tag">' in result


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


def _block_between(html: str, start_marker: str, end_marker: str) -> str:
    """Return the HTML strictly between two marker strings.

    Unlike ``_block_after``, doesn't assume the block itself contains no ``<p>`` -- a
    gallery card's own thumbnail markup does (see ``_gallery_cards._thumbnail_html``).
    """
    start = html.index(start_marker) + len(start_marker)
    return html[start : html.index(end_marker, start)]


def test_autocodelink_index(tmp_path):
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    dl = re.search(r'<dl class="sphinx-autocodelink-index">.*?</dl>', refs, re.DOTALL).group()

    # full index: pkg.thing heading links to its docs, and lists every referencing page.
    assert 'href="api.html#pkg.thing"' in dl
    for page in ('index.html', 'auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in dl

    # Default grouping and titles: all 3 categories apply to pkg.thing.
    grouped = _block_after(refs, 'since 3 categories apply):')
    groups = dict(
        re.findall(
            r'<div class="sphinx-autocodelink-index-group">'
            r'<p class="sphinx-autocodelink-index-group-label"><strong>([^<]*)</strong></p>'
            r'(<ul class="sphinx-autocodelink-index">.*?</ul>)</div>',
            grouped,
            re.DOTALL,
        )
    )
    assert set(groups) == {'Documentation', 'Sphinx Gallery', 'Docstring Examples'}
    # a "Sphinx Gallery" entry is a real, structured page with a real anchor -- exactly what a
    # real :ref: points at -- and renders the same way (bold, the ordinary link color).
    assert (
        f'<a href="auto_examples/plot_thing.html">{STD_REF}Plotting a thing</span></a>'
        in groups['Sphinx Gallery']
    )
    assert (
        f'<a href="auto_examples/plot_other.html">{STD_REF}Plotting another thing</span></a>'
        in groups['Sphinx Gallery']
    )
    # a "Docstring Examples" entry -- itself another documented object's own page -- renders
    # like a real cross-reference (bold, distinct color), not a plain page link.
    assert (
        '<a href="api.html"><code class="xref py py-obj docutils literal notranslate">API'
        '</code></a>' in groups['Docstring Examples']
    )
    # "Documentation" is the generic uncategorized bucket -- no similarly specific real
    # target to point at, so it's a plain link like any other page reference.
    assert '<a href="index.html">Index</a>' in groups['Documentation']

    # :no-group: forces one flat list despite the same 3 categories applying.
    forced_flat = _block_after(refs, 'forced flat despite 3 categories applying:')
    assert 'sphinx-autocodelink-index-group' not in forced_flat
    assert '<a href="index.html">Index</a>' in forced_flat
    assert (
        f'<a href="auto_examples/plot_thing.html">{STD_REF}Plotting a thing</span></a>'
        in forced_flat
    )

    # :no-titles: shows docnames instead.
    no_titles = _block_after(refs, 'docnames instead of titles:')
    assert '<a href="index.html">index</a>' in no_titles
    assert (
        f'<a href="auto_examples/plot_thing.html">{STD_REF}auto_examples/plot_thing</span></a>'
        in no_titles
    )

    # filtered index for a name with no references.
    assert 'No references found.' in refs


def test_sort_alphabetical_by_default(tmp_path):
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    forced_flat = _block_after(refs, 'forced flat despite 3 categories applying:')
    order = re.findall(r'<li><a href="([^"]*)"', forced_flat)
    # 'index' references pkg.thing twice (see test_sort_frequency below) but still sorts
    # after 'api' alphabetically by title ('API' < 'Index') when frequency isn't in play.
    assert order == [
        'api.html',
        'index.html',
        'auto_examples/plot_thing.html',
        'auto_examples/plot_other.html',
    ]


def test_sort_frequency_ranks_by_usage_and_accumulates_across_spellings(tmp_path):
    # index.rst spells pkg.thing two ways; both must count toward its one total.
    outdir, _ = _build(tmp_path, confoverrides={'autocodelink_sort': 'frequency'})
    refs = (outdir / 'refs.html').read_text()
    forced_flat = _block_after(refs, 'forced flat despite 3 categories applying:')
    order = re.findall(r'<li><a href="([^"]*)"', forced_flat)
    assert order[0] == 'index.html'
    # api.html and the two gallery examples are tied at one use each -- alphabetical by
    # title breaks the tie ('API' < 'Plotting a thing' < 'Plotting another thing').
    assert order[1:] == [
        'api.html',
        'auto_examples/plot_thing.html',
        'auto_examples/plot_other.html',
    ]


def test_sort_frequency_shows_usage_count_beside_each_entry(tmp_path):
    outdir, _ = _build(
        tmp_path,
        confoverrides={'autocodelink_sort': 'frequency', 'autocodelink_show_usage_count': True},
    )
    refs = (outdir / 'refs.html').read_text()
    forced_flat = _block_after(refs, 'forced flat despite 3 categories applying:')
    assert (
        '<a href="index.html">Index</a> '
        '<span class="sphinx-autocodelink-usage-count">(2 uses)</span>' in forced_flat
    )
    assert (
        f'<a href="auto_examples/plot_thing.html">{STD_REF}Plotting a thing</span></a> '
        '<span class="sphinx-autocodelink-usage-count">(1 use)</span>' in forced_flat
    )


def test_sort_alphabetical_shows_no_usage_count(tmp_path):
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    forced_flat = _block_after(refs, 'forced flat despite 3 categories applying:')
    assert 'sphinx-autocodelink-usage-count' not in forced_flat


def test_sort_frequency_without_show_usage_count_hides_it(tmp_path):
    outdir, _ = _build(tmp_path, confoverrides={'autocodelink_sort': 'frequency'})
    refs = (outdir / 'refs.html').read_text()
    forced_flat = _block_after(refs, 'forced flat despite 3 categories applying:')
    assert 'sphinx-autocodelink-usage-count' not in forced_flat


def test_show_usage_count_in_alphabetical_mode(tmp_path):
    outdir, _ = _build(tmp_path, confoverrides={'autocodelink_show_usage_count': True})
    refs = (outdir / 'refs.html').read_text()
    forced_flat = _block_after(refs, 'forced flat despite 3 categories applying:')
    # Sort stays alphabetical (api.html before index.html) but the count still shows.
    order = re.findall(r'<li><a href="([^"]*)"', forced_flat)
    assert order[:2] == ['api.html', 'index.html']
    assert (
        '<a href="index.html">Index</a> '
        '<span class="sphinx-autocodelink-usage-count">(2 uses)</span>' in forced_flat
    )


def test_gallery_cards_disabled_by_default(tmp_path):
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    forced_flat = _block_between(
        refs, 'forced flat despite 3 categories applying:', 'docnames instead of titles:'
    )
    assert 'sd-cards-carousel' not in forced_flat


def test_gallery_cards_renders_thumbnail_carousel(tmp_path):
    outdir, _ = _build(tmp_path, confoverrides={'autocodelink_gallery_cards': True})
    refs = (outdir / 'refs.html').read_text()
    forced_flat = _block_between(
        refs, 'forced flat despite 3 categories applying:', 'docnames instead of titles:'
    )

    assert 'sd-cards-carousel' in forced_flat
    assert forced_flat.count('<div class="sphx-glr-thumbcontainer"') == 2  # plot_thing, plot_other
    # The other 2 categories mixed into this same flat list still render as a plain <ul>,
    # since only "Sphinx Gallery" entries become cards -- their carousel isn't a <ul> at all.
    assert '<li><a href="index.html">Index</a></li>' in forced_flat
    assert '<span class="std std-ref"' not in forced_flat  # no lingering plain :ref: style

    # Each thumbnail points at a real, built image file -- not a guessed path.
    for name in ('plot_thing', 'plot_other'):
        match = re.search(rf'<img src="([^"]*sphx_glr_{name}_thumb\.png)"', forced_flat)
        assert match is not None, f'no thumbnail image found for {name}'
        assert (outdir / match.group(1)).is_file()

    # Hovering over a card shows the example's own intro paragraph, not Sphinx-Gallery's own
    # "go to the end to download" note, which precedes the title on every example page.
    assert 'tooltip="Uses the fixture' in forced_flat
    assert 'Go to the end to download' not in forced_flat

    # The example's own title is shown -- as a visible title div, and (hidden by
    # Sphinx-Gallery's own CSS) as the stretched link's text, not a bare docname either way.
    assert '<div class="sphx-glr-thumbnail-title">Plotting a thing</div>' in forced_flat
    assert (
        '<a class="reference internal" href="auto_examples/plot_thing.html">'
        '<span>Plotting a thing</span></a>' in forced_flat
    )


def test_autocodelink_category_labels_renames_group_headings(tmp_path):
    # renames the *display* label only -- grouping itself still goes by the real,
    # unrenamed category, so this must still land the exact same 3 groups as the
    # default-labels case above.
    outdir, _ = _build(
        tmp_path,
        confoverrides={
            'autocodelink_category_labels': {
                'Sphinx Gallery': 'Gallery Examples',
                'Documentation': 'API Docs',
                'Docstring Examples': 'API Examples',
            }
        },
    )
    refs = (outdir / 'refs.html').read_text()
    grouped = _block_after(refs, 'since 3 categories apply):')
    labels = re.findall(
        r'<p class="sphinx-autocodelink-index-group-label"><strong>([^<]*)</strong></p>', grouped
    )
    assert set(labels) == {'Gallery Examples', 'API Docs', 'API Examples'}
    # sorted by the *displayed* label ('API Docs', 'API Examples', 'Gallery Examples'), not
    # by the underlying category ('Docstring Examples', 'Documentation', 'Sphinx Gallery') --
    # those would put 'API Examples' before 'API Docs', out of alphabetical order.
    assert labels == ['API Docs', 'API Examples', 'Gallery Examples']


def test_autocodelink_category_order_overrides_alphabetical(tmp_path):
    # Reverse of alphabetical order -- proves the explicit order actually took effect.
    outdir, _ = _build(
        tmp_path,
        confoverrides={
            'autocodelink_category_order': [
                'Sphinx Gallery',
                'Documentation',
                'Docstring Examples',
            ]
        },
    )
    refs = (outdir / 'refs.html').read_text()
    grouped = _block_after(refs, 'since 3 categories apply):')
    labels = re.findall(
        r'<p class="sphinx-autocodelink-index-group-label"><strong>([^<]*)</strong></p>', grouped
    )
    assert labels == ['Sphinx Gallery', 'Documentation', 'Docstring Examples']


def test_autocodelink_category_order_partial_list_sorts_the_rest_after(tmp_path):
    outdir, _ = _build(
        tmp_path,
        confoverrides={'autocodelink_category_order': ['Docstring Examples', 'Documentation']},
    )
    refs = (outdir / 'refs.html').read_text()
    grouped = _block_after(refs, 'since 3 categories apply):')
    labels = re.findall(
        r'<p class="sphinx-autocodelink-index-group-label"><strong>([^<]*)</strong></p>', grouped
    )
    assert labels == ['Docstring Examples', 'Documentation', 'Sphinx Gallery']


def test_doctest_block_used_in_links_to_its_own_section(tmp_path):
    outdir, _ = _build(tmp_path, confoverrides={'autocodelink_doctest_blocks': True})
    refs = (outdir / 'refs.html').read_text()
    # doctest_page uses pkg.thing in both Notes and Examples; the later section wins
    assert 'href="doctest_page.html#examples"' in refs
    assert 'href="doctest_page.html#notes"' not in refs


def test_directive_in_a_docstring_anchors_to_its_section(tmp_path):
    # `.. autocodelink::` inside a docstring records through record_namespace(), which
    # has no section to name at directive-run time -- autodoc's content is still a
    # detached subtree. The anchor is resolved later, from the assembled page.
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    assert 'href="api_sectioned.html#examples"' in refs


def test_page_with_code_in_several_places_gets_no_anchor(tmp_path):
    # api.rst has code inside a docstring section and code outside any -- no telling
    # which a record came from, so it stays a plain page link rather than guessing
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    assert '<li><a href="api.html">' in refs
    assert '<li><a href="api.html#' not in refs


def test_several_objects_sharing_one_code_section_still_anchors(tmp_path):
    # api_sectioned.rst documents two objects but only one has code, so the section is
    # unambiguous -- the object count on its own must not block the anchor
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    assert 'href="api_sectioned.html#examples"' in refs


def test_doctest_blocks_off_records_nothing(tmp_path):
    outdir, _ = _build(tmp_path)
    refs = (outdir / 'refs.html').read_text()
    # the page still exists (it's in the toctree, hence the nav links) -- it just
    # contributes no "Used In" entry
    assert '<li><a href="doctest_page.html' not in refs


def test_autodoc_backrefs(tmp_path):
    outdir, _ = _build(tmp_path)
    api = (outdir / 'api.html').read_text()

    # pkg.thing is referenced elsewhere: a real, hoistable section, not just a raw block.
    section = re.search(
        r'<section class="sphinx-autocodelink-backrefs"[^>]*>.*?</section>', api, re.DOTALL
    )
    assert section is not None
    assert '<h2>Used In' in section.group()
    for page in ('index.html', 'auto_examples/plot_thing.html', 'auto_examples/plot_other.html'):
        assert f'href="{page}"' in section.group()
    # titles, not docnames; a plain link since "Documentation" is the generic bucket.
    assert '<a href="index.html">Index</a>' in section.group()

    # pkg.unused has no references: nothing appended at all, not even "No references found."
    unused_dd = re.search(r'id="pkg\.unused">.*?</dd>', api, re.DOTALL).group()
    assert 'sphinx-autocodelink-backrefs' not in unused_dd
    assert 'No references found' not in unused_dd


@needs_monitoring
def test_gallery_resolves_a_helper_functions_own_local_scope(tmp_path):
    # plot_scopes.py's `widget` only ever exists inside `show`'s own local scope, and
    # `show` is defined in one cell and called in another -- neither the example's own
    # top-level namespace nor the defining cell's scrape can resolve `widget.render`.
    outdir, _ = _build(tmp_path)
    example = (outdir / 'auto_examples' / 'plot_scopes.html').read_text()
    assert (
        '<a class="sphinx-autocodelink-a" href="../api.html#pkg.Widget.render">'
        '<span class="n">widget</span><span class="o">.</span>'
        '<span class="n">render</span></a>' in example
    )


@needs_monitoring
def test_gallery_resolves_a_receiver_no_dotted_name_addresses(tmp_path):
    # `registry['a'].describe()`: the receiver is a subscript, so there's no dotted name
    # to look up at all. Only the trailing attribute is wrapped -- `registry` keeps the
    # link its own name resolves to.
    outdir, _ = _build(tmp_path)
    example = (outdir / 'auto_examples' / 'plot_scopes.html').read_text()
    assert (
        '<a class="sphinx-autocodelink-a" href="../api.html#pkg.Registry">'
        '<span class="n">registry</span></a><span class="p">[</span>' in example
    )
    assert (
        '<a class="sphinx-autocodelink-a" href="../api.html#pkg.Widget.describe">'
        '<span class="o">.</span><span class="n">describe</span></a>' in example
    )


@needs_monitoring
def test_gallery_traced_names_reach_their_own_used_in_section(tmp_path):
    outdir, _ = _build(tmp_path)
    api = (outdir / 'api.html').read_text()
    for name in ('pkg.Widget.render', 'pkg.Widget.describe'):
        member = re.search(rf'id="{re.escape(name)}">.*?</dd>', api, re.DOTALL).group()
        assert 'href="auto_examples/plot_scopes.html"' in member


@needs_monitoring
def test_gallery_tracing_survives_joblib_workers(tmp_path):
    outdir, _ = _build(tmp_path, parallel=2)
    example = (outdir / 'auto_examples' / 'plot_scopes.html').read_text()
    assert 'href="../api.html#pkg.Widget.render"' in example
    assert 'href="../api.html#pkg.Widget.describe"' in example


def test_gallery_tracing_can_be_turned_off(tmp_path):
    # `trace=False` on the scraper is the kill switch: the top-level namespace still
    # records, everything only its own scopes could resolve stops.
    srcdir = tmp_path / 'src'
    shutil.copytree(TINYPAGES, srcdir)
    conf = srcdir / 'conf.py'
    conf.write_text(
        conf.read_text().replace('AutoCodeLinkScraper()', 'AutoCodeLinkScraper(trace=False)')
    )
    app = Sphinx(
        srcdir=str(srcdir),
        confdir=str(srcdir),
        outdir=str(tmp_path / 'out'),
        doctreedir=str(tmp_path / 'doctrees'),
        buildername='html',
    )
    app.build()
    example = (tmp_path / 'out' / 'auto_examples' / 'plot_scopes.html').read_text()
    assert 'href="../api.html#pkg.Registry"' in example
    assert 'href="../api.html#pkg.Widget.render"' not in example
    assert 'href="../api.html#pkg.Widget.describe"' not in example
