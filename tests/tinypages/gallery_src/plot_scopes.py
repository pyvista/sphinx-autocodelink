"""
Plotting from a helper
======================

Neither identifier below is resolvable from this example's own top-level
namespace: ``widget`` only ever exists inside ``show``'s own local scope, and
``registry['a']`` is a receiver no dotted name addresses.
"""

import pkg

registry = pkg.registry

# %%
# Define the helper. Nothing in its body runs in this cell, so the scraper for
# this cell sees no local scope at all -- a helper defined in one cell and
# called in another has to resolve when it actually runs.


def show(key):
    """Reach ``pkg.Widget.render`` through a local that never leaves this scope."""
    widget = registry[key]
    return widget.render()


# %%
# Call the helper, a cell later.
print(show('a'))

# %%
# A subscripted receiver: no dotted name addresses ``registry['a']``.
print(registry['a'].describe())
