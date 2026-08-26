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


class Widget:
    """A documented class only ever reached through a helper function's own local."""

    def render(self) -> int:
        """Return a constant."""
        return 4

    def describe(self) -> str:
        """Return a constant."""
        return 'widget'


class Registry:
    """A documented container, so that ``registry[key]`` has a documented item type."""

    def __getitem__(self, key: str) -> Widget:
        """Return a widget for ``key``."""
        return Widget()


#: A module-level registry, for the subscripted-receiver case.
registry = Registry()
