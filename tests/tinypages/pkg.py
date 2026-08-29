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


def anchored_target() -> int:
    """Return a constant, used only from a docstring's own ``Examples`` section."""
    return 9


def sectioned_example() -> int:
    """Return a constant.

    Examples
    --------

    .. autocodelink::

        import pkg
        pkg.anchored_target()

    """
    return 10


def no_examples_here() -> int:
    """Return a constant, documented without any example code."""
    return 11


#: A module-level singleton, documented as data rather than by its class.
state = Widget()
