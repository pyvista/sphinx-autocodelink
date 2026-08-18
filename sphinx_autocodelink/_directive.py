"""The ``.. autocodelink::`` and ``.. autocodelink-index::`` directives."""

from __future__ import annotations

import doctest
from html import escape
import json
from typing import TYPE_CHECKING
from typing import ClassVar

from docutils import nodes
from docutils.parsers.rst import Directive
from docutils.parsers.rst import directives

from sphinx_autocodelink import _note_index_doc
from sphinx_autocodelink import exec_with_local_scopes
from sphinx_autocodelink import record_namespace

if TYPE_CHECKING:
    from collections.abc import Callable


class AutoCodeLink(Directive):
    """Execute the content and record its identifiers for dynamic linking.

    Renders the content as a syntax-highlighted code block; produces no
    figures or other output. Doctest-style (``>>>``) content executes with
    prompts stripped; plain code executes as-is.

    :category: tags this page's records for grouping in
        ``.. autocodelink-index::`` output (e.g. ``:category: Tutorials``).
        Untagged pages display under a generic "Documentation" bucket when grouped.
    """

    has_content = True
    required_arguments = 0
    optional_arguments = 0
    option_spec: ClassVar[dict[str, Callable[[str], object]]] = {'category': directives.unchanged}

    def run(self) -> list[nodes.Node]:
        """Execute the directive's content and return its rendered code block."""
        source = '\n'.join(self.content)
        is_doctest = any(line.strip().startswith('>>>') for line in self.content)
        code = doctest.script_from_examples(source) if is_doctest else source

        env = self.state.document.settings.env
        filename = f'<{env.docname}>'
        namespace = exec_with_local_scopes(compile(code, filename, 'exec'), {}, filename)
        record_namespace(
            env=env,
            docname=env.docname,
            source=code,
            namespace=namespace,
            category=self.options.get('category', ''),
            state=self.state,
        )

        node = nodes.literal_block(source, source)
        node['language'] = 'pycon' if is_doctest else 'python'
        return [node]


def _group_choice(arg: str) -> str:
    return directives.choice(arg, ('auto', 'always', 'never'))


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

    :group: ``auto`` (default), ``always``, or ``never`` -- whether
        referencing pages are grouped by their recorded category (e.g.
        "Sphinx Gallery" vs "Docstring examples"). ``auto`` groups only
        when more than one category is actually present for a given
        entry; a single category (or none at all) renders as today's
        flat list either way.

    :no-titles: shows each referencing page's own docname instead of its
        title. Titles are on by default, read straight from Sphinx's own
        tracked document titles.
    """

    has_content = False
    required_arguments = 0
    optional_arguments = 1
    final_argument_whitespace = False
    option_spec: ClassVar[dict[str, Callable[[str], object]]] = {
        'label': directives.unchanged,
        'hide-empty': directives.flag,
        'group': _group_choice,
        'no-titles': directives.flag,
    }

    def run(self) -> list[nodes.Node]:
        """Note this page as hosting an index, and emit its placeholder."""
        env = self.state.document.settings.env
        _note_index_doc(env, env.docname)

        opts = {
            'name': self.arguments[0] if self.arguments else '',
            'hide_empty': 'hide-empty' in self.options,
            'group': self.options.get('group', 'auto'),
            'titles': 'no-titles' not in self.options,
        }
        raw = (
            f'<div class="sphinx-autocodelink-index" data-opts="{escape(json.dumps(opts))}"></div>'
        )
        placeholder = nodes.raw('', raw, format='html')

        label = self.options.get('label', '')
        if not label:
            return [placeholder]

        section = nodes.section(classes=['sphinx-autocodelink-backrefs'])
        section['ids'] = [nodes.make_id(f'autocodelink-backrefs-{opts["name"] or "index"}')]
        section += nodes.title(label, label)
        section += placeholder
        self.state.document.note_implicit_target(section, section)
        return [section]
