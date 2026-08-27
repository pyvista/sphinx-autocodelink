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
    """Execute the content and render it as a linked, syntax-highlighted code block.

    Doctest-style (``>>>``) content executes with prompts stripped. ``:category:``
    tags this page's records for grouping in ``.. autocodelink-index::`` output.
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


class AutoCodeLinkIndex(Directive):
    """A backreferences index: every linked name and the pages that use it.

    Takes an optional documented dotted name to index just that one. Options are
    ``:label:``, ``:hide-empty:``, ``:no-group:`` and ``:no-titles:``; see the README.
    Filled in at ``build-finished``, once every page's links are known.
    """

    has_content = False
    required_arguments = 0
    optional_arguments = 1
    final_argument_whitespace = False
    option_spec: ClassVar[dict[str, Callable[[str], object]]] = {
        'label': directives.unchanged,
        'hide-empty': directives.flag,
        'no-group': directives.flag,
        'no-titles': directives.flag,
    }

    def run(self) -> list[nodes.Node]:
        """Note this page as hosting an index, and emit its placeholder."""
        env = self.state.document.settings.env
        _note_index_doc(env, env.docname)

        opts = {
            'name': self.arguments[0] if self.arguments else '',
            'hide_empty': 'hide-empty' in self.options,
            'group': 'no-group' not in self.options,
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
        section['names'].append(nodes.fully_normalize_name(label))
        section += nodes.title(label, label)
        section += placeholder
        self.state.document.note_implicit_target(section, section)
        return [section]
