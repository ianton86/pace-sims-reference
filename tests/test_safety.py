# -*- coding: utf-8 -*-
"""
The safety envelope, and the provenance rule that decides reject from clamp.

This is the layer that does not trust the agent, so its tests are about what
happens when something above it asks for the wrong thing. The rule under test is
narrow and easy to state backwards, which is why it is worth pinning from both
directions:

* an **operator's** out-of-range value is REJECTED, so a mistake in a plan
  surfaces rather than quietly producing the wrong experiment;
* an **automatic** one is CLAMPED and logged, because aborting an unattended run
  is worse than running at the edge of a declared range;
* **unknown** provenance is treated as the operator's, so a caller that has not
  been taught about the envelope fails closed;
* and nothing is defaulted — an undeclared bound is unconstrained, never a
  guess at what is safe.

Getting the first two the wrong way round is the failure worth engineering
against: it would silently rewrite an operator's experiment and abort an
unattended one.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace import (Command, EnvelopeViolation, ExperimentStateFile, Executor,
                  MeasurementParams, Peak, SafetyEnvelope, SimulatedInstrument,
                  StopCondition)
from pace.safety import guard_command


def envelope(**kwargs):
    kwargs.setdefault('temperature', (-150.0, 600.0))
    return SafetyEnvelope(**kwargs)


# ── Nothing is defaulted ─────────────────────────────────────────────

def test_an_empty_envelope_constrains_nothing():
    """Unconstrained is the pre-envelope behaviour and must stay reachable: a
    guessed safe range would either permit something destructive or pin an
    experiment to a range nobody chose."""
    empty = SafetyEnvelope()
    assert empty.check_temperature(10_000.0) == 10_000.0
    empty.check_stage(x=999.0)          # does not raise
    assert empty.to_dict()['temperature'] is None


def test_an_undeclared_axis_is_unconstrained_not_zero():
    env = SafetyEnvelope(stage={'x': (0.0, 10.0)})
    env.check_stage(y=500.0)            # y was never declared
    with pytest.raises(EnvelopeViolation):
        env.check_stage(x=500.0)


def test_inverted_limits_are_refused_at_declaration():
    """A bound that cannot be satisfied should fail where it is written, not on
    the first value that meets it."""
    with pytest.raises(ValueError):
        SafetyEnvelope(temperature=(600.0, -150.0))
    with pytest.raises(ValueError):
        SafetyEnvelope(stage={'x': (5.0, 5.0)})


def test_declaring_stage_limits_replaces_rather_than_merges():
    """So the declaration in front of you is the whole of what is enforced."""
    env = SafetyEnvelope(stage={'x': (0.0, 10.0), 'y': (0.0, 10.0)})
    env.set_stage_limits(x=(0.0, 5.0))
    assert set(env.stage) == {'x'}
    env.check_stage(y=999.0)


# ── Reject or clamp, by provenance ───────────────────────────────────

def test_an_operator_value_out_of_range_is_rejected():
    with pytest.raises(EnvelopeViolation) as excinfo:
        envelope().resolve(900.0, 'plan')
    assert '900.0' in str(excinfo.value) and '600.0' in str(excinfo.value), \
        'the refusal should name both the value and the bound it broke'


def test_an_automatic_value_out_of_range_is_clamped():
    value, clamped = envelope().resolve(900.0, 'auto')
    assert (value, clamped) == (600.0, True)


def test_it_clamps_to_the_nearer_bound_in_both_directions():
    assert envelope().resolve(-900.0, 'auto')[0] == -150.0
    assert envelope().resolve(900.0, 'auto')[0] == 600.0


def test_unknown_provenance_is_treated_as_the_operators():
    """Failing closed: a caller that has not been taught about the envelope is
    refused rather than quietly clamped."""
    for origin in (None, '', 'agent', 'AUTO'):
        with pytest.raises(EnvelopeViolation):
            envelope().resolve(900.0, origin)


def test_a_value_inside_the_range_is_untouched_either_way():
    for origin in ('plan', 'auto'):
        assert envelope().resolve(400.0, origin) == (400.0, False)


def test_the_bounds_themselves_are_allowed():
    """Inclusive, so a plan may sit exactly on a declared limit."""
    for origin in ('plan', 'auto'):
        assert envelope().resolve(600.0, origin) == (600.0, False)
        assert envelope().resolve(-150.0, origin) == (-150.0, False)


# ── Guarding a command ───────────────────────────────────────────────

def test_a_clamped_value_is_written_back_into_the_command():
    """Three things depend on this: the log and telemetry report what the
    instrument was really asked for, the plan file records it, and a RESUMED
    run is not rejected on the stale value — a resumed step counts as
    operator-planned, so it would be refused rather than clamped next time."""
    command = Command('set_temperature', [900.0, None, None])
    command.origin = 'auto'
    guard_command(command, envelope())
    assert command.args[0] == 600.0


def test_a_rejected_command_is_left_alone():
    command = Command('set_temperature', [900.0, None, None])
    with pytest.raises(EnvelopeViolation):
        guard_command(command, envelope())
    assert command.args[0] == 900.0, 'a refusal must not half-apply'


def test_an_absolute_stage_target_is_bounded():
    env = SafetyEnvelope(stage={'x': (0.0, 10.0)})
    with pytest.raises(EnvelopeViolation):
        guard_command(Command('move_stage', [{'x': 50.0}]), env)


def test_a_relative_stage_move_is_not_bounded_here():
    """Deliberate, and stated rather than half-implemented: resolving a
    relative move needs to know where the stage is, which this layer does not.
    The driver bounds it. A check that covered some moves and not others would
    read as one that covered all of them."""
    env = SafetyEnvelope(stage={'x': (0.0, 10.0)})
    guard_command(Command('move_stage', [{'dx': 50.0}]), env)     # no raise


def test_no_envelope_guards_nothing():
    command = Command('set_temperature', [9000.0, None, None])
    guard_command(command, None)
    assert command.args[0] == 9000.0


# ── Persistence ──────────────────────────────────────────────────────

def test_the_envelope_survives_a_round_trip():
    original = SafetyEnvelope(temperature=(-150.0, 600.0),
                              stage={'x': (0.0, 10.0), 'z': (1.0, 2.0)},
                              withdraw_z=5.0)
    back = SafetyEnvelope.from_dict(original.to_dict())
    assert back.to_dict() == original.to_dict()
    with pytest.raises(EnvelopeViolation):
        back.resolve(900.0, 'plan')


def test_a_missing_record_is_no_envelope():
    """A run from before the envelope existed resumes unconstrained, which is
    what it ran as — not silently bounded by somebody else's numbers."""
    assert SafetyEnvelope.from_dict(None) is None
    assert SafetyEnvelope.from_dict({}) is None


def test_a_malformed_record_raises_rather_than_resuming_unconstrained():
    """The one outcome that must not happen quietly."""
    with pytest.raises(ValueError):
        SafetyEnvelope.from_dict({'temperature': [600.0, -150.0]})


# ── Through the executor ─────────────────────────────────────────────

def params():
    return MeasurementParams(peaks=[Peak('marker', 18.0)],
                             stop_condition=StopCondition(max_scans=3))


def run(tmp_path, commands, env):
    store = ExperimentStateFile(tmp_path)
    driver = SimulatedInstrument()
    ex = Executor(store, driver, tick_interval=0, envelope=env,
                  param_sets={'quick': params()})
    ex.load(commands)
    return ex, driver, ex.run()


def test_an_operators_out_of_envelope_step_ends_the_run(tmp_path):
    _, driver, reason = run(tmp_path,
                            [Command('set_temperature', [900.0, None, None])],
                            envelope())
    assert 'EnvelopeViolation' in reason
    assert driver.setpoint == 25.0, 'nothing should have been commanded'


def test_an_automatic_step_runs_at_the_bound_instead_of_aborting(tmp_path):
    """An unattended run must not be ended by a generated value; it runs at the
    edge of the declared range and says so."""
    store = ExperimentStateFile(tmp_path)
    driver = SimulatedInstrument()
    ex = Executor(store, driver, tick_interval=0, envelope=envelope(),
                  param_sets={'quick': params()})
    ex.load([Command('measure_now', ['quick', 1, 'a', 'S1'])])
    ex.insert([Command('set_temperature', [900.0, None, None])])   # stamped auto

    assert ex.run() is None
    assert driver.setpoint == 600.0


def test_the_driver_is_handed_the_envelope(tmp_path):
    """It generates values the plan never named — a ramp's intermediate
    setpoints — so it has to bound those itself."""
    store = ExperimentStateFile(tmp_path)
    driver = SimulatedInstrument()
    env = envelope()
    ex = Executor(store, driver, tick_interval=0, envelope=env)
    ex.load([])
    ex.run()
    assert driver.envelope is env


def test_the_envelope_is_recorded_in_the_plan(tmp_path):
    """Read back off the object that enforces it, so the record cannot claim
    bounds that are not the ones in force."""
    run(tmp_path, [Command('shutdown', [])],
        SafetyEnvelope(temperature=(-150.0, 600.0), withdraw_z=5.0))

    with open(tmp_path / 'experiment.json', encoding='utf-8') as f:
        recorded = json.load(f)['meta']['safety_envelope']
    assert recorded['temperature'] == [-150.0, 600.0]
    assert recorded['withdraw_z'] == 5.0

    restored = SafetyEnvelope.from_dict(recorded)
    with pytest.raises(EnvelopeViolation):
        restored.resolve(900.0, 'plan')
