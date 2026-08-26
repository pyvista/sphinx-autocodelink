"""Sphinx-Gallery ``image_scrapers`` integration.

Records identifiers from Sphinx-Gallery's own example execution, without
producing an image. Sphinx-Gallery's own parallel example generation runs in
separate joblib worker processes that never go through Sphinx's own
``env-merge-info``, so records go to disk instead, and get picked back up at
``build-finished`` (see :func:`sphinx_autocodelink.record_namespace_to_disk`).

An example's own top-level namespace is all a scraper is handed, which leaves
out everything that only exists deeper in: a helper function's locals, a
receiver no dotted name addresses. :func:`reset_autocodelink` closes that gap by
tracing the example's execution itself -- Sphinx-Gallery's ``reset_modules``
hook is the one that fires *before* an example runs, which is what tracing
needs. It's wired up automatically wherever :class:`AutoCodeLinkScraper` is
configured (see :func:`sphinx_autocodelink.setup`), so there's nothing to add
by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sphinx.util import logging as sphinx_logging

from sphinx_autocodelink import DEFAULT_GALLERY_CATEGORY
from sphinx_autocodelink import DEFAULT_RECORDS_DIR
from sphinx_autocodelink import record_namespace_to_disk
from sphinx_autocodelink._scope_records import ExampleRecorder
from sphinx_autocodelink._scope_records import clear_caches
from sphinx_autocodelink._tracing import ScopeTracer
from sphinx_autocodelink._tracing import monitoring_available

_logger = sphinx_logging.getLogger(__name__)

#: The tracer and record collector for whichever example is running in this process.
#: Process-global rather than scraper state because the two halves are driven from
#: opposite ends of Sphinx-Gallery's own machinery -- ``reset_modules`` before the
#: example, ``image_scrapers`` after each of its blocks -- and Sphinx-Gallery runs
#: exactly one example at a time per process, parallel workers included.
_RECORDER = ExampleRecorder()
_TRACER: ScopeTracer | None = None

#: Dotted name of :func:`reset_autocodelink`, as Sphinx-Gallery's ``reset_modules``
#: takes it -- a string keeps ``sphinx_gallery_conf`` picklable for Sphinx's config cache.
RESET_AUTOCODELINK = 'sphinx_autocodelink.gallery.reset_autocodelink'

#: Set once the first tracing failure has been reported, to keep a broken build
#: from repeating the same warning for every remaining example.
_REPORTED_FAILURE = False


def _report_failure(error: BaseException) -> None:
    """Warn once that tracing has been given up on for the rest of this build."""
    global _REPORTED_FAILURE
    if not _REPORTED_FAILURE:
        _REPORTED_FAILURE = True
        # Not print(): Sphinx-Gallery redirects stdout into the example's own page
        # output while a block runs, which is exactly when this can fire.
        _logger.warning(
            'autocodelink: gallery example tracing disabled for the rest of the build (%s: %s)',
            type(error).__name__,
            error,
        )


def _tracer() -> ScopeTracer:
    """Return this process's tracer, creating it on first use."""
    global _TRACER
    if _TRACER is None:
        _TRACER = ScopeTracer(_RECORDER.on_scope, _RECORDER.on_call, _report_failure)
    return _TRACER


def reset_autocodelink(
    gallery_conf: dict[str, Any], fname: str | None, when: str = 'before'
) -> None:
    """Trace the example about to run, for identifiers its top-level namespace misses.

    A Sphinx-Gallery ``reset_modules`` entry. Added automatically alongside
    :class:`AutoCodeLinkScraper`; adding it by hand is only needed if something
    in the build replaces ``sphinx_gallery_conf['reset_modules']`` after
    ``config-inited``:

    .. code-block:: python

        sphinx_gallery_conf = {
            'reset_modules': (..., reset_autocodelink),
        }

    Tracing needs :mod:`sys.monitoring` (Python 3.12+); below that this is a
    no-op, and examples resolve from their top-level namespace only.
    """
    tracer = _tracer()
    if when != 'before' or not fname:
        tracer.stop()
        clear_caches()
        _RECORDER.drain()
        return
    _RECORDER.drain()
    clear_caches()
    tracer.start(fname)


class AutoCodeLinkScraper:
    """A no-op ``image_scrapers`` entry that records identifiers for linking.

    Add alongside your real image scraper(s), and add ``sphinx_autocodelink``
    to ``extensions``:

    .. code-block:: python

        sphinx_gallery_conf = {
            'image_scrapers': (AutoCodeLinkScraper(), 'matplotlib'),
        }

    Resolves identifiers in an example's own top-level (module) scope, plus --
    on Python 3.12+, via :func:`reset_autocodelink` -- everything its own
    helper functions' scopes and call sites resolve. Pass ``trace=False`` to
    record the top-level scope only.
    """

    def __init__(
        self,
        records_dir: str = DEFAULT_RECORDS_DIR,
        category: str = DEFAULT_GALLERY_CATEGORY,
        trace: bool = True,
    ) -> None:
        """Store the records directory (relative to the Sphinx source directory) and category.

        ``category`` tags every page this scraper records, for grouping in
        ``.. autocodelink-index::`` output; pass ``''`` to leave pages untagged.
        """
        self.records_dir = records_dir
        self.category = category
        self.trace = trace

    def __call__(self, block: Any, block_vars: dict[str, Any], gallery_conf: dict[str, Any]) -> str:
        """Record this block's identifiers. Called by Sphinx-Gallery; returns no image."""
        docname = (
            Path(block_vars['target_file'])
            .relative_to(gallery_conf['src_dir'])
            .with_suffix('')
            .as_posix()
        )
        record_namespace_to_disk(
            directory=Path(gallery_conf['src_dir']) / self.records_dir,
            docname=docname,
            source=block.content,
            namespace=block_vars['example_globals'],
            category=self.category,
            extra=_RECORDER.drain(),
        )
        return ''


def wants_tracing(gallery_conf: dict[str, Any] | None) -> bool:
    """Return whether ``gallery_conf`` has a scraper that asked for traced examples."""
    if not monitoring_available() or not gallery_conf:
        return False
    scrapers = gallery_conf.get('image_scrapers') or ()
    if not isinstance(scrapers, (list, tuple)):
        scrapers = (scrapers,)
    return any(isinstance(s, AutoCodeLinkScraper) and s.trace for s in scrapers)
