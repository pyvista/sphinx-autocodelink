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
    'image_scrapers': (AutoCodeLinkScraper(), ...),  # ... = your other scraper(s), if any
}
```

Sphinx-Gallery's own `parallel=True` mode runs each example in a separate worker process, bypassing
Sphinx's usual mechanism for merging data back into the main build. `AutoCodeLinkScraper` writes its
records to disk instead, so they survive regardless.

`AutoCodeLinkScraper` also resolves identifiers local to an example's own helper functions by
default (`trace_locals=True`; see [Resolving identifiers local to a helper
function](#resolving-identifiers-local-to-a-helper-function)), by taking over
`sphinx_gallery_conf['show_memory']` -- but only if nothing else has already claimed it for its own
purpose (e.g. real memory profiling). If it has, `AutoCodeLinkScraper` backs off and logs a warning;
compose the two yourself with `sphinx_autocodelink.gallery.trace_call_memory()`:

```python
from sphinx_autocodelink.gallery import trace_call_memory

sphinx_gallery_conf = {
    'show_memory': trace_call_memory(my_own_show_memory),
}
```

If `sphinx_gallery_conf['reference_url']` is also configured for a module `AutoCodeLinkScraper`
covers too, both will try to link the same identifiers. This extension runs its own embedding after
Sphinx-Gallery's, and skips anything already inside a link -- so the two don't produce broken,
nested `<a>` tags, but Sphinx-Gallery's own (usually less precise, since it's static analysis rather
than the real executed object) link wins wherever both would apply. Prefer `intersphinx_mapping`
over `reference_url`, which this extension already reads and which covers every page, not just
gallery pages -- then there's nothing to fall back to it for.

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

Add `:label:` to wrap the list in a real section with that title, instead of rendering inline --
important if anything in your setup (e.g. an "on this page" sidebar built from real headings) needs
a genuine section rather than inline content. Add `:hide-empty:` to omit the whole section, title
included, when there's nothing to show, instead of printing "No references found.":

```rst
.. autocodelink-index:: pkg.thing
   :label: Used in
   :hide-empty:
```

Referencing pages show their real title by default (read straight from Sphinx's own tracked
document titles, so it never drifts from what the page actually says), not their docname. Add
`:no-titles:` to show docnames instead.

Set `autocodelink_autodoc_backrefs = True` to append exactly that -- a hidden-if-empty "Used in"
section -- to every autodoc-documented object's own docstring automatically, via
`autodoc-process-docstring`. Off by default; requires `sphinx.ext.autodoc` (directly, or via
something that depends on it, e.g. numpydoc).

**Grouping by category.** `AutoCodeLinkScraper` tags every page it records `'Sphinx Gallery'` by
default (pass `category=` to change or clear it); `.. autocodelink::` and `record_namespace()` take
an optional `category=`/`:category:` of your own choosing. `.. autocodelink-index::` uses this to
group referencing pages -- but only adaptively: `:group: auto` (the default) groups by category only
when a given entry's references actually span more than one category, otherwise it's today's flat
list either way, so a name referenced from just one place never gets a pointless one-item subheading.
Force it with `:group: always` or `:group: never`. Untagged pages fall under a generic
`'Documentation'` bucket whenever grouping does happen.

Set `autocodelink_category_labels` to rename categories' *displayed* group headings, without
changing the category strings themselves (what `:group:` actually groups by) -- e.g. to drop
implementation detail your readers don't need ("Sphinx Gallery" is a mechanism, not something
a reader needs to know about):

```python
autocodelink_category_labels = {
    'Sphinx Gallery': 'Gallery Examples',
    'Documentation': 'API Reference',
}
```

### Resolving identifiers local to a helper function

Only a script's top-level namespace is resolvable by default -- a root identifier that only ever
exists inside one of the script's own helper functions (a local variable, a parameter) has nothing
to look it up against, even though the code accessing it is right there. Use
`exec_with_local_scopes()` in place of a plain `exec()` to also resolve those:

```python
from sphinx_autocodelink import exec_with_local_scopes

namespace = exec_with_local_scopes(compile(code, filename, 'exec'), {}, filename)
```

It runs `code` exactly as `exec(code, namespace)` would, and returns a namespace with every one of
the script's own function calls' own locals merged in underneath -- at the cost of some precision, a
local name in one call can shadow what globals (or a *different* call) bound under the same name.
Only frames compiled from `filename` are captured, so calls into library internals aren't traced or
merged in.

### Library use

A consumer that already executes example code for its own purposes (e.g. to render a figure) can
skip the directive and `AutoCodeLinkScraper` entirely, and call `record_namespace()` directly with
the resulting namespace. For example, [pyvista's `pyvista-plot` directive](https://github.com/pyvista/pyvista/blob/main/pyvista/ext/plot_directive.py)
already builds its own namespace via `exec(code, ns)` to render a figure; adding autolinking is one
extra call after that:

```python
from sphinx_autocodelink import record_namespace

record_namespace(env=env, docname=env.docname, source=code, namespace=ns)
```

Then, from the consumer's own `setup(app)`, call `app.setup_extension('sphinx_autocodelink')`:

```python
def setup(app):
    app.setup_extension('sphinx_autocodelink')
    app.add_directive('pyvista-plot', PlotDirective)
    ...
```

## Development

```bash
uv sync --group dev
uv run pytest
uv run pre-commit run --all-files
```
