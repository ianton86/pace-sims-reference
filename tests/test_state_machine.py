# -*- coding: utf-8 -*-
"""
The executor's control flow, pinned against a fake driver.

Everything here runs with no hardware and no simulator: a driver is any object
with a method per command, so the fakes below are a handful of lines each. That
is the point of the seam rather than a convenience for testing — the same seam
is what lets the replay harness re-run the loop over logged data.

Four properties carry weight beyond bookkeeping, and each has a failure that is
quiet rather than loud:

* **The two-value handler protocol.** ``IDLE`` re-runs, ``NEXT`` advances. Get
  it wrong and a long operation either blocks the loop (so the run cannot be
  stopped while it happens) or is abandoned half-finished.
* **The abort backstop.** A failing handler must end the run through the normal
  exit path. Letting it propagate skips the caller's teardown, so a failed step
  corrupts the record of the steps that already succeeded.
* **Ending is three different things.** Immediate, graceful, and stopping one
  measurement. Conflating any two loses something real — the tests spell out
  which.
* **The plan is amendable while it runs**, including in the window between the
  executor's last read and its next write, which is where an edit gets
  destroyed invisibly.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace import (Command, ExperimentStateFile, Executor, MeasurementParams,
                  NextAction)


# ── Fakes ────────────────────────────────────────────────────────────

class FakeDriver:
    """Records what it was asked to do; finishes every command in one tick."""

    def __init__(self, **overrides):
        self.calls = []
        self.aborted = False
        self.ticks = 0
        self._overrides = overrides

    def _record(self, name, args):
        self.calls.append((name, list(args)))
        handler = self._overrides.get(name)
        if handler is not None:
            return handler(*args)
        return NextAction.NEXT

    def measure_now(self, *args):
        return self._record('measure_now', args)

    def time_series(self, *args):
        return self._record('time_series', args)

    def set_temperature(self, *args):
        return self._record('set_temperature', args)

    def move_stage(self, *args):
        return self._record('move_stage', args)

    def shutdown(self, *args):
        return self._record('shutdown', args)

    def abort(self):
        self.aborted = True

    def on_tick(self):
        self.ticks += 1

    def status(self):
        return {'driver_calls': len(self.calls)}


def build(tmp_path, commands, driver=None, **kwargs):
    store = ExperimentStateFile(tmp_path)
    driver = driver if driver is not None else FakeDriver()
    ex = Executor(store, driver, tick_interval=0, **kwargs)
    ex.load(commands)
    return ex, driver, store


def read_status(tmp_path):
    with open(tmp_path / 'experiment_status.json', encoding='utf-8') as f:
        return json.load(f)


def external_edit(tmp_path, mutate):
    """Write the plan file the way an outside agent would, and make the change
    unambiguously newer than whatever the executor last wrote."""
    path = tmp_path / 'experiment.json'
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    mutate(data)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    stamp = os.stat(path).st_mtime + 10
    os.utime(path, (stamp, stamp))


# ── The handler protocol ─────────────────────────────────────────────

def test_idle_re_runs_the_same_command_and_next_advances(tmp_path):
    """The whole of the loop's control flow, in one test."""
    seen = []

    def slow(*args):
        seen.append(len(seen))
        return NextAction.IDLE if len(seen) < 3 else NextAction.NEXT

    driver = FakeDriver(move_stage=slow)
    ex, _, _ = build(tmp_path, [Command('move_stage', [{'x': 1.0}]),
                                Command('shutdown', [])], driver)
    ex.run()

    assert len(seen) == 3, 'IDLE should have called the same handler again'
    assert [name for name, _ in driver.calls] == ['move_stage'] * 3 + ['shutdown']
    assert ex.index == 2


def test_a_handler_returning_something_else_is_refused(tmp_path):
    """Not a NextAction means the loop cannot know what to do next, so it must
    stop rather than pick a direction."""
    driver = FakeDriver(shutdown=lambda *a: 'done')
    ex, _, _ = build(tmp_path, [Command('shutdown', [])], driver)
    reason = ex.run()
    assert reason is not None and 'NextAction' in reason


def test_a_command_with_no_handler_names_both_places_it_looked(tmp_path):
    ex, driver, _ = build(tmp_path, [Command('align_column', [])])
    reason = ex.run()
    assert 'align_column' in reason
    assert 'FakeDriver' in reason, 'the message should name the driver that lacks it'


# ── The abort backstop ───────────────────────────────────────────────

def boom(*args):
    raise ValueError('the stage did not answer')


def test_a_failing_handler_ends_the_run_through_the_normal_path(tmp_path):
    """It must not propagate: the caller's teardown is what flushes the data
    store and the log, and skipping it corrupts the record of the steps that
    had already succeeded."""
    driver = FakeDriver(move_stage=boom)
    ex, _, _ = build(tmp_path, [Command('move_stage', [{'x': 1.0}]),
                                Command('shutdown', [])], driver)

    reason = ex.run()      # does not raise

    assert 'ValueError' in reason and 'the stage did not answer' in reason
    assert 'shutdown' not in [name for name, _ in driver.calls], \
        'the run should stop at the failure, not carry on'


def test_the_abort_is_recorded_where_a_monitor_will_see_it(tmp_path):
    driver = FakeDriver(move_stage=boom)
    ex, _, _ = build(tmp_path, [Command('move_stage', [{}])], driver)
    ex.run()

    status = read_status(tmp_path)
    assert status['aborted'] is True
    assert 'ValueError' in status['abort_reason']


def test_a_clean_finish_carries_no_abort_keys(tmp_path):
    """Otherwise a monitor cannot tell a finished run from a crashed one."""
    ex, _, _ = build(tmp_path, [Command('shutdown', [])])
    assert ex.run() is None
    status = read_status(tmp_path)
    assert 'aborted' not in status and 'abort_reason' not in status


def test_a_failing_plan_export_does_not_suppress_the_status_write(tmp_path):
    """The two records are guarded independently, and telemetry goes first: it
    is what a monitor polls, so it is the one that must survive."""
    driver = FakeDriver(move_stage=boom)
    ex, _, store = build(tmp_path, [Command('move_stage', [{}])], driver)

    def refuse(*args, **kwargs):
        raise OSError('plan file is locked')
    store.export = refuse

    ex.run()
    assert read_status(tmp_path)['aborted'] is True


# ── Ending a run is three different things ───────────────────────────

def test_end_now_fires_mid_command_and_aborts_what_is_in_flight(tmp_path):
    """Checked at the top of the tick, so it does not wait for a boundary — an
    operator ending a run *now* does not mean at the end of this acquisition."""
    ex, driver, _ = build(tmp_path, [Command('measure_now', [None, 1, None, None]),
                                     Command('shutdown', [])],
                          driver=FakeDriver(measure_now=lambda *a: NextAction.IDLE))

    def stop_it():
        ex._live_end_experiment_now()
    driver._overrides['measure_now'] = lambda *a: (stop_it(), NextAction.IDLE)[1]

    ex.run()
    assert driver.aborted, 'the driver should have been told to abort'
    assert 'shutdown' not in [n for n, _ in driver.calls]


def test_end_after_waits_for_the_boundary_and_leaves_the_rest_pending(tmp_path):
    """Graceful: it never interrupts an acquisition, and what has not run stays
    in the file so the run can be resumed or amended."""
    ex, driver, _ = build(tmp_path, [Command('measure_now', [None, 1, None, None]),
                                     Command('measure_now', [None, 2, None, None]),
                                     Command('shutdown', [])])
    ex._live_end_experiment_after()
    ex.run()

    assert [n for n, _ in driver.calls] == ['measure_now'], \
        'the command in flight should finish, and nothing after it should start'

    with open(tmp_path / 'experiment.json', encoding='utf-8') as f:
        steps = json.load(f)['steps']
    assert [s['status'] for s in steps] == ['completed', 'running', 'pending']


def test_stop_measurement_only_arms_against_a_measurement(tmp_path):
    """Raising the flag against a temperature step would leave it set for
    whatever measurement came next — stopping one nobody asked to stop."""
    ex, _, _ = build(tmp_path, [Command('set_temperature', [400.0, None, None]),
                                Command('measure_now', [None, 1, None, None])])

    ex._live_stop_measurement()
    assert ex.stop_measurement_requested is False

    ex.index = 1
    ex._live_stop_measurement()
    assert ex.stop_measurement_requested is True


def test_stop_measurement_is_cleared_when_the_command_completes(tmp_path):
    ex, _, _ = build(tmp_path, [Command('measure_now', [None, 1, None, None]),
                                Command('measure_now', [None, 2, None, None])])
    ex._live_stop_measurement()
    assert ex.stop_measurement_requested is True
    ex.run()
    assert ex.stop_measurement_requested is False, \
        'the flag is scoped to one command, or it stops the next one too'


# ── Persistent mode: the mechanism a checkpoint gate stands on ───────

def test_a_drained_queue_ends_a_plain_run(tmp_path):
    ex, _, _ = build(tmp_path, [Command('shutdown', [])], persistent=False)
    ex.run()
    assert ex._persistent_idle is False


def test_a_persistent_run_holds_instead_of_ending(tmp_path):
    """It keeps polling and keeps writing telemetry — a run holding at a
    checkpoint still has hardware to report on."""
    ex, driver, _ = build(tmp_path, [Command('shutdown', [])], persistent=True)

    original = ex.write_status
    def release_after_a_few_idle_ticks():
        original()
        if ex._persistent_idle and driver.ticks >= 3:
            ex._live_end_experiment_after()
    ex.write_status = release_after_a_few_idle_ticks

    ex.run()
    assert driver.ticks >= 3, 'the driver should keep ticking while idle'
    assert read_status(tmp_path)['persistent_idle'] is True


# ── The plan is amendable while it runs ──────────────────────────────

def test_an_external_edit_replaces_pending_steps_only(tmp_path):
    """The file's copy of a running step cannot be more current than ours, and
    adopting it would restart the step in progress."""
    ex, driver, _ = build(tmp_path, [Command('set_temperature', [400.0, None, None]),
                                     Command('shutdown', [])])
    ex.export()

    def swap_the_pending_step(data):
        for step in data['steps']:
            if step['status'] == 'pending':
                step['action'] = 'wait_a_while'
                step['args'] = {'time': 0.0}
    external_edit(tmp_path, swap_the_pending_step)

    ex.run()
    assert 'shutdown' not in [n for n, _ in driver.calls]
    assert ex.queue[-1].name == 'wait_a_while'


def test_an_edit_landing_before_an_export_is_applied_not_overwritten(tmp_path):
    """The window this closes is narrow, silent and permanent.

    The executor writes the plan wholesale from memory, so an edit that arrives
    after its last poll is destroyed — and destroyed invisibly, because the
    executor's own write refreshes the modification time the poll compares
    against, so the change is never seen again. It presents as "my edit did
    nothing".
    """
    edited = []

    def edit_during_the_command(*args):
        # Land the edit after this tick's poll and before the boundary export,
        # which is exactly the window.
        if not edited:
            edited.append(True)
            def append_a_step(data):
                data['steps'].append({'action': 'move_stage', 'status': 'pending',
                                      'args': {'x': 1.0}})
            external_edit(tmp_path, append_a_step)
        return NextAction.NEXT

    driver = FakeDriver(set_temperature=edit_during_the_command)
    ex, _, _ = build(tmp_path, [Command('set_temperature', [400.0, None, None])],
                     driver)
    ex.export()
    ex.run()

    assert [n for n, _ in driver.calls] == ['set_temperature', 'move_stage'], \
        'the inserted step was overwritten by the export instead of applied'


def test_insert_stamps_provenance(tmp_path):
    """``origin`` is what the safety envelope resolves an out-of-range value by:
    an operator's is rejected so the mistake surfaces, an automatic one is
    clamped, because aborting an unattended run is worse."""
    ex, _, _ = build(tmp_path, [Command('shutdown', [])])
    explicit = Command('wait_a_while', [1.0])
    explicit.origin = 'plan'
    generated = Command('wait_a_while', [1.0])

    ex.insert([generated])
    assert generated.origin == 'auto'

    explicit.origin = 'operator_override'
    ex.insert([explicit])
    assert explicit.origin == 'operator_override', \
        'a caller that set an origin deliberately should be left alone'


# ── Live commands ────────────────────────────────────────────────────

def test_an_unknown_live_command_is_acknowledged_by_name(tmp_path):
    """The sender has no return channel but the telemetry file, so a rejection
    that is not published is indistinguishable from one never read."""
    ex, _, _ = build(tmp_path, [Command('shutdown', [])])
    ex.export()
    external_edit(tmp_path, lambda d: d.__setitem__('commands', ['stop_now']))

    ex.poll()
    ex.write_status()

    ack = read_status(tmp_path)['last_command']
    assert ack['command'] == 'stop_now'
    assert ack['status'] == 'unknown'
    assert 'end_experiment_now' in ack['error'], 'the valid set should be named'


def test_a_live_command_that_raises_is_acknowledged_as_an_error(tmp_path):
    ex, _, _ = build(tmp_path, [Command('shutdown', [])])
    ex._live_commands = dict(ex._live_commands, boom='_live_boom')
    ex._live_boom = boom
    ex.export()
    external_edit(tmp_path, lambda d: d.__setitem__('commands', ['boom']))

    ex.poll()
    ex.write_status()
    assert read_status(tmp_path)['last_command']['status'] == 'error'


def test_pause_holds_the_run_and_continue_releases_it(tmp_path):
    ex, driver, _ = build(tmp_path, [Command('pause', ['checkpoint 1']),
                                     Command('shutdown', [])])

    ticks = []
    original = ex.write_status
    def release_after_a_few():
        original()
        ticks.append(1)
        if len(ticks) == 3:
            ex._live_continue_experiment()
    ex.write_status = release_after_a_few

    ex.run()
    assert len(ticks) > 3, 'the loop should have kept ticking while paused'
    assert [n for n, _ in driver.calls] == ['shutdown']
    assert ex._paused is False


# ── Parameter sets ───────────────────────────────────────────────────

def test_the_named_parameter_set_is_resolved_before_dispatch(tmp_path):
    """The driver is handed the parameters, never the plan's name for them."""
    film = MeasurementParams()
    command = Command('measure_now', ['film', 1, None, None])
    ex, driver, _ = build(tmp_path, [command], param_sets={'film': film})
    ex.run()

    assert driver.calls[0][1][0] is film
    assert command.args[0] == 'film', (
        'resolving must not write the parameters back into the command — the '
        'plan refers to a set by name, and an inline copy there would stop an '
        'edit of the set reaching the steps that have not run')


def test_an_unnamed_measurement_takes_the_active_set(tmp_path):
    """This is what ``change_measurement_params`` is for: a step that names no
    set follows whatever the run last switched to."""
    default, other = MeasurementParams(), MeasurementParams()
    ex, driver, _ = build(
        tmp_path,
        [Command('measure_now', [None, 1, None, None]),
         Command('change_measurement_params', ['other']),
         Command('measure_now', [None, 2, None, None])],
        param_sets={'default': default, 'other': other})
    ex.run()

    measured = [args[0] for name, args in driver.calls if name == 'measure_now']
    assert measured == [default, other]


def test_an_unknown_parameter_set_keeps_the_current_one(tmp_path):
    """A typo must not silently leave the run with no parameters at all."""
    default = MeasurementParams()
    ex, driver, _ = build(
        tmp_path,
        [Command('change_measurement_params', ['flim']),
         Command('measure_now', [None, 1, None, None])],
        param_sets={'default': default})
    ex.run()
    assert ex.active_param_set == 'default'
    assert driver.calls[-1][1][0] is default
