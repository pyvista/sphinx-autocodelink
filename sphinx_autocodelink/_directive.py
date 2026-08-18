"""The ``.. autocodelink::`` and ``.. autocodelink-index::`` directives."""

from __future__ import annotations

import doctest
from html import escape

from docutils import nodes
from docutils.parsers.rst import Directive

from sphinx_autocodelink import _note_index_doc
from sphinx_autocodelink import record_namespace


class AutoCodeLink(Directive):
    """Execute the content and record its identifiers for dynamic linking.

    Renders the content as a syntax-highlighted code block; produces no
    figures or other output. Doctest-style (``>>>``) content executes with
    prompts stripped; plain code executes as-is.
    """

    has_content = True
    required_arguments = 0
    optional_arguments = 0

    def run(self) -> list[nodes.Node]:
        """Execute the directive's content and return its rendered code block."""
        source = '\n'.join(self.content)
        is_doctest = any(line.strip().startswith('>>>') for line in self.content)
        code = doctest.script_from_examples(source) if is_doctest else source

        env = self.state.document.settings.env
        namespace: dict = {}
        exec(compile(code, f'<{env.docname}>', 'exec'), namespace)  # noqa: S102
        record_namespace(env=env, docname=env.docname, source=code, namespace=namespace)

        node = nodes.literal_block(source, source)
        node['language'] = 'pycon' if is_doctest else 'python'
        return [node]


class AutoCodeLinkIndex(Directive):
    """A backreferences index: every linked name and the pages that use it.

    With no argument, lists every resolved name site-wide. With one
    argument -- a documented dotted name, e.g. ``pkg.thing`` -- lists just
    the pages that reference that name. Actually filled in at
    ``build-finished``, once every page's links are known.
    """

    has_content = False
    required_arguments = 0
    optional_arguments = 1
    final_argument_whitespace = False

    def run(self) -> list[nodes.Node]:
        """Note this page as hosting an index, and emit its placeholder."""
        env = self.state.document.settings.env
        _note_index_doc(env, env.docname)

        name = self.arguments[0] if self.arguments else ''
        raw = f'<div class="sphinx-autocodelink-index" data-name="{escape(name)}"></div>'
        return [nodes.raw('', raw, format='html')]
