"""Unit tests for the sys.monitoring tracer and the records it feeds."""

from __future__ import annotations

import ast
import functools
import sys

import pytest

import sphinx_autocodelink as autolink
from sphinx_autocodelink import _scope_records as scope_records
from sphinx_autocodelink import _tracing

needs_monitoring = pytest.mark.skipif(
    not _tracing.monitoring_available(), reason='sys.monitoring added in Python 3.12'
)


class Widget:
    """A resolution target with a method, defined where the tests can point at it."""

    def render(self):
        """Return a constant."""
        return 'rendered'


class Registry:
    """A resolution target whose items are ``Widget``s, for subscripted receivers."""

    def __getitem__(self, key):
        """Return a widget."""
        return Widget()


def _trace(tmp_path, source, *, name='traced_example.py', namespace=None):
    """Execute ``source`` from a real file under a tracer; return (recorder, errors)."""
    path = tmp_path / name
    path.write_text(source)
    errors = []
    recorder = scope_records.ExampleRecorder()
    tracer = _tracing.ScopeTracer(recorder.on_scope, recorder.on_call, errors.append)
    scope_records.clear_caches()
    assert tracer.start(path.name)
    try:
        exec(compile(source, str(path), 'exec'), dict(namespace or {}))  # noqa: S102
    finally:
        tracer.close()
        scope_records.clear_caches()
    return recorder, errors


def _accessed(records):
    """Return every ``_Candidate``'s accessed name."""
    return {r.accessed for r in records if isinstance(r, autolink._Candidate)}


def _exprs(records):
    """Return every ``_ExprCandidate``'s expression and candidates."""
    return {r.expr: r.candidates for r in records if isinstance(r, autolink._ExprCandidate)}


@needs_monitoring
def test_resolves_a_local_that_never_leaves_its_helpers_scope(tmp_path):
    source = "def show(key):\n    widget = registry[key]\n    return widget.render()\n\nshow('a')\n"
    recorder, errors = _trace(tmp_path, source, namespace={'registry': Registry()})
    assert errors == []
    records = recorder.drain()
    assert 'widget.render' in _accessed(records)
    [candidate] = [r for r in records if getattr(r, 'accessed', None) == 'widget.render']
    assert candidate.candidates[0].endswith('Widget.render')


@needs_monitoring
def test_resolves_a_receiver_no_dotted_name_addresses(tmp_path):
    source = "registry['a'].render()\n"
    recorder, errors = _trace(tmp_path, source, namespace={'registry': Registry()})
    assert errors == []
    exprs = _exprs(recorder.drain())
    assert "registry['a'].render" in exprs
    assert exprs["registry['a'].render"][0].endswith('Widget.render')


@needs_monitoring
def test_a_dotted_call_the_namespace_already_resolves_is_not_recorded_twice(tmp_path):
    # `registry.__getitem__` is reached as `registry[...]`, but `widget.render()` is a
    # plain dotted name the scope recording already covers -- exactly one record, not two.
    source = 'def show(key):\n    widget = registry[key]\n    return widget.render()\n\nshow("a")\n'
    recorder, _ = _trace(tmp_path, source, namespace={'registry': Registry()})
    records = recorder.drain()
    renders = [r for r in records if getattr(r, 'accessed', None) == 'widget.render']
    assert len(renders) == 1


@needs_monitoring
def test_nested_definitions_are_left_to_their_own_scope(tmp_path):
    source = (
        'def outer(reg):\n'
        '    def inner(key):\n'
        '        inner_widget = reg[key]\n'
        '        return inner_widget.render()\n'
        '\n'
        '    outer_widget = reg["a"]\n'
        '    outer_widget.render()\n'
        '    return inner("b")\n'
        '\n'
        'outer(registry)\n'
    )
    recorder, _ = _trace(tmp_path, source, namespace={'registry': Registry()})
    accessed = _accessed(recorder.drain())
    # both scopes resolve their own local, and neither resolves the other's
    assert {'outer_widget.render', 'inner_widget.render'} <= accessed


@needs_monitoring
def test_builtins_are_not_linked(tmp_path):
    recorder, _ = _trace(tmp_path, "print('x')\nlen('abc')\n")
    assert recorder.drain() == []


@needs_monitoring
def test_module_scope_is_left_to_the_scrapers_own_block_recording(tmp_path):
    # Only the call record shows up: the module frame's own scope is skipped.
    recorder, _ = _trace(tmp_path, "registry['a'].render()\n", namespace={'registry': Registry()})
    records = recorder.drain()
    assert all(isinstance(r, autolink._ExprCandidate) for r in records)


@needs_monitoring
def test_foreign_code_is_filtered_out(tmp_path):
    # `Registry.__getitem__` and `Widget.render` run inside this test module, not the
    # traced file -- their own scopes must never be recorded.
    recorder, _ = _trace(tmp_path, "registry['a'].render()\n", namespace={'registry': Registry()})
    assert not any(getattr(r, 'accessed', '').startswith('self') for r in recorder.drain())


@needs_monitoring
def test_a_callback_failure_is_reported_once_and_gives_up(tmp_path):
    def boom(*args):
        msg = 'callback exploded'
        raise RuntimeError(msg)

    errors = []
    path = tmp_path / 'boom_example.py'
    source = 'def helper():\n    return 1\n\nhelper()\nhelper()\n'
    path.write_text(source)
    tracer = _tracing.ScopeTracer(boom, boom, errors.append)
    assert tracer.start(path.name)
    try:
        exec(compile(source, str(path), 'exec'), {})  # noqa: S102
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        # given up for good: a later start is refused rather than retried
        assert tracer.start(path.name) is False
    finally:
        tracer.close()


@needs_monitoring
def test_a_failing_error_reporter_is_swallowed(tmp_path):
    def boom(*args):
        raise RuntimeError

    path = tmp_path / 'boom_reporter.py'
    source = 'def helper():\n    return 1\n\nhelper()\n'
    path.write_text(source)
    tracer = _tracing.ScopeTracer(boom, boom, boom)
    assert tracer.start(path.name)
    try:
        exec(compile(source, str(path), 'exec'), {})  # noqa: S102
    finally:
        tracer.close()


def test_start_is_refused_without_monitoring(tmp_path, monkeypatch):
    monkeypatch.setattr(_tracing, 'monitoring_available', lambda: False)
    tracer = _tracing.ScopeTracer(lambda *a: None, lambda *a: None, lambda e: None)
    assert tracer.start('anything.py') is False
    assert tracer.active is False
    tracer.close()


@needs_monitoring
def test_start_is_refused_when_every_tool_id_is_taken(monkeypatch):
    def taken(tool_id, name):
        raise ValueError

    monkeypatch.setattr(sys.monitoring, 'use_tool_id', taken)
    tracer = _tracing.ScopeTracer(lambda *a: None, lambda *a: None, lambda e: None)
    assert tracer.start('anything.py') is False


def test_scope_records_skips_an_unreadable_file(tmp_path):
    scope_records.clear_caches()
    code = compile('def f():\n    pass\n', str(tmp_path / 'missing.py'), 'exec')
    assert scope_records.scope_records(code, sys._getframe()) == []
    assert scope_records._index_for(str(tmp_path / 'missing.py')) is None


def test_scope_records_skips_a_code_object_the_source_does_not_describe(tmp_path):
    path = tmp_path / 'mismatch.py'
    path.write_text('x = 1\n')
    scope_records.clear_caches()
    code = compile('def f():\n    pass\n', str(path), 'exec').co_consts[0]
    assert scope_records.scope_records(code, sys._getframe()) == []


def test_scope_source_of_a_lambda():
    node = ast.parse('f = lambda x: x.render()').body[0].value
    assert scope_records._scope_source(node) == 'x.render()'


def test_scope_source_of_something_that_is_not_a_scope():
    assert scope_records._scope_source(ast.parse('x = 1').body[0]) is None


def test_scope_source_prunes_every_kind_of_nested_definition():
    node = ast.parse(
        'def outer():\n'
        '    def inner():\n'
        '        pass\n'
        '    async def ainner():\n'
        '        pass\n'
        '    class Inner:\n'
        '        pass\n'
        '    f = lambda: 1\n'
        '    return 1\n'
    ).body[0]
    source = scope_records._scope_source(node)
    assert 'inner' not in source
    assert 'Inner' not in source
    assert 'lambda' not in source
    assert 'return 1' in source


def test_scope_source_that_cannot_be_unparsed(monkeypatch):
    def boom(*args):
        raise RecursionError

    monkeypatch.setattr(scope_records.ast, 'unparse', boom)
    node = ast.parse('def f():\n    pass\n').body[0]
    assert scope_records._scope_source(node) is None


def _call_offset(code, node):
    """Return the instruction offset whose reported position is ``node``'s own."""
    wanted = (node.lineno, node.end_lineno, node.col_offset, node.end_col_offset)
    return 2 * next(i for i, position in enumerate(code.co_positions()) if position == wanted)


@needs_monitoring
def test_call_node_falls_back_to_a_line_with_exactly_one_call(tmp_path):
    path = tmp_path / 'fallback.py'
    path.write_text('def f(reg):\n    return reg["a"].render()\n')
    scope_records.clear_caches()
    code = compile(path.read_text(), str(path), 'exec').co_consts[0]
    index = scope_records._index_for(str(path))
    [node] = index.calls_by_line[2]
    offset = _call_offset(code, node)
    assert scope_records._call_node(code, offset)[1] is node
    # without the exact position, one call on the line is still unambiguous
    index.calls.clear()
    assert scope_records._call_node(code, offset)[1] is node


@needs_monitoring
def test_call_node_gives_up_on_a_line_with_more_than_one_call(tmp_path):
    path = tmp_path / 'ambiguous.py'
    path.write_text('def f(reg):\n    return reg.one() + reg.two()\n')
    scope_records.clear_caches()
    code = compile(path.read_text(), str(path), 'exec').co_consts[0]
    index = scope_records._index_for(str(path))
    offset = _call_offset(code, index.calls_by_line[2][0])
    index.calls.clear()
    assert scope_records._call_node(code, offset) is None


@needs_monitoring
def test_call_node_ignores_an_offset_past_the_end(tmp_path):
    path = tmp_path / 'past_end.py'
    path.write_text('def f(reg):\n    return reg["a"].render()\n')
    scope_records.clear_caches()
    code = compile(path.read_text(), str(path), 'exec').co_consts[0]
    assert scope_records._call_node(code, 10**6) is None


@needs_monitoring
def test_call_records_ignores_an_offset_with_no_call(tmp_path):
    path = tmp_path / 'nocall.py'
    path.write_text('x = 1\n')
    scope_records.clear_caches()
    code = compile(path.read_text(), str(path), 'exec')
    assert scope_records.call_records(code, 0, Widget().render, sys._getframe()) == []


def test_recorder_stops_at_the_record_cap():
    recorder = scope_records.ExampleRecorder()
    recorder.records = ['x'] * scope_records.MAX_RECORDS
    recorder._add(['one more'])
    assert len(recorder.records) == scope_records.MAX_RECORDS


def test_clear_caches_empties_both_caches(tmp_path):
    path = tmp_path / 'cached.py'
    path.write_text('x = 1\n')
    scope_records._index_for(str(path))
    assert scope_records._INDEXES
    scope_records.clear_caches()
    assert not scope_records._INDEXES
    assert not scope_records._POSITIONS


# scope_records()/call_records() reached through a real frame rather than through
# monitoring, for the same reason the callback tests above are: coverage can't see
# inside a sys.monitoring callback.


def make_widget() -> Widget:
    """Return a widget, annotated so a chain off the call resolves without tracing."""
    return Widget()


def _frame_from(tmp_path, name, body, *args):
    """Run ``show`` from a real file built out of ``body``; return the frame it ran in."""
    source = f'import sys\n\n\ndef show(reg):\n{body}    return sys._getframe()\n'
    path = tmp_path / name
    path.write_text(source)
    namespace = {'make_widget': make_widget}
    exec(compile(source, str(path), 'exec'), namespace)  # noqa: S102
    scope_records.clear_caches()
    return namespace['show'](*args)


def test_scope_records_resolves_against_the_frames_own_locals(tmp_path):
    frame = _frame_from(
        tmp_path, 'direct.py', "    widget = reg['a']\n    widget.render()\n", Registry()
    )
    records = scope_records.scope_records(frame.f_code, frame)
    assert 'widget.render' in _accessed(records)


def test_scope_records_skips_the_module_scope(tmp_path):
    frame = _frame_from(tmp_path, 'modscope.py', '    pass\n', Registry())
    module_code = compile('x = 1', frame.f_code.co_filename, 'exec')
    assert scope_records.scope_records(module_code, frame) == []


@needs_monitoring
def test_call_records_of_a_receiver_no_dotted_name_addresses(tmp_path):
    frame = _frame_from(tmp_path, 'exprcall.py', "    reg['a'].render()\n", Registry())
    index = scope_records._index_for(frame.f_code.co_filename)
    [node] = index.calls_by_line[5]
    records = scope_records.call_records(
        frame.f_code, _call_offset(frame.f_code, node), Widget().render, frame
    )
    assert _exprs(records) == {"reg['a'].render": records[0].candidates}


@needs_monitoring
def test_call_records_skips_what_the_frames_own_names_already_resolve(tmp_path):
    frame = _frame_from(
        tmp_path, 'dotted.py', "    widget = reg['a']\n    widget.render()\n", Registry()
    )
    index = scope_records._index_for(frame.f_code.co_filename)
    [node] = index.calls_by_line[6]
    records = scope_records.call_records(
        frame.f_code, _call_offset(frame.f_code, node), Widget().render, frame
    )
    assert records == []


@needs_monitoring
def test_call_records_of_a_dotted_name_the_frame_no_longer_holds(tmp_path):
    frame = _frame_from(
        tmp_path,
        'deleted.py',
        "    widget = reg['a']\n    widget.render()\n    del widget\n",
        Registry(),
    )
    index = scope_records._index_for(frame.f_code.co_filename)
    [node] = index.calls_by_line[6]
    [record] = scope_records.call_records(
        frame.f_code, _call_offset(frame.f_code, node), Widget().render, frame
    )
    assert record.accessed == 'widget.render'
    assert record.candidates[0].endswith('Widget.render')


@needs_monitoring
def test_call_records_ignores_a_callable_with_no_documented_name(tmp_path):
    frame = _frame_from(tmp_path, 'nocand.py', "    reg['a'].render()\n", Registry())
    index = scope_records._index_for(frame.f_code.co_filename)
    [node] = index.calls_by_line[5]
    offset = _call_offset(frame.f_code, node)
    assert scope_records.call_records(frame.f_code, offset, functools.partial(len), frame) == []


@needs_monitoring
def test_call_records_ignores_a_call_that_is_not_on_an_attribute(tmp_path):
    frame = _frame_from(tmp_path, 'notattr.py', '    (lambda: 1)()\n', Registry())
    index = scope_records._index_for(frame.f_code.co_filename)
    [node] = index.calls_by_line[5]
    offset = _call_offset(frame.f_code, node)
    assert scope_records.call_records(frame.f_code, offset, Widget, frame) == []


# The callbacks below are invoked directly rather than through a real traced run:
# CPython suspends every other sys.monitoring tool while one tool's callback is
# running, so a callback reached through monitoring is invisible to coverage.


def _armed(on_scope=None, on_call=None, on_error=None):
    """Return a tracer armed for a file, without starting monitoring."""
    tracer = _tracing.ScopeTracer(
        on_scope or (lambda *args: None),
        on_call or (lambda *args: None),
        on_error or (lambda error: None),
    )
    tracer._basename = 'armed_example.py'
    return tracer


@needs_monitoring
def test_start_event_instruments_only_the_file_being_traced(tmp_path):
    path = tmp_path / 'events_example.py'
    path.write_text('x = 1\n')
    tracer = _tracing.ScopeTracer(lambda *a: None, lambda *a: None, lambda e: None)
    assert tracer.start(path.name)
    try:
        foreign = compile('y = 1', str(tmp_path / 'other.py'), 'exec')
        assert tracer._start_event(foreign, 0) is sys.monitoring.DISABLE
        assert tracer._instrumented == []

        mine = compile('x = 1', str(path), 'exec')
        assert tracer._start_event(mine, 0) is sys.monitoring.DISABLE
        assert tracer._instrumented == [mine]

        # the path is pinned now, so a same-named file elsewhere no longer matches
        elsewhere = compile('x = 1', str(tmp_path / 'sub' / path.name), 'exec')
        tracer._start_event(elsewhere, 0)
        assert tracer._instrumented == [mine]
    finally:
        tracer.close()


@needs_monitoring
def test_start_event_that_fails_gives_up_on_tracing(tmp_path):
    path = tmp_path / 'armed_example.py'
    errors = []
    tracer = _armed(on_error=errors.append)
    # no tool id claimed, so instrumenting raises
    assert tracer._start_event(compile('x = 1', str(path), 'exec'), 0) is sys.monitoring.DISABLE
    assert len(errors) == 1
    assert tracer._broken


@needs_monitoring
def test_return_and_call_events_report_only_their_own_frame():
    scopes, calls = [], []
    tracer = _armed(lambda code, frame: scopes.append(code), lambda code, *a: calls.append(code))
    own = sys._getframe().f_code
    assert tracer._return_event(own, 0, None) is sys.monitoring.DISABLE
    assert tracer._call_event(own, 0, len, None) is sys.monitoring.DISABLE
    assert scopes == [own]
    assert calls == [own]

    other = compile('x = 1', 'other.py', 'exec')
    tracer._return_event(other, 0, None)
    tracer._call_event(other, 0, len, None)
    assert scopes == [own]
    assert calls == [own]


@needs_monitoring
def test_events_do_not_re_enter_a_running_callback():
    scopes = []
    tracer = _armed(lambda code, frame: scopes.append(code))
    tracer._in_callback = True
    tracer._return_event(sys._getframe().f_code, 0, None)
    assert scopes == []


@needs_monitoring
def test_a_failing_scope_callback_gives_up_on_tracing():
    def boom(*args):
        raise RuntimeError

    errors = []
    tracer = _armed(on_scope=boom, on_call=boom, on_error=errors.append)
    assert tracer._return_event(sys._getframe().f_code, 0, None) is sys.monitoring.DISABLE
    assert tracer._call_event(sys._getframe().f_code, 0, len, None) is sys.monitoring.DISABLE
    # the second event finds the tracer already given up on, and reports nothing new
    assert len(errors) == 1


def test_stop_and_close_are_safe_before_anything_started():
    tracer = _tracing.ScopeTracer(lambda *a: None, lambda *a: None, lambda e: None)
    tracer.stop()
    tracer.close()
    assert tracer.active is False


@needs_monitoring
def test_call_records_skips_a_chain_off_a_call_the_return_type_resolves(tmp_path):
    frame = _frame_from(tmp_path, 'chain.py', '    make_widget().render()\n', Registry())
    index = scope_records._index_for(frame.f_code.co_filename)
    [outer] = [c for c in index.calls_by_line[5] if isinstance(c.func, ast.Attribute)]
    records = scope_records.call_records(
        frame.f_code, _call_offset(frame.f_code, outer), Widget().render, frame
    )
    assert records == []
