"""Sphinx-Gallery ``image_scrapers`` integration.

Records identifiers from Sphinx-Gallery's own example execution, without
producing an image. Sphinx-Gallery's own parallel example generation runs in
separate joblib worker processes that never go through Sphinx's own
``env-merge-info``, so records go to disk instead, and get picked back up at
``build-finished`` (see :func:`sphinx_autocodelink.record_namespace_to_disk`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sphinx_autocodelink import DEFAULT_GALLERY_CATEGORY
from sphinx_autocodelink import DEFAULT_RECORDS_DIR
from sphinx_autocodelink import record_namespace_to_disk


class AutoCodeLinkScraper:
    """A no-op ``image_scrapers`` entry that records identifiers for linking.

    Add alongside your real image scraper(s), and add ``sphinx_autocodelink``
    to ``extensions``:

    .. code-block:: python

        sphinx_gallery_conf = {
            'image_scrapers': (AutoCodeLinkScraper(), 'matplotlib'),
        }

    Only resolves identifiers in an example's own top-level (module) scope --
    a root identifier that only ever exists inside one of the example's own
    helper functions (a local variable, a parameter) isn't resolvable here.
    """

    def __init__(
        self, records_dir: str = DEFAULT_RECORDS_DIR, category: str = DEFAULT_GALLERY_CATEGORY
    ) -> None:
        """Store the records directory (relative to the Sphinx source directory) and category.

        ``category`` tags every page this scraper records, for grouping in
        ``.. autocodelink-index::`` output; pass ``''`` to leave pages untagged.
        """
        self.records_dir = records_dir
        self.category = category

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
        )
        return ''
