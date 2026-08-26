"""Sphinx-Gallery-style thumbnail cards for the "Used In" list's gallery entries.

Opt-in via ``autocodelink_gallery_cards``. Reuses Sphinx-Gallery's own
``sphx-glr-thumbcontainer`` markup and sphinx-design's card/grid/carousel markup, both
already loaded site-wide once those extensions are active. The HTML is hand-built
because this runs post-build, long after Sphinx's own RST parser, so it is pinned to
those extensions' current output rather than a public contract.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from docutils import nodes
from sphinx.util.osutil import relative_uri

from sphinx_autocodelink import _docname_title
from sphinx_autocodelink import _sorted_refs

if TYPE_CHECKING:
    from sphinx.application import Sphinx

#: Thumbnails per carousel card: one row, wrapping 2x2 only on mobile widths.
_THUMBNAILS_PER_CARD = 4

#: Sphinx-Gallery's own intro truncation length (sphinx_gallery.gen_rst docstring).
_INTRO_MAX_CHARS = 95

#: Sizes thumbnails to the carousel's own variable-width columns rather than
#: Sphinx-Gallery's fixed grid cells, and reserves the scrollbar's space at all times
#: so hovering the carousel doesn't shift the page.
_CAROUSEL_STYLE = (
    '<style>'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbcontainer{overflow:hidden;height:100%}'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbcontainer img{'
    'width:100%;max-width:none;height:120px;max-height:none;'
    'object-fit:cover;display:block}'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbnail-title{'
    'width:100%;min-width:0;min-height:2.6em;line-height:1.3em}'
    '.sphinx-autocodelink-gallery-carousel .sphinx-autocodelink-usage-count{'
    'height:1.2em;font-size:0.85em;opacity:0.75}'
    '.sphinx-autocodelink-gallery-carousel .sd-container-fluid{padding:0}'
    '.sphinx-autocodelink-gallery-carousel .sd-row{'
    'margin:0 -8px;column-gap:0;row-gap:16px}'
    '.sphinx-autocodelink-gallery-carousel .sd-col{padding:0 8px}'
    '.sphinx-autocodelink-gallery-carousel.sd-cards-carousel{'
    'overflow-x:scroll!important;scrollbar-color:transparent transparent}'
    '.sphinx-autocodelink-gallery-carousel.sd-cards-carousel::-webkit-scrollbar{height:8px}'
    '.sphinx-autocodelink-gallery-carousel.sd-cards-carousel::-webkit-scrollbar-track{'
    'background:transparent}'
    '.sphinx-autocodelink-gallery-carousel.sd-cards-carousel::-webkit-scrollbar-thumb{'
    'background:transparent}'
    '.sphinx-autocodelink-gallery-carousel.sd-cards-carousel:hover,'
    '.sphinx-autocodelink-gallery-carousel.sd-cards-carousel:focus-within{'
    'scrollbar-color:rgba(128,128,128,.6) transparent}'
    '.sphinx-autocodelink-gallery-carousel.sd-cards-carousel:hover::-webkit-scrollbar-thumb,'
    '.sphinx-autocodelink-gallery-carousel.sd-cards-carousel:focus-within::-webkit-scrollbar-thumb{'
    'background:rgba(128,128,128,.6)}'
    '</style>'
)


def _thumbnail_source_path(app: Sphinx, docname: str) -> str | None:
    """Return the source-relative path Sphinx-Gallery wrote an example's thumbnail to.

    ``<example dir>/images/thumb/sphx_glr_<name>_thumb.<ext>``, with the extension
    found on disk since it varies. None if no thumbnail was ever written.
    """
    path = PurePosixPath(docname)
    thumb_dir = Path(app.srcdir) / path.parent / 'images' / 'thumb'
    matches = sorted(thumb_dir.glob(f'sphx_glr_{path.name}_thumb.*'))
    if not matches:
        return None
    return str(path.parent / 'images' / 'thumb' / matches[0].name)


def _thumbnail_href(app: Sphinx, docname: str, source_path: str) -> str | None:
    """Return the built thumbnail's URL relative to ``docname``, or None if never built.

    Read from ``env.images``, since the builder renames images on a basename collision.
    """
    entry = app.env.images.get(source_path)
    if entry is None:
        return None
    _docnames, unique_name = entry
    return relative_uri(app.builder.get_target_uri(docname), f'_images/{unique_name}')


def _page_intro(app: Sphinx, docname: str) -> str:
    """Return the first paragraph of ``docname``'s own page, truncated for a tooltip.

    Scoped to the title's own section, so Sphinx-Gallery's boilerplate note above the
    title isn't mistaken for the intro. Falls back to the title itself.
    """
    paragraph = None
    try:
        doctree = app.env.get_doctree(docname)
    except Exception:  # noqa: BLE001, S110 -- a page that failed to build shouldn't sink this
        pass
    else:
        title_node = next(doctree.findall(nodes.title), None)
        if title_node is not None and title_node.parent is not None:
            paragraph = next(title_node.parent.findall(nodes.paragraph), None)
    text = paragraph.astext() if paragraph is not None else _docname_title(app, docname)
    text = ' '.join(text.split())
    if len(text) <= _INTRO_MAX_CHARS:
        return text
    return text[: _INTRO_MAX_CHARS - 1].rstrip() + '…'


def _thumbnail_html(
    app: Sphinx, *, docname: str, ref: str, title: str, usage_count: int | None
) -> str:
    """Render one Sphinx-Gallery-style thumbnail card for ``ref``, linked from ``docname``.

    ``usage_count`` becomes a subtitle under the title, or None to omit it.
    """
    href = app.builder.get_relative_uri(docname, ref)
    source_path = _thumbnail_source_path(app, ref)
    thumb_href = _thumbnail_href(app, docname, source_path) if source_path is not None else None
    image = f'<img src="{thumb_href}" alt="">' if thumb_href is not None else ''
    escaped_title = escape(title)
    tooltip = escape(_page_intro(app, ref))
    count_html = ''
    if usage_count is not None:
        uses = 'use' if usage_count == 1 else 'uses'
        count_html = f'<div class="sphinx-autocodelink-usage-count">{usage_count} {uses}</div>'
    return (
        f'<div class="sphx-glr-thumbcontainer" tooltip="{tooltip}">'
        f'{image}'
        # A real `:doc:` role would render as this same <p><a><span>: Sphinx-Gallery's
        # CSS stretches the <a> to cover the whole container and hides the <span> text
        # inside it, using this link only to make the container clickable -- the title
        # below is what's actually shown.
        f'<p><a class="reference internal" href="{href}"><span>{escaped_title}</span></a></p>'
        f'<div class="sphx-glr-thumbnail-title">{escaped_title}</div>'
        f'{count_html}'
        '</div>'
    )


def _card_html(thumbnails: list[str]) -> str:
    """Wrap up to :data:`_THUMBNAILS_PER_CARD` thumbnails in one sphinx-design card."""
    items = ''.join(f'<div class="sd-col sd-d-flex-column">{t}</div>' for t in thumbnails)
    return (
        '<div class="sd-card sd-sphinx-override sd-mb-3 sd-shadow-sm">'
        '<div class="sd-card-body">'
        '<div class="sd-container-fluid sd-sphinx-override">'
        '<div class="sd-row sd-row-cols-2 sd-row-cols-xs-2 sd-row-cols-sm-2 '
        f'sd-row-cols-md-4 sd-row-cols-lg-4">{items}</div>'
        '</div></div></div>'
    )


def render_gallery_carousel(
    refs: list[str],
    *,
    docname: str,
    app: Sphinx,
    usage_counts: dict[str, int] | None = None,
) -> str:
    """Render ``refs`` as a card-carousel of Sphinx-Gallery-style thumbnail cards.

    Ordered and counted like :func:`sphinx_autocodelink._render_ref_list`, in one
    horizontally-scrolling carousel instead of a collapsing list.
    """
    usage_counts = usage_counts or {}
    show_counts = getattr(app.config, 'autocodelink_show_usage_count', False)
    labeled = _sorted_refs(refs, app=app, usage_counts=usage_counts)
    thumbnails = [
        _thumbnail_html(
            app,
            docname=docname,
            ref=ref,
            title=title,
            usage_count=usage_counts.get(ref, 0) if show_counts else None,
        )
        for title, ref in labeled
    ]
    cards = [
        _card_html(thumbnails[i : i + _THUMBNAILS_PER_CARD])
        for i in range(0, len(thumbnails), _THUMBNAILS_PER_CARD)
    ]
    return (
        f'{_CAROUSEL_STYLE}'
        '<div class="sd-sphinx-override sd-cards-carousel sd-card-cols-1 '
        f'sphinx-autocodelink-gallery-carousel">{"".join(cards)}</div>'
    )
