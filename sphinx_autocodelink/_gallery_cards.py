"""Sphinx-Gallery-style thumbnail cards for the "Used In" list's gallery entries.

Opt-in via ``autocodelink_gallery_cards`` (see :func:`sphinx_autocodelink.setup`).
Reuses Sphinx-Gallery's own ``sphx-glr-thumbcontainer`` markup -- including its
CSS-only hover tooltip (``content: attr(tooltip)``, no JS involved) -- and
sphinx-design's card/grid/carousel markup, both already loaded site-wide once those
two extensions are active (Sphinx-Gallery is a hard requirement of this module's
caller; sphinx-design is not, but its CSS is inert, not an error, if absent).

The HTML below is hand-built rather than emitted as ``.. card-carousel::``/``..
card::``/``.. grid::``/Sphinx-Gallery's own thumbnail directives, because it runs
post-build, rewriting already-built HTML files (see
:func:`sphinx_autocodelink._embed_links`) well after Sphinx's own RST parser has
run -- nothing would ever parse those directives at that point. That does mean this
is pinned to the current HTML output of both extensions, not a stable public
contract of either.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from docutils import nodes
from sphinx.util.osutil import relative_uri

if TYPE_CHECKING:
    from sphinx.application import Sphinx

#: Thumbnails bundled into each carousel card -- large screens show all four in one
#: row, smaller screens wrap them 2x2 (see the row-cols classes in ``_card_html``).
_THUMBNAILS_PER_CARD = 4

#: Sphinx-Gallery's own intro truncation length (sphinx_gallery.gen_rst docstring).
_INTRO_MAX_CHARS = 95

#: Overrides Sphinx-Gallery's own thumbnail sizing, which assumes its own gallery
#: index page's fixed-width grid cells. A carousel card's own grid column is narrower
#: at some breakpoints (see _card_html) and always variable-width, so a fixed 160px
#: image can be wider than its actual container -- the container itself doesn't clip
#: (Sphinx-Gallery's own CSS leaves it `overflow: visible`), so the excess is visible
#: even through the hover tooltip overlay, which is sized to the container, not the
#: oversized image. A `.sphx-glr-thumbcontainer img` selector alone isn't enough to
#: win here either: `max-width`/`max-height` always cap a `width`/`height` override
#: regardless of selector specificity, so those need overriding explicitly too. Also
#: fixes every thumbnail (image and title alike) to the same box size, so cards with
#: different title lengths still come out a uniform height.
_CAROUSEL_STYLE = (
    '<style>'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbcontainer{overflow:hidden}'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbcontainer img{'
    'width:100%;max-width:none;height:120px;max-height:none;'
    'object-fit:cover;display:block}'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbnail-title{'
    'height:2.6em;overflow:hidden;display:-webkit-box;'
    '-webkit-line-clamp:2;-webkit-box-orient:vertical}'
    '</style>'
)


def _docname_title(app: Sphinx, docname: str) -> str:
    """Return ``docname``'s page title, or the docname itself if it has none on record."""
    title_node = app.env.titles.get(docname)
    return title_node.astext() if title_node is not None else docname


def _thumbnail_source_path(app: Sphinx, docname: str) -> str | None:
    """Return the source-relative path Sphinx-Gallery wrote an example's thumbnail to.

    Matches ``sphinx_gallery.backreferences._thumbnail_div``'s own convention: the
    example's own directory, then ``images/thumb/sphx_glr_<example filename>_thumb.<ext>``.
    The extension varies -- an animated example's own thumbnail is a ``.gif``, a plain
    one a ``.png`` -- so it's found on disk (mirroring how Sphinx-Gallery itself finds
    its own thumbnail file) rather than assumed. None if no such file was ever written
    (the example never rendered, or hasn't been built with an image scraper enabled).
    """
    path = PurePosixPath(docname)
    thumb_dir = Path(app.srcdir) / path.parent / 'images' / 'thumb'
    matches = sorted(thumb_dir.glob(f'sphx_glr_{path.name}_thumb.*'))
    if not matches:
        return None
    return str(path.parent / 'images' / 'thumb' / matches[0].name)


def _thumbnail_href(app: Sphinx, docname: str, source_path: str) -> str | None:
    """Return the built thumbnail's URL relative to ``docname``, or None if never built.

    Looked up via Sphinx's own image-collection bookkeeping (``env.images``) rather
    than assumed: the HTML builder flattens every referenced image into a single
    ``_images/`` directory, renaming on a basename collision, so the final filename
    isn't necessarily the source one.
    """
    entry = app.env.images.get(source_path)
    if entry is None:
        return None
    _docnames, unique_name = entry
    return relative_uri(app.builder.get_target_uri(docname), f'_images/{unique_name}')


def _page_intro(app: Sphinx, docname: str) -> str:
    """Return the first paragraph of ``docname``'s own page, truncated for a tooltip.

    Mirrors ``sphinx_gallery.gen_rst.extract_intro_and_title``: the first paragraph
    *within the title's own section*, falling back to the page's title if it has none,
    collapsed to one line and capped at the same length Sphinx-Gallery itself uses.
    Scoped to the title's section rather than just "the first paragraph on the page"
    because Sphinx-Gallery inserts its own boilerplate note (pointing readers at the
    downloads at the bottom) *before* the title on every example page -- naively taking
    the very first paragraph anywhere picks that up instead of the real intro.
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


def _thumbnail_html(app: Sphinx, *, docname: str, ref: str, title: str) -> str:
    """Render one Sphinx-Gallery-style thumbnail card for ``ref``, linked from ``docname``."""
    href = app.builder.get_relative_uri(docname, ref)
    source_path = _thumbnail_source_path(app, ref)
    thumb_href = _thumbnail_href(app, docname, source_path) if source_path is not None else None
    image = f'<img src="{thumb_href}" alt="">' if thumb_href is not None else ''
    escaped_title = escape(title)
    tooltip = escape(_page_intro(app, ref))
    return (
        f'<div class="sphx-glr-thumbcontainer" tooltip="{tooltip}">'
        f'{image}'
        # A real `:doc:` role would render as this same <p><a><span>: Sphinx-Gallery's
        # CSS stretches the <a> to cover the whole container and hides the <span> text
        # inside it, using this link only to make the container clickable -- the title
        # below is what's actually shown.
        f'<p><a class="reference internal" href="{href}"><span>{escaped_title}</span></a></p>'
        f'<div class="sphx-glr-thumbnail-title">{escaped_title}</div>'
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
        f'sd-row-cols-md-2 sd-row-cols-lg-4">{items}</div>'
        '</div></div></div>'
    )


def render_gallery_carousel(refs: list[str], *, docname: str, app: Sphinx) -> str:
    """Render ``refs`` as a card-carousel of Sphinx-Gallery-style thumbnail cards.

    Sorted by title, like a plain link list would be; ``_THUMBNAILS_PER_CARD`` thumbnails
    per card, as many cards as needed, in one horizontally-scrolling carousel -- this
    replaces the usual "N more" collapse entirely, at any length.
    """
    labeled = sorted((_docname_title(app, ref), ref) for ref in refs)
    thumbnails = [
        _thumbnail_html(app, docname=docname, ref=ref, title=title) for title, ref in labeled
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
