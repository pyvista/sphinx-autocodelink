Index
=====

.. toctree::

   api
   api_sectioned
   auto_examples/index
   refs
   doctest_page

.. autocodelink::

   import pkg
   pkg.thing()

.. autocodelink::

   import pkg

   def helper():
       # `local_ref` only ever exists inside helper's own local scope -- a plain
       # exec() can't resolve `local_ref.thing`, only exec_with_local_scopes can.
       local_ref = pkg
       local_ref.thing()

   helper()

.. autocodelink::

   import pkg

   @pkg.tag
   def decorated():
       return 0

   decorated()
