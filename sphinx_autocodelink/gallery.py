"""Sphinx-Gallery ``image_scrapers`` integration.

Records identifiers from Sphinx-Gallery's own example execution, without
producing an image. Sphinx-Gallery's own parallel example generation runs in
separate joblib worker processes that never go through Sphinx's own
``env-merge-info``, so records go to disk instead, and get picked back up at
``build-finished`` (see :func:`sphinx_autocodelink.record_namespace_to_disk`).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
import sys
from typing import TYPE_CHECKING
from typing import Any

from sphinx.util import logging

from sphinx_autocodelink import DEFAULT_RECORDS_DIR
from sphinx_autocodelink import record_namespace_to_disk

if TYPE_CHECKING:
    from collections.abc import Callable

    from sphinx.application import Sphinx
    from sphinx.config import Config

_logger = logging.getLogger(__name__)

#: Mailbox from `_call_memory_with_tracing`/`trace_call_memory` to the scraper's own `__call__`,
#: for the one example block Sphinx-Gallery is between executing and scraping right now.
#: Sphinx-Gallery calls them in that exact order for one block before moving to the next, even
#: under `parallel=True` (a separate worker process per example, so no cross-example clash).
_LAST_TRACED_LOCALS: dict[str, Any] = {}


@dataclass
class _PendingExample:
    """One example's blocks and everything seen anywhere in it so far, up to this point.

    A helper function's own body -- defined in one block, only actually *called* from a
    later one -- can only be resolved against a namespace that has everything any of the
    example's blocks bound, not just whichever block happened to trigger that particular
    call. So every block gets re-recorded (see `AutoCodeLinkScraper.__call__`) against
    the fullest namespace seen *so far*, each time a new block comes in for the same
    example -- rather than deferred to some "this example is finished" signal, which
    a joblib worker process (Sphinx-Gallery's `parallel=True`) may never reliably fire
    before the disk records need to already be there.
    """

    directory: Path
    docname: str
    category: str
    blocks: list[str] = field(default_factory=list)
    namespace: dict[str, Any] = field(default_factory=dict)


#: The one example currently being scraped in this process, or ``None`` between examples.
#: Sphinx-Gallery scrapes one example's blocks strictly in order before moving to the next,
#: even under `parallel=True` (a separate worker process per example), so one slot suffices.
_PENDING: _PendingExample | None = None


class AutoCodeLinkScraper:
    """A no-op ``image_scrapers`` entry that records identifiers for linking.

    Add alongside your real image scraper(s), and add ``sphinx_autocodelink``
    to ``extensions``:

    .. code-block:: python

        sphinx_gallery_conf = {
            'image_scrapers': (AutoCodeLinkScraper(), 'matplotlib'),
        }
    """

    def __init__(
        self,
        records_dir: str = DEFAULT_RECORDS_DIR,
        category: str = 'Sphinx Gallery',
        trace_locals: bool = True,
    ) -> None:
        """Store the records directory (relative to the Sphinx source directory) and category.

        ``category`` tags every page this scraper records, for grouping in
        ``.. autocodelink-index::`` output; pass ``''`` to leave pages untagged.

        ``trace_locals`` resolves identifiers local to an example's own helper
        functions too (see :func:`sphinx_autocodelink.exec_with_local_scopes`),
        by taking over ``sphinx_gallery_conf['show_memory']`` -- only if it
        isn't already set to something else. Set to ``False`` to opt out, or
        if you need ``show_memory`` for its own purpose; see
        :func:`trace_call_memory` to use both.
        """
        self.records_dir = records_dir
        self.category = category
        self.trace_locals = trace_locals

    def __call__(self, block: Any, block_vars: dict[str, Any], gallery_conf: dict[str, Any]) -> str:
        """Re-record this example's every block so far, namespace included; no image."""
        docname = (
            Path(block_vars['target_file'])
            .relative_to(gallery_conf['src_dir'])
            .with_suffix('')
            .as_posix()
        )

        global _PENDING
        if _PENDING is None or _PENDING.docname != docname:
            _PENDING = _PendingExample(
                directory=Path(gallery_conf['src_dir']) / self.records_dir,
                docname=docname,
                category=self.category,
            )

        # `example_globals` only grows across an example's own blocks, so re-merging it
        # every call keeps the latest snapshot without needing its own end-of-example signal.
        _PENDING.namespace.update(block_vars['example_globals'])
        if self.trace_locals and _LAST_TRACED_LOCALS:
            _PENDING.namespace.update(_LAST_TRACED_LOCALS)
            _LAST_TRACED_LOCALS.clear()
        _PENDING.blocks.append(block.content)

        target = _PENDING.directory / f'{docname}.json'
        target.unlink(missing_ok=True)
        for source in _PENDING.blocks:
            record_namespace_to_disk(
                directory=_PENDING.directory,
                docname=docname,
                source=source,
                namespace=_PENDING.namespace,
                category=_PENDING.category,
            )
        return ''


def _trace_call(func: Callable[[], Any]) -> tuple[dict[str, Any], Any]:
    """Call ``func()`` under tracing; return every local scope seen, and ``func()``'s result.

    Uses ``sys.setprofile()`` rather than ``sys.settrace()`` -- see
    :func:`sphinx_autocodelink.exec_with_local_scopes` for why.

    Only captures frames from ``func()``'s own top-level module (its first ``'<module>'``
    frame, since ``func`` is opaque and carries no filename to filter on upfront) --
    otherwise every return anywhere in the process, library internals included, gets
    merged in, holding unrelated namespaces (and heavy objects in them) alive too long.
    """
    captured: dict[str, Any] = {}
    old_profile = sys.getprofile()
    module_filename: list[str] = []

    def _profiler(frame: Any, event: str, arg: Any) -> None:
        if old_profile is not None:
            old_profile(frame, event, arg)
        if event == 'call' and frame.f_code.co_name == '<module>' and not module_filename:
            module_filename.append(frame.f_code.co_filename)
        if event == 'return' and module_filename and frame.f_code.co_filename == module_filename[0]:
            captured.update(frame.f_locals)

    sys.setprofile(_profiler)
    try:
        result = func()
    finally:
        sys.setprofile(old_profile)
    return captured, result


def _call_memory_with_tracing(func: Callable[[], Any]) -> tuple[float, Any]:
    """Sphinx-Gallery ``show_memory`` callable: trace ``func()``, stash its locals for scraping."""
    captured, result = _trace_call(func)
    _LAST_TRACED_LOCALS.clear()
    _LAST_TRACED_LOCALS.update(captured)
    return 0.0, result


def trace_call_memory(
    inner: Callable[[Callable[[], Any]], tuple[float, Any]],
) -> Callable[[Callable[[], Any]], tuple[float, Any]]:
    """Wrap your own Sphinx-Gallery ``show_memory`` callable with local-scope tracing.

    Use when ``sphinx_gallery_conf['show_memory']`` is already set to
    something else for its own purpose (e.g. real memory profiling):
    ``AutoCodeLinkScraper``'s own automatic wiring backs off rather than
    overriding it, and logs a warning pointing here.

    .. code-block:: python

        from sphinx_autocodelink.gallery import trace_call_memory

        sphinx_gallery_conf = {
            'show_memory': trace_call_memory(my_own_show_memory),
        }
    """

    def _wrapped(func: Callable[[], Any]) -> tuple[float, Any]:
        captured, (mem_max, result) = _trace_call(lambda: inner(func))
        _LAST_TRACED_LOCALS.clear()
        _LAST_TRACED_LOCALS.update(captured)
        return mem_max, result

    return _wrapped


def _install_gallery_tracing(app: Sphinx, config: Config) -> None:
    """Wrap Sphinx-Gallery's own ``show_memory`` with local-scope tracing, if wanted and free.

    Connected at ``config-inited`` priority 20: after Sphinx-Gallery's own
    ``fill_gallery_conf_defaults`` (priority 10), so ``image_scrapers`` is
    already resolved into real callables to check against, and
    ``show_memory`` reflects whatever the user actually configured.
    """
    gallery_conf = getattr(config, 'sphinx_gallery_conf', None)
    if not gallery_conf:
        return
    scrapers = gallery_conf.get('image_scrapers') or ()
    if not any(isinstance(s, AutoCodeLinkScraper) and s.trace_locals for s in scrapers):
        return
    if gallery_conf.get('show_memory'):
        _logger.warning(
            "sphinx_autocodelink: 'show_memory' is already set in sphinx_gallery_conf, so "
            'AutoCodeLinkScraper local-scope tracing is off for Sphinx-Gallery examples. '
            'Wrap your own show_memory callable with '
            'sphinx_autocodelink.gallery.trace_call_memory() to use both.'
        )
        return
    gallery_conf['show_memory'] = _call_memory_with_tracing
