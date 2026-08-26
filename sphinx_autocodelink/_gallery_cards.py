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

#: Thumbnails bundled into each carousel card -- medium screens and up show all four
#: in one row, only truly narrow (mobile-width) screens wrap them 2x2 (see the
#: row-cols classes in ``_card_html``).
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
#: fixes every thumbnail (image, title, and usage count alike) to the same box size,
#: so cards with different title lengths still come out a uniform height. The title's
#: own `line-height` is pinned explicitly too: `height: 2.6em` only clips cleanly at
#: exactly two lines if each line is `1.3em` tall, and the surrounding theme's actual
#: line-height isn't guaranteed to match that -- left unset, a taller line-height
#: clips the second line mid-glyph, reading as if it collided with the usage count.
#: `width: 100%` and `min-width: 0` on the title are needed too: the thumbcontainer is
#: a flex column (Sphinx-Gallery's own layout), and a flex item's `min-width` defaults
#: to `auto` -- its unwrapped content width -- rather than the container's, so a long
#: title can overflow past both edges of its own card instead of wrapping to fit it.
#:
#: sphinx-design's own grid gutters are horizontal-only (bootstrap-style: `.sd-row`
#: gets a negative side margin, `.sd-col` a matching positive side padding, `row-gap`
#: is never set) and layered under an extra `.sd-container-fluid` side padding on top
#: of that -- so by default the edge-to-thumbnail gap ends up wider than, and
#: different from, the between-thumbnail gap, while there's no vertical gap between
#: wrapped rows at all. Zeroing the container's own padding and giving the row a
#: matching negative margin/positive column padding pair collapses the outer edge
#: back down to just the card body's own padding (equal on all four sides by
#: default), and setting `column-gap: 0` (the horizontal gap comes from the
#: margin/padding pair, not this) plus an explicit `row-gap` makes the vertical gap
#: between wrapped rows match that same value.
#:
#: The last block fixes a layout shift: sphinx-design's own carousel CSS is
#: `overflow-x: hidden`, switching to `overflow-x: auto` only on `:hover`/`:focus` --
#: so a non-overlay scrollbar (its track occupies real layout space, unlike macOS's
#: overlay style) only appears, and only then claims its ~8px of height, once the
#: mouse arrives, nudging every element below the carousel down for as long as the
#: mouse stays there. `overflow-x: scroll` always reserves that space, whether or not
#: the mouse is over it; the thumb/track are then made transparent by default and
#: given a visible color only on hover/focus, so the *reserved space* stays constant
#: while the *scrollbar's visibility* still toggles the way sphinx-design's did.
_CAROUSEL_STYLE = (
    '<style>'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbcontainer{overflow:hidden}'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbcontainer img{'
    'width:100%;max-width:none;height:120px;max-height:none;'
    'object-fit:cover;display:block}'
    '.sphinx-autocodelink-gallery-carousel .sphx-glr-thumbnail-title{'
    'width:100%;min-width:0;height:2.6em;line-height:1.3em;overflow:hidden;'
    'display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}'
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


def _thumbnail_html(
    app: Sphinx, *, docname: str, ref: str, title: str, usage_count: int | None
) -> str:
    """Render one Sphinx-Gallery-style thumbnail card for ``ref``, linked from ``docname``.

    ``usage_count`` -- how many times ``ref``'s own recorded source used the target this
    carousel is a "Used In" entry for -- is shown as a subtitle line under the title when
    given (``autocodelink_sort == 'frequency'``; see :func:`render_gallery_carousel`),
    None to omit it entirely rather than show a meaningless "0 uses".
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

    Sorted by title (``autocodelink_sort``'s default, ``'alphabetical'``), or by
    ``usage_counts`` descending when it's ``'frequency'`` instead, ties broken
    alphabetically -- the same ordering :func:`sphinx_autocodelink._render_ref_list`
    applies to a plain link list, and each card's own count shown the same way: as
    plain text, not part of the link. ``_THUMBNAILS_PER_CARD`` thumbnails per card, as
    many cards as needed, in one horizontally-scrolling carousel -- this replaces the
    usual "N more" collapse entirely, at any length.
    """
    usage_counts = usage_counts or {}
    show_counts = getattr(app.config, 'autocodelink_sort', 'alphabetical') == 'frequency'
    pairs = [(_docname_title(app, ref), ref) for ref in refs]
    if show_counts:
        labeled = sorted(pairs, key=lambda pair: (-usage_counts.get(pair[1], 0), pair[0]))
    else:
        labeled = sorted(pairs)
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
