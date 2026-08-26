"""Unit tests for sphinx_autocodelink._gallery_cards, without a full Sphinx build."""

from __future__ import annotations

from types import SimpleNamespace

from docutils import nodes

from sphinx_autocodelink import _gallery_cards as cards


def _doctree(title_text=None, paragraph_text=None):
    """Return a document with an optional title and an optional paragraph after it."""
    document = nodes.document(settings=SimpleNamespace(), reporter=SimpleNamespace())
    section = nodes.section()
    if title_text is not None:
        section += nodes.title(title_text, title_text)
    if paragraph_text is not None:
        section += nodes.paragraph(paragraph_text, paragraph_text)
    document += section
    return document


def _fake_app(
    *,
    srcdir,
    titles=None,
    images=None,
    doctrees=None,
    target_uris=None,
    relative_uris=None,
    sort='alphabetical',
):
    """Build a minimal Sphinx-app-shaped fake for the functions under test.

    ``srcdir`` is a real directory (typically ``tmp_path``): ``_thumbnail_source_path``
    globs the filesystem for real, so a test exercising it needs one, and every other
    test needs to pass *some* directory even if it's never actually read.
    """
    titles = titles or {}
    images = images or {}
    doctrees = doctrees or {}
    target_uris = target_uris or {}
    relative_uris = relative_uris or {}

    def get_doctree(docname):
        if docname not in doctrees:
            msg = 'no doctree'
            raise Exception(msg)  # noqa: TRY002 -- matches an arbitrary env failure
        return doctrees[docname]

    return SimpleNamespace(
        srcdir=str(srcdir),
        env=SimpleNamespace(titles=titles, images=images, get_doctree=get_doctree),
        builder=SimpleNamespace(
            get_target_uri=lambda docname: target_uris.get(docname, f'{docname}.html'),
            get_relative_uri=lambda from_, to: relative_uris.get(to, f'{to}.html'),
        ),
        config=SimpleNamespace(autocodelink_sort=sort),
    )


def _write_thumbnail(srcdir, docname, filename):
    """Create a real (empty) thumbnail file at the path Sphinx-Gallery would write it to."""
    thumb_dir = (srcdir / docname).parent / 'images' / 'thumb'
    thumb_dir.mkdir(parents=True, exist_ok=True)
    (thumb_dir / filename).touch()


def test_docname_title_known(tmp_path):
    app = _fake_app(srcdir=tmp_path, titles={'page': nodes.title('Page Title', 'Page Title')})
    assert cards._docname_title(app, 'page') == 'Page Title'


def test_docname_title_unknown_falls_back_to_docname(tmp_path):
    app = _fake_app(srcdir=tmp_path)
    assert cards._docname_title(app, 'page') == 'page'


def test_thumbnail_source_path_finds_the_real_extension(tmp_path):
    app = _fake_app(srcdir=tmp_path)
    docname = 'examples/01-filter/clip_closed_surface'
    _write_thumbnail(tmp_path, docname, 'sphx_glr_clip_closed_surface_thumb.gif')
    path = cards._thumbnail_source_path(app, docname)
    assert path == 'examples/01-filter/images/thumb/sphx_glr_clip_closed_surface_thumb.gif'


def test_thumbnail_source_path_top_level_docname(tmp_path):
    app = _fake_app(srcdir=tmp_path)
    _write_thumbnail(tmp_path, 'plot_thing', 'sphx_glr_plot_thing_thumb.png')
    path = cards._thumbnail_source_path(app, 'plot_thing')
    assert path == 'images/thumb/sphx_glr_plot_thing_thumb.png'


def test_thumbnail_source_path_none_when_never_written(tmp_path):
    app = _fake_app(srcdir=tmp_path)
    assert cards._thumbnail_source_path(app, 'plot_thing') is None


def test_thumbnail_href_resolves_via_env_images(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        images={
            'examples/images/thumb/sphx_glr_plot_thing_thumb.png': (
                {'auto_examples/index'},
                'sphx_glr_plot_thing_thumb.png',
            )
        },
        target_uris={'api': 'api.html'},
    )
    href = cards._thumbnail_href(app, 'api', 'examples/images/thumb/sphx_glr_plot_thing_thumb.png')
    assert href == '_images/sphx_glr_plot_thing_thumb.png'


def test_thumbnail_href_renamed_on_collision(tmp_path):
    # env.images renames on a basename collision -- the unique name, not the original
    # basename, is what's actually under _images/ in the built output.
    app = _fake_app(
        srcdir=tmp_path,
        images={'a/thumb.png': ({'x'}, 'thumb1.png')},
        target_uris={'api': 'api.html'},
    )
    assert cards._thumbnail_href(app, 'api', 'a/thumb.png') == '_images/thumb1.png'


def test_thumbnail_href_none_when_never_built(tmp_path):
    app = _fake_app(srcdir=tmp_path)
    assert cards._thumbnail_href(app, 'api', 'nonexistent/thumb.png') is None


def test_thumbnail_href_relative_to_a_nested_page(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        images={'thumb.png': ({'x'}, 'thumb.png')},
        target_uris={'guide/api': 'guide/api.html'},
    )
    assert cards._thumbnail_href(app, 'guide/api', 'thumb.png') == '../_images/thumb.png'


def test_page_intro_uses_first_paragraph_after_title(tmp_path):
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Title', 'The real intro.')})
    assert cards._page_intro(app, 'ex') == 'The real intro.'


def test_page_intro_skips_content_before_the_title(tmp_path):
    # Sphinx-Gallery inserts its own "go to the end to download" note *before* the
    # title on every example page -- a paragraph anywhere on the page isn't enough.
    document = nodes.document(settings=SimpleNamespace(), reporter=SimpleNamespace())
    document += nodes.paragraph('Go to the end to download.', 'Go to the end to download.')
    section = nodes.section()
    section += nodes.title('Title', 'Title')
    section += nodes.paragraph('The real intro.', 'The real intro.')
    document += section

    app = _fake_app(srcdir=tmp_path, doctrees={'ex': document})
    assert cards._page_intro(app, 'ex') == 'The real intro.'


def test_page_intro_falls_back_to_title_with_no_paragraph(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        titles={'ex': nodes.title('Title', 'Title')},
        doctrees={'ex': _doctree('Title')},
    )
    assert cards._page_intro(app, 'ex') == 'Title'


def test_page_intro_falls_back_to_title_on_missing_doctree(tmp_path):
    app = _fake_app(srcdir=tmp_path, titles={'ex': nodes.title('Title', 'Title')})
    assert cards._page_intro(app, 'ex') == 'Title'


def test_page_intro_collapses_whitespace(tmp_path):
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Title', 'Line one\nline two')})
    assert cards._page_intro(app, 'ex') == 'Line one line two'


def test_page_intro_truncates_long_text(tmp_path):
    long_text = 'x' * 200
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Title', long_text)})
    intro = cards._page_intro(app, 'ex')
    assert len(intro) == cards._INTRO_MAX_CHARS
    assert intro.endswith('…')


def test_page_intro_leaves_short_text_untouched(tmp_path):
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Title', 'Short.')})
    assert cards._page_intro(app, 'ex') == 'Short.'


def test_thumbnail_html_includes_image_when_resolvable(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        titles={'ex': nodes.title('Ex', 'Ex')},
        images={'images/thumb/sphx_glr_ex_thumb.png': ({'x'}, 'sphx_glr_ex_thumb.png')},
        doctrees={'ex': _doctree('Ex', 'An intro.')},
    )
    _write_thumbnail(tmp_path, 'ex', 'sphx_glr_ex_thumb.png')
    html = cards._thumbnail_html(app, docname='index', ref='ex', title='Ex', usage_count=None)
    assert '<img src="_images/sphx_glr_ex_thumb.png" alt="">' in html
    assert 'tooltip="An intro."' in html
    assert 'sphx-glr-thumbcontainer' in html


def test_thumbnail_html_omits_image_when_unresolvable(tmp_path):
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Ex', 'An intro.')})
    html = cards._thumbnail_html(app, docname='index', ref='ex', title='Ex', usage_count=None)
    assert '<img' not in html
    assert 'sphx-glr-thumbcontainer' in html


def test_thumbnail_html_link_text_is_wrapped_in_a_span(tmp_path):
    # Sphinx-Gallery's own CSS only hides this link's text if it's wrapped in a <span> --
    # see the comment in _thumbnail_html.
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Ex', 'An intro.')})
    html = cards._thumbnail_html(app, docname='index', ref='ex', title='Ex', usage_count=None)
    assert '<a class="reference internal" href="ex.html"><span>Ex</span></a>' in html


def test_thumbnail_html_escapes_title_and_tooltip(tmp_path):
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Ex', 'A <script> intro & more.')})
    html = cards._thumbnail_html(
        app, docname='index', ref='ex', title='A & <Title>', usage_count=None
    )
    assert '<script>' not in html
    assert 'A &amp; &lt;Title&gt;' in html
    assert 'A &lt;script&gt; intro &amp; more.' in html


def test_thumbnail_html_shows_usage_count_singular(tmp_path):
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Ex', 'An intro.')})
    html = cards._thumbnail_html(app, docname='index', ref='ex', title='Ex', usage_count=1)
    assert '<div class="sphinx-autocodelink-usage-count">1 use</div>' in html


def test_thumbnail_html_shows_usage_count_plural(tmp_path):
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Ex', 'An intro.')})
    html = cards._thumbnail_html(app, docname='index', ref='ex', title='Ex', usage_count=3)
    assert '<div class="sphinx-autocodelink-usage-count">3 uses</div>' in html


def test_thumbnail_html_omits_usage_count_when_none(tmp_path):
    app = _fake_app(srcdir=tmp_path, doctrees={'ex': _doctree('Ex', 'An intro.')})
    html = cards._thumbnail_html(app, docname='index', ref='ex', title='Ex', usage_count=None)
    assert 'sphinx-autocodelink-usage-count' not in html


def test_render_gallery_carousel_sorts_by_frequency_and_shows_counts(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        sort='frequency',
        titles={'a': nodes.title('Alpha', 'Alpha'), 'b': nodes.title('Bravo', 'Bravo')},
        doctrees={'a': _doctree('Alpha', 'Intro.'), 'b': _doctree('Bravo', 'Intro.')},
    )
    html = cards.render_gallery_carousel(
        ['a', 'b'], docname='index', app=app, usage_counts={'a': 1, 'b': 5}
    )
    # Bravo (5 uses) ranks before Alpha (1 use), reversing alphabetical order.
    assert html.index('Bravo') < html.index('Alpha')
    assert '<div class="sphinx-autocodelink-usage-count">5 uses</div>' in html
    assert '<div class="sphinx-autocodelink-usage-count">1 use</div>' in html


def test_render_gallery_carousel_alphabetical_mode_shows_no_counts(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        titles={'a': nodes.title('Alpha', 'Alpha')},
        doctrees={'a': _doctree('Alpha', 'Intro.')},
    )
    html = cards.render_gallery_carousel(['a'], docname='index', app=app, usage_counts={'a': 5})
    # The style block's own CSS selector mentions the class name regardless -- what
    # matters is that no *element* using it was actually rendered.
    assert '<div class="sphinx-autocodelink-usage-count">' not in html


def test_card_html_wraps_each_thumbnail_in_a_grid_item():
    html = cards._card_html(['<div>one</div>', '<div>two</div>'])
    assert html.count('sd-col sd-d-flex-column') == 2
    assert 'sd-card sd-sphinx-override' in html
    assert 'sd-row-cols-lg-4' in html


def test_render_gallery_carousel_sorted_by_title(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        titles={'b': nodes.title('Bravo', 'Bravo'), 'a': nodes.title('Alpha', 'Alpha')},
        doctrees={'b': _doctree('Bravo', 'Bravo intro.'), 'a': _doctree('Alpha', 'Alpha intro.')},
    )
    html = cards.render_gallery_carousel(['b', 'a'], docname='index', app=app)
    assert html.index('Alpha') < html.index('Bravo')


def test_render_gallery_carousel_wraps_in_carousel_container(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        titles={'a': nodes.title('Alpha', 'Alpha')},
        doctrees={'a': _doctree('Alpha', 'Intro.')},
    )
    html = cards.render_gallery_carousel(['a'], docname='index', app=app)
    assert 'sd-cards-carousel' in html
    assert html.count('sd-card sd-sphinx-override') == 1


def test_render_gallery_carousel_includes_style_once(tmp_path):
    app = _fake_app(
        srcdir=tmp_path,
        titles={'a': nodes.title('Alpha', 'Alpha')},
        doctrees={'a': _doctree('Alpha', 'Intro.')},
    )
    html = cards.render_gallery_carousel(['a'], docname='index', app=app)
    assert html.count('<style>') == 1


def test_render_gallery_carousel_chunks_into_multiple_cards(tmp_path):
    refs = [f'p{i}' for i in range(cards._THUMBNAILS_PER_CARD + 1)]
    app = _fake_app(
        srcdir=tmp_path,
        titles={ref: nodes.title(ref, ref) for ref in refs},
        doctrees={ref: _doctree(ref, 'Intro.') for ref in refs},
    )
    html = cards.render_gallery_carousel(refs, docname='index', app=app)
    assert html.count('sd-card sd-sphinx-override') == 2
    assert html.count('<div class="sphx-glr-thumbcontainer"') == len(refs)


def test_render_gallery_carousel_empty_refs(tmp_path):
    app = _fake_app(srcdir=tmp_path)
    html = cards.render_gallery_carousel([], docname='index', app=app)
    assert 'sd-cards-carousel' in html
    assert 'sd-card sd-sphinx-override' not in html
