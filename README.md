# sphinx-autocodelink

Automatically add links to code blocks in Sphinx documentation.

This extension is similar to [sphinx-codeautolink](https://github.com/felix-hilden/sphinx-codeautolink),
except it uses dynamic analysis to resolve links instead of static analysis.
The dynamic analysis is based on how [Sphinx-Gallery](https://sphinx-gallery.github.io) resolves links
for its `'reference_url'` configuration option.

## Installation

```bash
pip install sphinx-autocodelink
```

Add the extension to your Sphinx `conf.py`:

```python
extensions = [
    ...,
    'sphinx_autocodelink',
]
```

## Usage

Links only appear on code you actually run through one of the two mechanisms below. Each is opt-in
by use: nothing happens unless you write the directive, or add the scraper -- there's no
`conf.py` switch to flip.

### The `autocodelink` directive

Write it wherever you want a code block executed and linked. It only affects that one block:

```rst
.. autocodelink::

   import pkg
   pkg.thing()
```

No figure or other output is produced, just a syntax-highlighted, linked code block. Doctest-style
content (`>>>`) also works, with prompts stripped before execution.

### Sphinx-Gallery

Sphinx-Gallery already executes your example scripts; this hooks into that execution instead of
running anything itself. Add `AutoCodeLinkScraper` alongside your real image scraper(s):

```python
from sphinx_autocodelink.gallery import AutoCodeLinkScraper

sphinx_gallery_conf = {
    'image_scrapers': (AutoCodeLinkScraper(), 'matplotlib'),
}
```

Sphinx-Gallery's own `parallel=True` mode runs each example in a separate worker process, bypassing
Sphinx's usual mechanism for merging data back into the main build. `AutoCodeLinkScraper` writes its
records to disk instead, so they survive regardless.

**Drop any module from `sphinx_gallery_conf['reference_url']` once you add `AutoCodeLinkScraper` for
it.** `reference_url` is Sphinx-Gallery's own, older code-linking mechanism, and it isn't aware of
this extension: both wrap the same `<span>` code identifiers, and Sphinx-Gallery's own embedder
doesn't check whether a match is already inside a link. Leaving `reference_url` configured for the
same module produces broken, nested `<a>` tags:

```html
<a class="sphinx-autocodelink-a" href="..."><a href="..." class="sphx-glr-backref-...">...
```

Use `intersphinx_mapping` instead, which this extension already reads -- it covers every page, not
just gallery pages, and Sphinx-Gallery's own `reference_url = None` (local resolution) is redundant
with it anyway.

### Backreferences index

`.. autocodelink-index::` lists every linked name and the pages that reference it, filled in once
the whole site's links are known:

```rst
.. autocodelink-index::
```

Pass a documented dotted name to show just its own references -- handy on that name's own API page:

```rst
.. autocodelink-index:: pkg.thing
```

### Library use

A consumer that already executes example code for its own purposes (e.g. to render a figure) can
call `record_namespace()` directly with the resulting namespace, then call `sphinx_autocodelink.setup()`
from its own `setup(app)` to wire up link embedding.

## Development

```bash
uv sync --group dev
uv run pytest
uv run pre-commit run --all-files
```
