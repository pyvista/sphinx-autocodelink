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

Links only appear on code you actually run through one of the two mechanisms below. Neither one
does anything to code you don't feed it -- there's no site-wide "link everything" mode.

### The `autocodelink` directive

Write it wherever you want a code block executed and linked. It only affects that one block:

```rst
.. autocodelink::

   import pkg
   pkg.thing()
```

No figure or other output is produced, just a syntax-highlighted, linked code block. Doctest-style
content (`>>>`) also works, with prompts stripped before execution.

The directive is registered by default (so `.. autocodelink::` works out of the box), but you can
turn it off entirely -- so it errors as an unknown directive if used -- with `autocodelink_sources`
below.

### Sphinx-Gallery

Sphinx-Gallery already executes your example scripts; this hooks into that execution instead of
running anything itself. Two things are required together:

1. Add `Scraper` alongside your real image scraper(s) in `sphinx_gallery_conf` -- this is what
   makes Sphinx-Gallery hand it each example's namespace as it runs:

   ```python
   from sphinx_autocodelink.gallery import Scraper

   sphinx_gallery_conf = {
       'image_scrapers': (Scraper(), 'matplotlib'),
   }
   ```

2. Keep `'gallery'` in `autocodelink_sources` (it's there by default) -- this is what makes the
   recorded links actually get embedded into the built pages.

Without step 1, nothing is ever recorded. Without step 2, `Scraper` still records, but the records
are never embedded -- useful if you want to turn embedding off without touching `sphinx_gallery_conf`
(e.g. per build variant, via `conf.py`).

Sphinx-Gallery's own `parallel=True` mode runs each example in a separate worker process, bypassing
Sphinx's usual mechanism for merging data back into the main build. `Scraper` writes its records to
disk instead, so they survive regardless.

### `autocodelink_sources`

Controls which of the two mechanisms above are allowed to contribute links, independent of whether
you've actually used them. Defaults to both:

```python
autocodelink_sources = ['directive', 'gallery']  # the default; also accepts just one
```

This only affects this package's own directive and `Scraper`. A third-party extension that calls
`record_namespace()` directly (see below) is unaffected either way.

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
