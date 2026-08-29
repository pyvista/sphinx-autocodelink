Jupyter Index
=============

.. toctree::

   api

Cells
-----

The first cell is hidden, and binds the ``helper`` the visible cells use:

.. jupyter-execute::
   :hide-code:

   import os
   import sys

   sys.path.insert(0, os.getcwd())
   import pkg

   helper = pkg.Widget()

A doctest-style cell, as the kernel's own IPython accepts:

.. jupyter-execute::

   >>> import pkg
   >>> pkg.thing()

A plain cell using the hidden cell's binding:

.. jupyter-execute::

   helper.render()

A cell that raises, declared with ``:raises:``:

.. jupyter-execute::
   :raises:

   helper.describe(unexpected_arg=1)
