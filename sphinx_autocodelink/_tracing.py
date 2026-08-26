"""Low-overhead, scope-aware execution tracing built on :mod:`sys.monitoring`.

Sphinx-Gallery owns the ``exec()`` of its example scripts, so
:func:`sphinx_autocodelink.exec_with_local_scopes` can't be used there. This
traces that execution from the outside instead, and reports two things back:
every scope (function frame) the example's own code ran in, and every call the
example's own code made, with the real callable that was invoked.

Cost is the reason this is built on :mod:`sys.monitoring` (Python 3.12+) rather
than ``sys.settrace``/``sys.setprofile``: every event this registers is
``DISABLE``-d the first time it fires for a given code location, so a whole
gallery build pays one Python-level callback per distinct code location that
ever runs, not one per call. Nothing observed is retained -- callbacks resolve
what they need to plain strings and drop every reference before returning --
which is what keeps traced example objects (plotters, meshes, anything holding
a native resource) from outliving the example that created them.
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType
    from types import FrameType

#: ``sys.monitoring`` tool ids not reserved for a named tool (0/1/2/5 are the
#: debugger, coverage, profiler and optimizer), most preferred first.
_TOOL_IDS = (3, 4)

#: Name registered against whichever tool id is claimed.
_TOOL_NAME = 'sphinx-autocodelink'


def monitoring_available() -> bool:
    """Return whether this interpreter has :mod:`sys.monitoring` (Python 3.12+)."""
    return hasattr(sys, 'monitoring')


class ScopeTracer:
    """Report the scopes entered, and the calls made, by one traced source file.

    ``on_scope(code, frame)`` is called once per code object of the traced file,
    when a frame running it returns. ``on_call(code, offset,
    func, frame)`` is called once per call site in the traced file, with the
    real callable invoked there and the calling frame. Neither the frame nor
    the callable may be retained past the callback.

    Anything raised by a callback propagates into the code being traced, so
    every callback here is wrapped: the first failure reports through
    ``on_error(exception)`` and permanently silences this tracer for the rest
    of the process.
    """

    def __init__(
        self,
        on_scope: Callable[[CodeType, FrameType], None],
        on_call: Callable[[CodeType, int, Any, FrameType], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        """Store the callbacks; claim no tool id until :meth:`start`."""
        self._on_scope = on_scope
        self._on_call = on_call
        self._on_error = on_error
        self._tool_id: int | None = None
        self._basename: str | None = None
        self._filename: str | None = None
        self._instrumented: list[CodeType] = []
        self._broken = False
        self._in_callback = False

    @property
    def active(self) -> bool:
        """Return whether this tracer is currently instrumenting a file."""
        return self._basename is not None

    def start(self, basename: str) -> bool:
        """Begin tracing the next file whose basename is ``basename``; return success.

        Sphinx-Gallery's own ``reset_modules`` hook only reports an example's
        basename, so the full path is pinned from the first code object seen
        matching it, and everything else is filtered out from then on.
        """
        if self._broken or not monitoring_available():
            return False
        self.stop()
        if self._tool_id is None and not self._claim_tool_id():
            return False
        self._basename = basename
        self._filename = None
        monitoring = sys.monitoring
        events = monitoring.events
        monitoring.register_callback(self._tool_id, events.PY_START, self._start_event)
        monitoring.register_callback(self._tool_id, events.PY_RETURN, self._return_event)
        monitoring.register_callback(self._tool_id, events.CALL, self._call_event)
        monitoring.set_events(self._tool_id, events.PY_START)
        return True

    def stop(self) -> None:
        """Stop tracing, clearing every local event this tracer installed."""
        self._basename = None
        self._filename = None
        self._in_callback = False
        if self._tool_id is None:
            return
        monitoring = sys.monitoring
        # Teardown must never fail a build, whatever state monitoring is in.
        with contextlib.suppress(Exception):
            monitoring.set_events(self._tool_id, 0)
            for code in self._instrumented:
                monitoring.set_local_events(self._tool_id, code, 0)
        self._instrumented.clear()

    def close(self) -> None:
        """Stop tracing and release the ``sys.monitoring`` tool id this claimed.

        A build's tracer holds its tool id for the whole build -- the DISABLE-d
        state that makes tracing cheap lives with the id, and re-claiming it per
        example would throw that away. Only a caller that builds more than one
        tracer in a process (i.e. a test) needs this.
        """
        self.stop()
        if self._tool_id is not None:
            with contextlib.suppress(Exception):
                sys.monitoring.free_tool_id(self._tool_id)
            self._tool_id = None

    def _claim_tool_id(self) -> bool:
        """Claim the first free tool id, or return ``False`` if all are taken."""
        for tool_id in _TOOL_IDS:
            try:
                sys.monitoring.use_tool_id(tool_id, _TOOL_NAME)
            except ValueError:
                continue
            self._tool_id = tool_id
            return True
        return False

    def _fail(self, error: BaseException) -> Any:
        """Silence this tracer permanently and report ``error`` once."""
        if not self._broken:
            self._broken = True
            # Reporting must not raise back into the code being traced.
            with contextlib.suppress(Exception):
                self._on_error(error)
        self.stop()
        return sys.monitoring.DISABLE

    def _is_traced(self, code: CodeType) -> bool:
        """Return whether ``code`` belongs to the file being traced, pinning it if new."""
        if self._filename is not None:
            return code.co_filename == self._filename
        if os.path.basename(code.co_filename) != self._basename:
            return False
        self._filename = code.co_filename
        return True

    def _start_event(self, code: CodeType, instruction_offset: int) -> Any:
        """Instrument a traced file's code object on first entry; ignore everything else.

        ``DISABLE`` either way: a foreign code object never costs a second
        callback, and a traced one has its per-code events installed already.
        """
        disable = sys.monitoring.DISABLE
        try:
            if not self._ready() or not self._is_traced(code):
                return disable
            events = sys.monitoring.events
            # PY_RETURN only: PY_UNWIND isn't a valid local event, so a scope left by
            # a raised exception simply goes unrecorded, like the failing block itself.
            sys.monitoring.set_local_events(self._tool_id, code, events.CALL | events.PY_RETURN)
            self._instrumented.append(code)
        except Exception as error:  # noqa: BLE001
            return self._fail(error)
        return disable

    def _ready(self) -> bool:
        """Return whether a callback should do any work right now.

        Resolving what an event reports runs arbitrary attribute access on the
        traced objects, which can call back into traced code; the guard keeps
        that from re-entering a callback that's already running.
        """
        return not self._broken and self._basename is not None and not self._in_callback

    def _return_event(self, code: CodeType, instruction_offset: int, retval: Any) -> Any:
        """Report the returning frame's own scope, once per code object."""
        try:
            if self._ready():
                frame = sys._getframe(1)  # the monitored frame itself
                self._in_callback = True
                try:
                    if frame.f_code is code:
                        self._on_scope(code, frame)
                finally:
                    self._in_callback = False
                    del frame
        except Exception as error:  # noqa: BLE001
            return self._fail(error)
        return sys.monitoring.DISABLE

    def _call_event(self, code: CodeType, instruction_offset: int, func: Any, arg0: Any) -> Any:
        """Report one call site's real callable, once per call site."""
        try:
            if self._ready():
                frame = sys._getframe(1)  # the calling frame itself
                self._in_callback = True
                try:
                    if frame.f_code is code:
                        self._on_call(code, instruction_offset, func, frame)
                finally:
                    self._in_callback = False
                    del frame
        except Exception as error:  # noqa: BLE001
            return self._fail(error)
        return sys.monitoring.DISABLE
