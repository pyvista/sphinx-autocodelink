"""The ``.. autocodelink::`` and ``.. autocodelink-index::`` directives."""

from __future__ import annotations

import doctest
from html import escape
from typing import TYPE_CHECKING
from typing import ClassVar

from docutils import nodes
from docutils.parsers.rst import Directive
from docutils.parsers.rst import directives

from sphinx_autocodelink import _note_index_doc
from sphinx_autocodelink import record_namespace

if TYPE_CHECKING:
    from collections.abc import Callable


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

    :label: wraps the list in a real section with this title, instead of
        rendering inline -- so e.g. a Sphinx setup that hoists docstring
        sections to page level for its own "on this page" navigation picks
        this one up identically. :hide-empty: then omits the whole
        section -- title included -- instead of "No references found."
        when there's nothing to show; only meaningful alongside :label:.
    """

    has_content = False
    required_arguments = 0
    optional_arguments = 1
    final_argument_whitespace = False
    option_spec: ClassVar[dict[str, Callable[[str], object]]] = {
        'label': directives.unchanged,
        'hide-empty': directives.flag,
    }

    def run(self) -> list[nodes.Node]:
        """Note this page as hosting an index, and emit its placeholder."""
        env = self.state.document.settings.env
        _note_index_doc(env, env.docname)

        name = self.arguments[0] if self.arguments else ''
        hide_empty = 'hide-empty' in self.options
        raw = (
            f'<div class="sphinx-autocodelink-index" data-name="{escape(name)}" '
            f'data-hide-empty="{"1" if hide_empty else ""}"></div>'
        )
        placeholder = nodes.raw('', raw, format='html')

        label = self.options.get('label', '')
        if not label:
            return [placeholder]

        section = nodes.section(classes=['sphinx-autocodelink-backrefs'])
        section['ids'] = [nodes.make_id(f'autocodelink-backrefs-{name or "index"}')]
        section += nodes.title(label, label)
        section += placeholder
        self.state.document.note_implicit_target(section, section)
        return [section]
