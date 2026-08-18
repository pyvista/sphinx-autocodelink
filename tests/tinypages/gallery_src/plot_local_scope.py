"""
Local scope example
====================

``local_ref`` only ever exists inside ``helper``'s own local scope -- the
same pattern as pyvista's ``anatomical_groups.py`` calling a filter on a
local variable inside a nested function.
"""

import pkg


def helper():
    local_ref = pkg
    local_ref.thing()


helper()
