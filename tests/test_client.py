# -*- coding: utf-8 -*-
"""
Amending a running plan from outside it, and the lost update that threatens it.

The client is how a checkpoint decision becomes a change to the run: a retune is
``update_param_set``, a repeat is ``insert_step``, releasing the hold is
``send_command``. So the tests are of two kinds — that each of those does what
the decision meant, and that none of them can silently destroy the executor's
own record of the run.

The second kind is the one that matters. A read-modify-write here races the
executor's wholesale rewrite, and losing that race is **silent**: the edit
appears to succeed, the executor's step statuses are erased, and a completed
step can come back as pending — after which the executor re-runs a measurement
that already happened. Nothing reports it. The compare-and-swap below is what
turns that into a refusal and a retry.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace.client import ConcurrentEdit, ExperimentClient, WRITE_ATTEMPTS


PLAN = {
    'meta': {'current_step': 1},
    'param_sets': {'film': {'sputter_time_s': 2.0, 'resolution_px': 64}},
    'positions': {'coordinates': []},
    'commands': [],
    'steps': [
        {'action': 'measure_now', 'status': 'completed', 'params': 'film'},
        {'action': 'pause', 'status': 'running', 'args': {'message': 'QC'}},
        {'action': 'measure_now', 'status': 'pending', 'params': 'film'},
    ],
}


@pytest.fixture
def client(tmp_path):
    write_plan(tmp_path, PLAN)
    return ExperimentClient(tmp_path)


def write_plan(tmp_path, plan):
    with open(tmp_path / 'experiment.json', 'w', encoding='utf-8') as f:
        json.dump(plan, f)


def read_plan(tmp_path):
    with open(tmp_path / 'experiment.json', encoding='utf-8') as f:
        return json.load(f)


# ── Reading ──────────────────────────────────────────────────────────

def test_status_summarises_the_plan(client):
    status = client.status()
    assert status['n_steps'] == 3
    assert status['current_step'] == 1
    assert status['steps_by_status'] == {'completed': 1, 'running': 1,
                                         'pending': 1}


def test_steps_can_be_filtered_by_status(client):
    pending = client.list_steps(status='pending')
    assert [s['index'] for s in pending] == [2]


def test_telemetry_is_absent_rather_than_an_error_before_the_first_tick(client):
    assert client.runtime_status() is None


def test_telemetry_caught_mid_write_is_absent_rather_than_an_error(client, tmp_path):
    """It is rewritten every tick, so being caught half-written is normal and
    says nothing — the next tick supersedes it."""
    (tmp_path / 'experiment_status.json').write_text('{"step": 1,', encoding='utf-8')
    assert client.runtime_status() is None


def test_an_unknown_parameter_set_names_what_is_there(client):
    with pytest.raises(KeyError, match='film'):
        client.get_param_set('flim')


# ── The three amendments a checkpoint makes ──────────────────────────

def test_a_retune_edits_the_set_every_later_step_refers_to(client, tmp_path):
    """Steps name a set rather than carrying a copy, so one edit changes every
    step that has not run and leaves the ones that have alone. That is what
    makes an adaptive correction an edit rather than a rewrite."""
    client.update_param_set('film', sputter_time_s=1.6)

    plan = read_plan(tmp_path)
    assert plan['param_sets']['film']['sputter_time_s'] == 1.6
    assert plan['param_sets']['film']['resolution_px'] == 64, 'untouched'
    assert [s['params'] for s in plan['steps'] if 'params' in s] == \
        ['film', 'film'], 'the steps still refer to it by name'


def test_a_typo_in_a_field_name_is_refused(client):
    """Written to the file it would be ignored by the executor and look exactly
    like a change that was applied."""
    with pytest.raises(KeyError, match='sputter_tmie'):
        client.update_param_set('film', sputter_tmie=1.6)


def test_a_repeat_is_an_inserted_pending_step(client, tmp_path):
    client.insert_step(3, 'measure_now', params='film', filename='repeat_a')
    steps = read_plan(tmp_path)['steps']
    assert len(steps) == 4
    assert steps[3] == {'action': 'measure_now', 'status': 'pending',
                        'params': 'film', 'filename': 'repeat_a'}


def test_inserting_behind_the_executor_is_refused(client):
    """A step before the executor's position is never dispatched, so it would
    sit in the plan looking scheduled and silently never happen."""
    with pytest.raises(ValueError, match='never be dispatched'):
        client.insert_step(0, 'measure_now', params='film')


def test_releasing_a_checkpoint_is_a_command(client, tmp_path):
    client.send_command('continue_experiment')
    assert read_plan(tmp_path)['commands'] == ['continue_experiment']


def test_a_command_with_arguments_keeps_them(client, tmp_path):
    client.send_command('stop_measurement', reason='drift')
    assert read_plan(tmp_path)['commands'] == [{'stop_measurement':
                                                {'reason': 'drift'}}]


# ── The plan is also the record ──────────────────────────────────────

def test_a_completed_step_cannot_be_removed(client):
    """Deleting it falsifies the record while its measurement stays on disk."""
    with pytest.raises(ValueError, match='completed'):
        client.remove_step(0)


def test_a_running_step_cannot_be_removed_either(client):
    with pytest.raises(ValueError, match='running'):
        client.remove_step(1)


def test_a_pending_step_can_be_removed(client, tmp_path):
    client.remove_step(2)
    assert len(read_plan(tmp_path)['steps']) == 2


# ── The lost update ──────────────────────────────────────────────────

def test_a_write_over_a_changed_file_is_refused_and_retried(client, tmp_path):
    """The executor's rewrite lands between this client's read and its write.

    Without the compare-and-swap the stale copy goes back, erasing the
    executor's step statuses — and a completed step returning as pending makes
    the executor re-run a measurement that already happened.
    """
    original_read = client._read
    landed = []

    def executor_writes_in_between():
        data = original_read()
        if not landed:
            landed.append(True)
            moved_on = json.loads(json.dumps(PLAN))
            moved_on['steps'][1]['status'] = 'completed'
            moved_on['meta']['current_step'] = 2
            write_plan(tmp_path, moved_on)
        return data

    client._read = executor_writes_in_between
    client.update_param_set('film', sputter_time_s=1.6)

    plan = read_plan(tmp_path)
    assert plan['param_sets']['film']['sputter_time_s'] == 1.6, \
        'the edit should have been re-applied to the fresh file'
    assert plan['steps'][1]['status'] == 'completed', \
        "the executor's update survived"
    assert plan['meta']['current_step'] == 2


def test_an_append_happens_exactly_once_across_a_retry(client, tmp_path):
    """The retry re-runs the whole method, so an appending edit could append
    twice if the refused write had landed. It did not, which is what makes
    re-running safe rather than clever."""
    original_read = client._read
    landed = []

    def executor_writes_in_between():
        data = original_read()
        if not landed:
            landed.append(True)
            write_plan(tmp_path, dict(PLAN, commands=[]))
        return data

    client._read = executor_writes_in_between
    client.send_command('continue_experiment')
    assert read_plan(tmp_path)['commands'] == ['continue_experiment']


def test_giving_up_names_the_cause(client, tmp_path):
    """A plan that never settles must fail with something actionable, not with
    the internal conflict type."""
    original_read = client._read

    def always_changes():
        data = original_read()
        write_plan(tmp_path, dict(PLAN, meta={'current_step': os.urandom(4).hex()}))
        return data

    client._read = always_changes
    with pytest.raises(RuntimeError, match='kept changing'):
        client.update_param_set('film', sputter_time_s=1.6)


def test_the_version_is_the_content_not_the_timestamp(client, tmp_path):
    """Two writes inside one filesystem timestamp tick are indistinguishable by
    time, and the plan is rewritten once per step — so that window is real. A
    same-tick rewrite with DIFFERENT content must still be caught."""
    client._read()
    version = client._read_version

    changed = json.loads(json.dumps(PLAN))
    changed['meta']['current_step'] = 99
    write_plan(tmp_path, changed)
    os.utime(tmp_path / 'experiment.json',
             (os.stat(tmp_path / 'experiment.json').st_atime,
              os.stat(tmp_path / 'experiment.json').st_mtime))

    assert client._version() != version, \
        'a content change must be visible even at an unchanged timestamp'
    with pytest.raises(ConcurrentEdit):
        client._write(changed)


def test_rewriting_identical_content_is_not_a_conflict(client, tmp_path):
    """Nothing can be lost by writing over bytes that did not change, and
    treating it as a conflict would make an idempotent edit fail."""
    data = client._read()
    write_plan(tmp_path, data)          # same content, new file
    client._write(data)                 # must not raise


def test_the_retry_budget_is_finite(client):
    assert WRITE_ATTEMPTS >= 2
