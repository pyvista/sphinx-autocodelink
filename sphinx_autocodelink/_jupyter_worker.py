"""Execute ``jupyter-execute`` cell sources in a subprocess and emit records as JSON.

Run as ``python -m sphinx_autocodelink._jupyter_worker`` with a JSON payload on stdin:
``{"filename": str, "cells": [{"source": str, "reset": bool}, ...]}``. Cells run in
order in one shared namespace, reset where ``reset`` is set. Writes
``{"cells": [{"records": [...], "parse_error": str|null, "run_error": [type, msg]|null},
...]}`` to stdout. Executing here keeps cell code from mutating the Sphinx process.
"""

from __future__ import annotations

import doctest
import json
import sys
from typing import Any


def main() -> None:
    """Run the cells from stdin's payload and write their records to stdout."""
    from sphinx_autocodelink import _records_for
    from sphinx_autocodelink import _to_jsonable
    from sphinx_autocodelink import exec_with_local_scopes
    from sphinx_autocodelink import executable_script_from_examples

    payload = json.load(sys.stdin)
    filename = payload['filename']
    namespace: dict[str, Any] = {}
    results = []
    for cell in payload['cells']:
        if cell['reset']:
            namespace = {}
        source = cell['source']
        is_doctest = any(line.strip().startswith('>>>') for line in source.splitlines())
        code = doctest.script_from_examples(source) if is_doctest else source
        to_run = executable_script_from_examples(source) if is_doctest else source
        result: dict[str, Any] = {'records': [], 'parse_error': None, 'run_error': None}
        results.append(result)
        try:
            compiled = compile(to_run, filename, 'exec')
        except SyntaxError as error:
            result['parse_error'] = str(error)
            continue
        recorded = namespace
        try:
            recorded = exec_with_local_scopes(compiled, namespace, filename)
        except Exception as error:  # noqa: BLE001
            # Record what ran before the raise, exactly as in-process execution did.
            result['run_error'] = [type(error).__name__, str(error)]
        result['records'] = [_to_jsonable(r) for r in _records_for(code, recorded)]
    json.dump({'cells': results}, sys.stdout)


if __name__ == '__main__':
    main()
