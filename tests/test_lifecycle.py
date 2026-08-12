# -*- coding: utf-8 -*-
"""
The whole lifecycle, executor through driver, with no hardware.

The other test files pin one mechanism each against a fake. This one runs a real
plan through the real executor and the simulated instrument, because several of
the claims only exist where those meet: that a measurement spanning many ticks
does not block the loop, that a dynamic stop actually ends an acquisition, and —
the one the paper turns on — that a run can hold at a checkpoint while something
outside reads what was acquired, amends the plan, and releases it.

The simulator is a stand-in, not a model. Nothing here asserts on the *values*
it produces, only on the control flow they drive.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace import (Command, ExperimentStateFile, Executor, MeasurementParams,
                  Peak, SimulatedInstrument, StopCondition)


PEAKS = [Peak('marker', 18.0), Peak('substrate', 28.0)]


def dynamic_params(name='film', **kwargs):
    return MeasurementParams(
        name=name, peaks=PEAKS,
        stop_condition=StopCondition(kind='Dynamic', max_scans=400,
                                     label='substrate', threshold=1000,
                                     trigger='rise', post_scans=10, **kwargs))


def static_params(name='quick', max_scans=5):
    return MeasurementParams(name=name, peaks=PEAKS,
                             stop_condition=StopCondition(max_scans=max_scans))


def run(tmp_path, commands, param_sets, **kwargs):
    store = ExperimentStateFile(tmp_path)
    driver = SimulatedInstrument(substrate_scan=30)
    ex = Executor(store, driver, param_sets=param_sets, tick_interval=0,
                  **kwargs)
    ex.load(commands)
    reason = ex.run()
    return ex, driver, reason


def status(tmp_path):
    with open(tmp_path / 'experiment_status.json', encoding='utf-8') as f:
        return json.load(f)


# ── A plan runs ──────────────────────────────────────────────────────

def test_a_plan_runs_end_to_end(tmp_path):
    ex, driver, reason = run(
        tmp_path,
        [Command('move_stage', [{'x': 1.0, 'y': 2.0}]),
         Command('set_temperature', [200.0, None, None]),
         Command('measure_now', ['quick', 1, 'run_a', 'S1']),
         Command('shutdown', [])],
        {'quick': static_params()})

    assert reason is None
    assert driver.position == {'x': 1.0, 'y': 2.0, 'z': 0.0}
    assert driver.temperature == 200.0
    assert [m.filename for m in driver.measurements] == ['run_a']
    assert driver.shutdown_complete


def test_a_measurement_spans_many_ticks_without_blocking(tmp_path):
    """The loop must keep turning during an acquisition — it is what polls for
    the stop command and writes the telemetry a monitor reads."""
    ex, driver, _ = run(tmp_path,
                        [Command('measure_now', ['quick', 1, 'run_a', 'S1'])],
                        {'quick': static_params(max_scans=8)})

    measurement = driver.measurements[0]
    assert measurement.scans == 8
    assert driver.ticks > 8, 'the loop ticked throughout the acquisition'


def test_every_named_peak_is_recorded(tmp_path):
    _, driver, _ = run(tmp_path,
                       [Command('measure_now', ['quick', 1, 'a', 'S1'])],
                       {'quick': static_params(max_scans=4)})
    profile = driver.measurements[0].profile
    assert sorted(profile) == ['marker', 'substrate']
    assert all(len(v) == 4 for v in profile.values())


# ── The dynamic stop ends an acquisition ─────────────────────────────

def test_a_dynamic_stop_ends_the_measurement_before_the_ceiling(tmp_path):
    """The ceiling is 400 scans and the substrate is at 30, so a run that
    reaches the ceiling means the trigger never fired."""
    _, driver, _ = run(tmp_path,
                       [Command('measure_now', ['film', 1, 'run_a', 'S1'])],
                       {'film': dynamic_params()})

    measurement = driver.measurements[0]
    assert measurement.stopped_by == 'dynamic'
    assert 30 < measurement.scans < 60, (
        f'stopped at {measurement.scans}: expected shortly after the substrate '
        f'marker at 30 plus the 10 post-trigger scans')


def test_a_static_condition_stops_at_its_scan_count(tmp_path):
    _, driver, _ = run(tmp_path,
                       [Command('measure_now', ['quick', 1, 'a', 'S1'])],
                       {'quick': static_params(max_scans=12)})
    assert driver.measurements[0].scans == 12
    assert driver.measurements[0].stopped_by == 'scan_limit'


def test_the_simulator_is_deterministic(tmp_path):
    """Two runs must agree, or a test that asserts on a decision means
    nothing."""
    a = SimulatedInstrument(substrate_scan=30)
    b = SimulatedInstrument(substrate_scan=30)
    params = dynamic_params()
    for driver in (a, b):
        ex = Executor(ExperimentStateFile(tmp_path), driver, tick_interval=0,
                      param_sets={'film': params})
        ex.load([Command('measure_now', ['film', 1, 'a', 'S1'])])
        ex.run()
    assert a.measurements[0].scans == b.measurements[0].scans
    assert a.measurements[0].profile == b.measurements[0].profile


# ── Stopping ─────────────────────────────────────────────────────────

def test_stop_measurement_ends_the_acquisition_and_the_run_continues(tmp_path):
    """The distinction that matters: this ends one command, not the run."""
    store = ExperimentStateFile(tmp_path)
    driver = SimulatedInstrument(substrate_scan=30)
    ex = Executor(store, driver, tick_interval=0,
                  param_sets={'film': dynamic_params()})
    ex.load([Command('measure_now', ['film', 1, 'first', 'S1']),
             Command('measure_now', ['film', 2, 'second', 'S1'])])

    stopped = []
    original = ex.write_status
    def stop_the_first_one():
        original()
        if (not stopped and driver._acquiring is not None
                and driver._acquiring.scans == 5):
            stopped.append(True)
            ex._live_stop_measurement()
    ex.write_status = stop_the_first_one

    ex.run()

    first, second = driver.measurements
    assert first.stopped_by == 'operator' and first.scans <= 7
    assert second.stopped_by == 'dynamic', \
        'the run should have carried on to the next measurement'


def test_ending_the_run_now_abandons_the_acquisition_in_flight(tmp_path):
    store = ExperimentStateFile(tmp_path)
    driver = SimulatedInstrument(substrate_scan=30)
    ex = Executor(store, driver, tick_interval=0,
                  param_sets={'film': dynamic_params()})
    ex.load([Command('measure_now', ['film', 1, 'first', 'S1']),
             Command('measure_now', ['film', 2, 'second', 'S1'])])

    original = ex.write_status
    def end_it():
        original()
        if driver._acquiring is not None and driver._acquiring.scans == 5:
            ex._live_end_experiment_now()
    ex.write_status = end_it

    ex.run()

    assert [m.stopped_by for m in driver.measurements] == ['abort']
    assert len(driver.measurements) == 1, 'nothing after it should have started'


# ── The checkpoint ───────────────────────────────────────────────────

def test_a_checkpoint_holds_the_run_until_something_outside_releases_it(tmp_path):
    """This is the shape of the whole study: measure, hold, decide, continue.

    The hold is what makes the decision possible — the acquired data is
    complete and the instrument is not doing anything else, so whatever is
    steering the run can read the result, amend what happens next, and release
    it. Here the "agent" reads the measurement, inserts a second one at a
    corrected position, and continues.
    """
    store = ExperimentStateFile(tmp_path)
    driver = SimulatedInstrument(substrate_scan=30)
    ex = Executor(store, driver, tick_interval=0,
                  param_sets={'film': dynamic_params()})
    ex.load([Command('measure_now', ['film', 1, 'first', 'S1']),
             Command('pause', ['QC checkpoint: judge the first measurement']),
             Command('shutdown', [])])

    decided = []
    original = ex.write_status
    def decide_at_the_checkpoint():
        original()
        if ex._paused and not decided:
            decided.append(True)
            # What the agent sees at the checkpoint: a finished measurement.
            assert len(driver.measurements) == 1
            assert driver.measurements[0].stopped_by == 'dynamic'
            ex.insert([Command('measure_now', ['film', 2, 'second', 'S1'])])
            ex._live_continue_experiment()
    ex.write_status = decide_at_the_checkpoint

    ex.run()

    assert decided, 'the run never reached the checkpoint'
    assert [m.filename for m in driver.measurements] == ['first', 'second']
    assert driver.shutdown_complete, 'the rest of the plan should still run'


def test_a_persistent_run_holds_after_the_plan_drains(tmp_path):
    """The other way to hold: the plan simply ends and the run waits for the
    next decision rather than tearing down."""
    store = ExperimentStateFile(tmp_path)
    driver = SimulatedInstrument()
    ex = Executor(store, driver, tick_interval=0, persistent=True,
                  param_sets={'quick': static_params()})
    ex.load([Command('measure_now', ['quick', 1, 'first', 'S1'])])

    original = ex.write_status
    def add_work_once_then_finish():
        original()
        if ex._persistent_idle:
            if len(driver.measurements) == 1:
                ex.insert([Command('measure_now', ['quick', 2, 'second', 'S1'])])
            else:
                ex._live_end_experiment_after()
    ex.write_status = add_work_once_then_finish

    ex.run()

    assert [m.filename for m in driver.measurements] == ['first', 'second']
    assert status(tmp_path)['persistent_idle'] is True


# ── Telemetry ────────────────────────────────────────────────────────

def test_the_driver_reports_alongside_the_executor(tmp_path):
    run(tmp_path,
        [Command('set_temperature', [150.0, None, None]),
         Command('measure_now', ['quick', 1, 'a', 'S1'])],
        {'quick': static_params()})

    reported = status(tmp_path)
    assert reported['temperature'] == 150.0        # from the driver
    assert reported['measurements_completed'] == 1
    assert reported['n_steps'] == 2                # from the executor
    assert reported['persistent'] is False


# ── Refusals ─────────────────────────────────────────────────────────

def test_an_axis_given_both_an_absolute_and_a_relative_target_is_refused(tmp_path):
    """Commanding one and verifying the other fails against a target that was
    never aimed at — so it is refused before anything moves."""
    ex, driver, reason = run(tmp_path,
                             [Command('move_stage', [{'x': 1.0, 'dx': 0.5}])],
                             {})
    assert reason is not None and 'ValueError' in reason
    assert driver.position['x'] == 0.0, 'nothing should have moved'


def test_both_ramp_arguments_together_are_refused(tmp_path):
    """They over-determine the same line, so honouring either would silently
    deliver a rate nobody asked for."""
    _, _, reason = run(tmp_path,
                       [Command('set_temperature', [400.0, 10.0, 30.0])], {})
    assert reason is not None and 'ValueError' in reason


def test_a_plan_step_the_driver_cannot_do_names_the_step(tmp_path):
    _, _, reason = run(
        tmp_path,
        [Command('time_series', [[0.0, 60.0], 'quick', 1, 'a', 'S1'])],
        {'quick': static_params()})
    assert 'time_series' in reason
    assert 'SimulatedInstrument' in reason


# ── The boundary's stated authority must match its actual surface ────

def test_the_boundary_docstring_counts_its_commanding_methods():
    """The boundary docstring invites a reader to judge how much authority the
    orchestration layer has over hardware by counting the methods in it, so the
    count has to be right.

    It said "five", which omitted ``abort`` and ``stop_measurement`` — the two
    that end an acquisition rather than start one. Understating the authority
    surface is the wrong direction to be wrong in for a safety claim, and it is
    the drift a prose count acquires as soon as a method is added below it.
    """
    import inspect
    from pace.driver import base

    # These reach nothing: one receives the envelope, one is housekeeping, one
    # reports. Everything else on the interface can move the instrument.
    NON_COMMANDING = {'use_envelope', 'on_tick', 'status'}
    defined = {name for name, _ in
               inspect.getmembers(base.InstrumentDriver, inspect.isfunction)
               if not name.startswith('_')}
    commanding = defined - NON_COMMANDING

    assert len(commanding) == 7, sorted(commanding)
    assert 'seven methods command' in base.__doc__
    for name in sorted(commanding):
        assert f'``{name}``' in base.__doc__, (
            f'{name} commands the instrument but the boundary docstring, which '
            f'is what a reader counts, does not list it')
