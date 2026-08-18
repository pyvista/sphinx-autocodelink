"""A fake documented package, importable by the standalone-directive test fixture."""

from __future__ import annotations


def thing() -> int:
    """Return a constant, documented elsewhere in the fixture site."""
    return 1


def unused() -> int:
    """Return a constant. Never referenced by any example -- for the empty-backrefs case."""
    return 2


def documented_example() -> int:
    """Return a constant.

    .. autocodelink::

        import pkg
        pkg.thing()
    """
    return 3
