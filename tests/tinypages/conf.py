"""Minimal Sphinx config for building the standalone-directive test fixture."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from sphinx_autocodelink.gallery import AutoCodeLinkScraper

extensions = [
    'sphinx.ext.autodoc',
    'sphinx_autocodelink',
    'sphinx_design',
    'sphinx_gallery.gen_gallery',
]
exclude_patterns = ['_build']

autocodelink_autodoc_backrefs = True

sphinx_gallery_conf = {
    'examples_dirs': 'gallery_src',
    'gallery_dirs': 'auto_examples',
    'image_scrapers': (AutoCodeLinkScraper(),),
    'reset_modules': (),
    'plot_gallery': True,
    'parallel': True,
}
