# sphinx-autocodelink

Turn the identifiers in your documentation's code blocks into links to the API docs they
refer to.

This extension is similar to
[sphinx-codeautolink](https://github.com/felix-hilden/sphinx-codeautolink), except it uses
dynamic analysis to resolve links instead of static analysis: it runs your code and asks the
resulting objects what they are, rather than inferring their types from the source. The
dynamic analysis is based on how [Sphinx-Gallery](https://sphinx-gallery.github.io) resolves
links for its `'reference_url'` configuration option.

Running the code is what lets a link land on the right target when the type is written
nowhere — a chained call, a subscript, a variable local to a helper function.

## Quick start

Install it:

```bash
pip install sphinx-autocodelink
```

Add it to `conf.py`:

```python
extensions = [
    ...,
    'sphinx_autocodelink',
]
```

Then point it at the code you want linked. Nothing is linked until you do, so pick whichever
row matches where your code already lives:

| Your code is in                        | Add this                                  |
| -------------------------------------- | ----------------------------------------- |
| Sphinx-Gallery examples                | `AutoCodeLinkScraper` (below)             |
| blocks you mark up yourself            | `.. autocodelink::` (below)               |
| `>>>` doctest blocks, anywhere         | `autocodelink_doctest_blocks = True`      |
| an extension that already runs code    | `record_namespace()` (below)              |

More than one is fine — they can all be on at once.

### Sphinx-Gallery

Sphinx-Gallery already runs your example scripts, so this rides along with that. Add
`AutoCodeLinkScraper` next to your real image scraper(s):

```python
from sphinx_autocodelink.gallery import AutoCodeLinkScraper

sphinx_gallery_conf = {
    'image_scrapers': (AutoCodeLinkScraper(), ...),  # ... = your other scraper(s), if any
}
```

That's everything. Examples are linked, `parallel=True` and all.

### The `autocodelink` directive

Write it wherever you want a block executed and linked. It affects only that block:

```rst
.. autocodelink::

   import pkg
   pkg.thing()
```

You get a syntax-highlighted, linked code block and nothing else — no figure, no output.
Doctest-style (`>>>`) content works too, with the prompts stripped before it runs.

### Doctest blocks

```python
autocodelink_doctest_blocks = True
```

Every bare `>>>` block in your docs — a docstring's Examples section, a hand-written page,
anywhere — is executed and linked, with no markup on any of them.

This one is worth a moment's thought before you switch it on, because it runs code nobody
marked as runnable, including in docstrings `autodoc` pulls in from your dependencies. A block
that fails is skipped with a build warning rather than failing the build, but it has already
run by then. Each block gets a fresh namespace, so a later block can't see an earlier one's
variables.

### Your own extension

If your extension already executes code (to render a figure, say), hand the resulting namespace
over and skip everything above:

```python
from sphinx_autocodelink import record_namespace

record_namespace(env=env, docname=env.docname, source=code, namespace=ns, state=self.state)
```

Then call `app.setup_extension('sphinx_autocodelink')` from your own `setup(app)`.
[pyvista's `pyvista-plot` directive](https://github.com/pyvista/pyvista/blob/main/pyvista/ext/plot_directive.py)
does exactly this.

Only the top-level namespace of code you execute yourself is resolvable by default. Use
`exec_with_local_scopes()` in place of `exec()` to also resolve names that exist only inside
the script's own helper functions:

```python
from sphinx_autocodelink import exec_with_local_scopes

namespace = exec_with_local_scopes(compile(code, filename, 'exec'), {}, filename)
```

It runs `code` exactly as `exec(code, namespace)` would, and returns a namespace with the
script's own calls' locals merged in underneath. Merging is flat, so a local in one call can
shadow a global, or another call's local, of the same name — which resolves to the *wrong*
link, not to no link.

## "Used In" backreferences

`.. autocodelink-index::` lists the pages that use each linked name:

```rst
.. autocodelink-index::
```

Pass a documented dotted name for just that one name's references — useful on its own API page:

```rst
.. autocodelink-index:: pkg.thing
   :label: Used In
   :hide-empty:
```

To get that on every documented object automatically, without writing it anywhere:

```python
autocodelink_autodoc_backrefs = True
```

This needs `sphinx.ext.autodoc` loaded, directly or through something that depends on it such
as numpydoc. Without it nothing is appended and no warning is raised. Modules are skipped.

A page is listed only if it actually *uses* the name — a call, or an attribute read such as a
`@property` or an enum member. A bare mention (a type hint, an `isinstance` check) still gets
its own link in the code block, but doesn't earn a "Used In" entry.

A "Used In" entry links straight to the section holding the code, so it lands on the usage
rather than the top of the page. A `>>>` block is recorded with its own section, so a name
recorded from two of them links to the later one. A directive calling `record_namespace()`
from inside a docstring has no section to name at the time -- autodoc's content is still a
detached subtree -- so its anchor is worked out from the finished page instead; if that page's
code sits in more than one section, there is no telling which a record came from and it keeps
a plain page link. How many objects the page documents doesn't matter, only where its code
lives. This needs the section to be a real, linkable one: numpydoc renders `Examples` as a `.. rubric::` by default, which
carries no anchor, so the link falls back to the enclosing section — the page itself. Projects
that turn those rubrics into real headings get the deeper link for free.

### Categories

Every recording is tagged with where it came from, and the index can group by that tag:
`'Sphinx Gallery'` for the scraper, `'Docstring Examples'` for anything recorded inside an
object's own description, `'Documentation'` for everything else. `.. autocodelink::`,
`record_namespace()` and `AutoCodeLinkScraper` all take a `category` of your own choosing
instead.

Grouping shows a subheading for every category present, even just one. `:no-group:` renders a
flat list instead.

Categories render alphabetically by their displayed label. Rename the labels, reorder the
groups, or both:

```python
autocodelink_category_labels = {'Sphinx Gallery': 'Gallery Examples'}
autocodelink_category_order = ['Docstring Examples', 'Documentation', 'Sphinx Gallery']
```

`autocodelink_category_order` lists *category strings*, not renamed labels, and only the ones
your project actually produces. A category you leave off still renders, alphabetically at the
end, with a build warning naming it.

## Configuration

| `conf.py` value                    | Default                    | Does                                                                        |
| ---------------------------------- | -------------------------- | --------------------------------------------------------------------------- |
| `autocodelink_autodoc_backrefs`    | `False`                    | Append a hidden-if-empty "Used In" section to every documented object but modules |
| `autocodelink_doctest_blocks`      | `False`                    | Execute and link every bare `>>>` block site-wide                            |
| `autocodelink_sort`                | `'alphabetical'`           | `'frequency'` ranks each list by how often the page uses the target          |
| `autocodelink_show_usage_count`    | `False`                    | Show each listed page's own count, e.g. `Tutorial page (3 uses)`             |
| `autocodelink_gallery_cards`       | `False`                    | Render gallery entries as thumbnail cards instead of a link list             |
| `autocodelink_category_labels`     | `{}`                       | Rename a category's displayed heading                                        |
| `autocodelink_category_order`      | `()`                       | Order the groups explicitly instead of alphabetically                        |
| `autocodelink_records_dir`         | `'_autocodelink_records'`  | Where Sphinx-Gallery's worker processes leave their records                  |

`.. autocodelink::` takes `:category:`. `.. autocodelink-index::` takes an optional dotted name
plus `:label:`, `:hide-empty:`, `:no-group:` and `:no-titles:`.
`AutoCodeLinkScraper` takes `records_dir`, `category` and `trace`.

Lists longer than 8 entries show the first 5 and tuck the rest behind a "N more" toggle.
`autocodelink_gallery_cards = True` replaces that with a scrolling carousel of Sphinx-Gallery's
own thumbnails, and needs [sphinx-design](https://sphinx-design.readthedocs.io/) alongside
Sphinx-Gallery.

Entries are styled to match what they point at, with no configuration: a docstring example
renders like a `:class:` cross-reference, a gallery example like a `:ref:`, anything else as a
plain link.

## What resolves, and what doesn't

Sphinx-Gallery examples resolve everywhere the example actually ran — including inside its own
helper functions, and through receivers no name can address, like `dataset['label_map']`. This
needs Python 3.12+; below that, an example resolves from its top-level namespace only.
`AutoCodeLinkScraper(trace=False)` turns it off. Sphinx-Gallery's `reset_modules_order` has to
include `'before'` (the default), and you get a build warning if it doesn't.

Two things still don't resolve, both because there's nothing executed to observe: a helper
the example never calls, and a scope left by a raised exception.

One asymmetry worth knowing: the in-source link on a complex receiver's trailing attribute
needs the whole expression on one line. Its "Used In" entry doesn't — that's recorded either
way.

If you also set `sphinx_gallery_conf['reference_url']` for a module this covers, both extensions
will try to link the same identifiers. Nothing breaks — this one skips anything already inside a
link — but Sphinx-Gallery's own, less precise link wins where both apply. Prefer
`intersphinx_mapping`, which this reads already and which covers every page, not just gallery
ones.

## Development

```bash
uv sync --group dev
uv run pytest
uv run pre-commit run --all-files
```
