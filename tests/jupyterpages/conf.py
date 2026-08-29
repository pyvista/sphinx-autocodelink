"""Minimal Sphinx config for building the jupyter-execute test fixture."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

extensions = [
    'sphinx.ext.autodoc',
    'sphinx_autocodelink',
    'jupyter_sphinx',
]
exclude_patterns = ['_build']

autocodelink_autodoc_backrefs = True
autocodelink_jupyter_blocks = True

# Defaults, plus a kernel working dir the fixture's own ``pkg`` is importable from.
jupyter_execute_kwargs = {
    'timeout': -1,
    'allow_errors': True,
    'store_widget_state': True,
    'cwd': os.path.dirname(__file__),
}
