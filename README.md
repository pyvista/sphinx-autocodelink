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

`sphinx-autocodelink` is library-only by default: something has to execute code and hand it the
resulting namespace. Two built-in sources cover most cases, and both are enabled unless you say
otherwise:

### The `autocodelink` directive

Executes its content and links every identifier it accesses, with no figure or other output:

```rst
.. autocodelink::

   import pkg
   pkg.thing()
```

Doctest-style content (`>>>`) also works, with prompts stripped before execution.

### Sphinx-Gallery

Add `Scraper` alongside your real image scraper(s) in `sphinx_gallery_conf`:

```python
from sphinx_autocodelink.gallery import Scraper

sphinx_gallery_conf = {
    'image_scrapers': (Scraper(), 'matplotlib'),
}
```

Sphinx-Gallery's own `parallel=True` mode runs each example in a separate worker process, bypassing
Sphinx's usual mechanism for merging data back into the main build. `Scraper` writes its records to
disk instead, so they survive regardless.

### Choosing sources

Both sources are on by default; disable either with `autocodelink_sources` in `conf.py`:

```python
autocodelink_sources = ['gallery']  # or ['directive'], or the default ['directive', 'gallery']
```

This only toggles this package's own two sources. A third-party extension that calls
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
