# -*- coding: utf-8 -*-
"""
``experiment.json`` must round-trip every argument it carries.

The two converters are inverses, and this is where that is enforced. A step is
serialised by ``command_to_step`` and rebuilt by ``step_to_command``, and an
argument carried by only one of them produces a specific, quiet failure: the
plan works when it is built in memory and is silently altered by the file — and
the file is what a **resumed** run rebuilds its queue from, and what every live
edit passes through.

The temperature ramp is the worked example. If ``ramp_rate`` survives the
in-memory path but not the file, a resumed run turns every remaining ramp into
an instant setpoint change, on the one parameter whose entire point is the rate
at which it moves.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace import (Command, MeasurementParams, Peak, StopCondition,
                  ExperimentStateFile, command_to_step, step_to_command)


# ── The converters are inverses ──────────────────────────────────────

@pytest.mark.parametrize('cmd', [
    Command('measure_now', ['film', 3, 'run_a', 'STO']),
    Command('measure_now', [None, None, None, None]),
    Command('time_series', [[0.0, 300.0, 600.0], 'film', 2, 'series', None]),
    Command('set_temperature', [400.0, None, None]),
    Command('set_temperature', [400.0, 10.0, None]),
    Command('set_temperature', [400.0, None, 30.0]),
    Command('wait_a_while', [15.0]),
    Command('move_stage', [{'x': 1.0, 'y': 2.0}]),
    Command('move_stage', [{'dz': -0.5}]),
    Command('change_measurement_params', ['other']),
    Command('pause', ['checkpoint 3']),
    Command('pause', ['']),
    Command('shutdown', []),
])
def test_every_command_survives_a_round_trip(cmd):
    back = step_to_command(command_to_step(cmd))
    assert back.name == cmd.name
    assert back.args == cmd.args, (
        f'{cmd.name}: {cmd.args} became {back.args} — an argument carried by '
        f'only one direction is dropped by the file and lost on resume')


def test_the_ramp_arguments_reach_the_file():
    """The failure this test exists for: a ramp that becomes a step change."""
    step = command_to_step(Command('set_temperature', [400.0, 10.0, None]))
    assert step['args']['ramp_rate'] == 10.0
    assert 'ramp_time' not in step['args'], 'an unset option should not appear'


def test_a_step_written_without_the_optional_arguments_still_resolves():
    """Hand-edited plans and older files must not need every key."""
    cmd = step_to_command({'action': 'set_temperature',
                           'args': {'temperature': 300.0}, 'status': 'pending'})
    assert cmd.args == [300.0, None, None]


def test_an_unmodelled_action_is_carried_through_rather_than_dropped():
    """Extension should be additive: a file from a richer build round-trips."""
    step = {'action': 'align_column', 'args': [1, 2], 'status': 'pending'}
    cmd = step_to_command(step)
    assert cmd.name == 'align_column'
    assert command_to_step(cmd)['action'] == 'align_column'


def test_minutes_in_the_file_seconds_internally():
    """The file is written for a human to edit; the executor works in seconds."""
    step = command_to_step(Command('time_series', [[0.0, 300.0], 'p', 1, None, None]))
    assert step['args']['times_min'] == [0.0, 5.0]
    assert step_to_command(step).args[0] == [0.0, 300.0]


# ── The file itself ──────────────────────────────────────────────────

def _params():
    return {'film': MeasurementParams(
        name='film', peaks=[Peak('O-', 15.995), Peak('Si-', 27.977)],
        stop_condition=StopCondition(kind='Dynamic', label='Si-',
                                     threshold=1e4, max_scans=200))}


def test_the_exported_file_has_the_documented_shape(tmp_path):
    sf = ExperimentStateFile(tmp_path)
    sf.export([Command('measure_now', ['film', 1, None, None])], 0, _params(),
              meta={'path': str(tmp_path)}, positions=[(1.0, 2.0)],
              stage_rotation=12.0)

    data = json.loads((tmp_path / 'experiment.json').read_text())
    assert set(data) == {'meta', 'param_sets', 'positions', 'commands', 'steps'}
    assert data['commands'] == [], 'the inbox is exported empty'
    assert data['meta']['current_step'] == 0
    assert data['steps'][0]['status'] == 'running'
    assert data['positions']['stage_rotation'] == 12.0


def test_step_status_follows_the_current_index(tmp_path):
    sf = ExperimentStateFile(tmp_path)
    cmds = [Command('wait_a_while', [1.0]) for _ in range(3)]
    sf.export(cmds, 1, {})
    statuses = [s['status'] for s in
                json.loads((tmp_path / 'experiment.json').read_text())['steps']]
    assert statuses == ['completed', 'running', 'pending']


def test_an_immediate_command_is_read_then_cleared(tmp_path):
    """The inbox is consumed, or the same instruction runs again next poll."""
    sf = ExperimentStateFile(tmp_path)
    sf.export([Command('wait_a_while', [1.0])], 0, _params())

    path = tmp_path / 'experiment.json'
    data = json.loads(path.read_text())
    data['commands'] = ['pause_after']
    path.write_text(json.dumps(data))

    got = sf.import_updates()
    assert got['commands'] == ['pause_after']
    assert json.loads(path.read_text())['commands'] == []


def test_no_baseline_is_not_an_unread_change(tmp_path):
    """Declaring a fresh experiment where one already exists must not import
    the previous run's pending steps."""
    (tmp_path / 'experiment.json').write_text('{"steps": []}')
    sf = ExperimentStateFile(tmp_path)
    assert sf.check_external_update() is True
    assert sf.unread_external_change() is False


def test_an_export_marks_the_file_as_read_by_us(tmp_path):
    sf = ExperimentStateFile(tmp_path)
    sf.export([Command('wait_a_while', [1.0])], 0, {})
    assert sf.unread_external_change() is False


def test_completed_steps_from_an_earlier_run_are_preserved(tmp_path):
    sf = ExperimentStateFile(tmp_path)
    earlier = [{'action': 'measure_now', 'status': 'completed'}]
    sf.export([Command('wait_a_while', [1.0])], 0, {}, completed_steps=earlier)
    steps = json.loads((tmp_path / 'experiment.json').read_text())['steps']
    assert len(steps) == 2 and steps[0]['status'] == 'completed'


def test_runtime_status_goes_to_its_own_file(tmp_path):
    """Sharing a file with the agent-editable plan would race a per-tick write
    against an edit."""
    sf = ExperimentStateFile(tmp_path)
    sf.export([Command('wait_a_while', [1.0])], 0, {})
    sf.update_runtime_status({'measurement_phase': 'running', 'current_scan': 12})

    status = json.loads((tmp_path / 'experiment_status.json').read_text())
    assert status['current_scan'] == 12 and 'last_tick' in status
    assert 'measurement_phase' not in \
        json.loads((tmp_path / 'experiment.json').read_text())


def test_a_partly_written_file_is_never_visible(tmp_path):
    """Atomic write: a reader sees the old plan or the new one, never half."""
    sf = ExperimentStateFile(tmp_path)
    sf.export([Command('wait_a_while', [1.0])], 0, _params())
    assert not list(tmp_path.glob('*.tmp'))
    json.loads((tmp_path / 'experiment.json').read_text())   # parses
