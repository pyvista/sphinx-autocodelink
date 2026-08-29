Skipped Statements
==================

Examples
--------

A ``# doctest: +SKIP`` statement is not executed, but its identifiers still link
when the executed part bound their names.

>>> import pkg
>>> pkg.skipped_target()
12
>>> pkg.skipped_target(no_such_keyword=True)  # doctest: +SKIP

.. autocodelink::

    >>> import pkg
    >>> pkg.skipped_target()
    12
    >>> import module_that_does_not_exist  # doctest: +SKIP
