"""
Local scope example, define/call in separate blocks
=====================================================

Same as ``plot_local_scope.py``, but ``helper`` is *defined* in one
Sphinx-Gallery ``# %%`` block and *called* from a separate, later one -- the
same shape as pyvista's ``anatomical_groups.py``. ``helper``'s own body never
executes as part of the block that defines it, so anything it references
(``local_ref``) can only be resolved once the calling block's own execution
has actually run it.
"""

import pkg


def helper():
    local_ref = pkg
    local_ref.thing()


# %%
# Call the helper from a separate block.

helper()
