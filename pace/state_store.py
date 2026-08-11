# -*- coding: utf-8 -*-
"""
``experiment.json`` — the run's state, as a file both sides can edit.

This is the interface between the executor and whatever is steering it. The
executor writes the whole file from memory at each step boundary; an external
agent reads it, amends parameter sets or pending steps, and appends immediate
commands. There is no socket, no session and no lock: the file is the protocol.

Layout
------
Five top-level keys.

``meta``          run identity and configuration; ``current_step`` is the index
                  the executor is on.
``param_sets``    ``{name: MeasurementParams}``. Steps refer to these by name,
                  so amending one here changes every step not yet run.
``positions``     the measurement positions and the stage rotation they were
                  registered at.
``commands``      the **agent inbox**. Exported empty; an external writer
                  appends immediate commands, the executor drains and clears
                  them. This is the only key the executor reads back as
                  instructions rather than as state.
``steps``         the ordered plan, each with ``status`` in
                  ``pending`` / ``running`` / ``completed``.

Runtime telemetry goes to a **separate** file, ``experiment_status.json``,
written by the executor only. Keeping it out of ``experiment.json`` is what
stops a once-per-tick status write from racing an agent's edit of the plan.

The concurrency contract
------------------------
Two writers, no lock, and the failure is silent in both directions.

*The executor overwriting an edit.* It writes the whole file from memory, so an
external edit landing after its last poll is destroyed — and destroyed
invisibly, because its own write then refreshes the modification time the poll
compares against, so the change is never seen. The executor therefore applies
any unread external change **before** exporting. Draining rather than merging:
applying an external change is something it already knows how to do, whereas a
merge would need a policy per key.

*An editor overwriting the executor.* An export landing between an editor's
read and its write makes that editor's copy stale, and writing it back can
resurrect a completed step as pending. The editor's write is therefore a
compare-and-swap against a hash of the file's bytes (``pace.client``).

The version is the **content**, not the modification time. Two writes inside one
filesystem timestamp tick are indistinguishable by time, and this file is
rewritten often enough for that window to be real rather than theoretical.

Neither mechanism is a lock. Each closes the window between a read and a write
in the *other* process, not the one inside ``os.replace``.
"""

import datetime
import json
import os
from pathlib import Path

from .sequence import Command, MeasurementParams, Polarity


# ── Step ↔ Command conversion ────────────────────────────────────────
#
# Two directions, and they must stay exact inverses of one another: this file
# is what a RESUMED run rebuilds its queue from and what every live edit passes
# through, so an argument carried by only one direction works from a notebook
# and is silently dropped by the file.

_CMD_TO_ACTION = {
    'time_series':               'measure_time_series',
    'measure_now':               'measure_now',
    'wait_a_while':              'wait_a_while',
    'set_temperature':           'set_temperature',
    'move_stage':                'move_stage',
    'change_measurement_params': 'change_measurement_params',
    'pause':                     'pause',
    'shutdown':                  'shutdown_instrument',
}
_ACTION_TO_CMD = {v: k for k, v in _CMD_TO_ACTION.items()}


def command_to_step(cmd, status='pending'):
    """Serialise a Command as a plan step."""
    action = _CMD_TO_ACTION.get(cmd.name, cmd.name)
    step = {'action': action, 'status': status}
    args = cmd.args or []

    if action == 'measure_now':
        # [params, position, filename, sample]
        if args and args[0] is not None:
            p = args[0]
            step['params'] = p if isinstance(p, str) else '_inline'
        if len(args) > 1 and args[1] is not None:
            step['position'] = args[1]
        if len(args) > 2 and args[2] is not None:
            step['filename'] = args[2]
        if len(args) > 3 and args[3] is not None:
            step['sample'] = args[3]

    elif action == 'measure_time_series':
        # [times_seconds, params, position, filename, sample] — minutes in the
        # file, seconds internally: the file is written for a human to edit.
        if args:
            step['args'] = {'times_min': [t / 60 for t in args[0]]}
        if len(args) > 1 and args[1] is not None:
            p = args[1]
            step['params'] = p if isinstance(p, str) else '_inline'
        if len(args) > 2 and args[2] is not None:
            step['position'] = args[2]
        if len(args) > 3 and args[3] is not None:
            step['filename'] = args[3]
        if len(args) > 4 and args[4] is not None:
            step['sample'] = args[4]

    elif action == 'set_temperature':
        # The ramp arguments must travel with the step or they are lost.
        # Recording only the target would turn every remaining ramp into an
        # instant setpoint change on resume — silently, and on the one
        # parameter whose whole point is the rate at which it moves.
        step['args'] = {'temperature': args[0]}
        if len(args) > 1 and args[1] is not None:
            step['args']['ramp_rate'] = args[1]
        if len(args) > 2 and args[2] is not None:
            step['args']['ramp_time'] = args[2]

    elif action == 'wait_a_while':
        step['args'] = {'time': args[0]}

    elif action == 'move_stage':
        step['args'] = args[0] if args and isinstance(args[0], dict) else {}

    elif action == 'change_measurement_params':
        if args and args[0] is not None:
            p = args[0]
            step['params'] = p if isinstance(p, str) else '_inline'

    elif action == 'pause':
        if args and args[0]:
            step['args'] = {'message': args[0]}

    else:
        # An action this build does not model. Carried through unchanged rather
        # than dropped, so an extension is additive and a file written by a
        # richer build still round-trips here.
        if args:
            step['args'] = args

    return step


def step_to_command(step, param_sets=None):
    """Rebuild a Command from a plan step — the exact inverse of the above.

    Optional arguments are read with ``.get`` throughout, so a step written by
    an older build, or typed by hand with only the essentials, still resolves.
    """
    action = step['action']
    cmd_name = _ACTION_TO_CMD.get(action, action)
    args_dict = step.get('args', {}) or {}
    params_name = step.get('params')

    if action == 'measure_now':
        return Command(cmd_name, [params_name, step.get('position'),
                                  step.get('filename'), step.get('sample')])

    if action == 'measure_time_series':
        times_sec = [t * 60 for t in args_dict.get('times_min', [])]
        return Command(cmd_name, [times_sec, params_name, step.get('position'),
                                  step.get('filename'), step.get('sample')])

    if action == 'set_temperature':
        return Command(cmd_name, [args_dict['temperature'],
                                  args_dict.get('ramp_rate'),
                                  args_dict.get('ramp_time')])

    if action == 'wait_a_while':
        return Command(cmd_name, [args_dict['time']])

    if action == 'move_stage':
        return Command(cmd_name, [args_dict])

    if action == 'change_measurement_params':
        return Command(cmd_name, [params_name])

    if action == 'pause':
        return Command(cmd_name, [args_dict.get('message', '')])

    if action == 'shutdown_instrument':
        return Command(cmd_name, [])

    return Command(cmd_name, args_dict if isinstance(args_dict, list) else [])


# ── The file ─────────────────────────────────────────────────────────

class ExperimentStateFile:
    """Reads and writes ``experiment.json`` for one experiment directory."""

    def __init__(self, path):
        self.json_path = Path(path) / 'experiment.json'
        self._last_mtime = 0

    # -- export --

    def export(self, comm_series, comm_idx, param_sets, meta=None,
               positions=None, stage_rotation=None, motion_start_i=0,
               completed_steps=None, resolved_filenames=None):
        """Write the whole state.

        ``completed_steps`` are steps from earlier runs of the same experiment,
        preserved ahead of the current queue so a resumed run's file still
        records everything that happened rather than only what is left.
        """
        resolved = resolved_filenames or {}

        steps = list(completed_steps or [])
        for i, cmd in enumerate(comm_series):
            if i < comm_idx:
                status = 'completed'
            elif i == comm_idx:
                status = 'running'
            else:
                status = 'pending'
            step = command_to_step(cmd, status)
            if i in resolved:
                step['measurement_files'] = resolved[i]
            steps.append(step)

        data = {
            'meta': {
                'last_updated': datetime.datetime.now().isoformat(),
                'current_step': comm_idx,
                **(meta or {}),
            },
            'param_sets': {name: mp.to_dict()
                           for name, mp in (param_sets or {}).items()},
            'positions': {
                'coordinates': positions or [],
                'stage_rotation': stage_rotation,
                'motion_start_i': motion_start_i,
            },
            'commands': [],
            'steps': steps,
        }
        self._write(data)

    # -- import --

    def check_external_update(self):
        """Has the file changed since we last read or wrote it?"""
        if not self.json_path.exists():
            return False
        return self.json_path.stat().st_mtime != self._last_mtime

    def unread_external_change(self):
        """True when somebody else wrote since we last read or wrote.

        Distinct from ``check_external_update`` in exactly one case, and it is
        the case that matters: with **no baseline** — nothing read or written
        yet — that method reports True for any existing file, because it cannot
        tell "changed" from "never seen". Here it must be False, or declaring a
        fresh experiment in a directory that already holds one would import the
        previous run's pending steps into the new one.
        """
        return bool(self._last_mtime) and self.check_external_update()

    def import_updates(self, param_sets=None):
        """Read an external edit. Returns None when nothing changed."""
        if not self.check_external_update():
            return None
        data = self._read()
        if data is None:
            return None

        commands = data.get('commands', [])
        new_param_sets = {name: MeasurementParams.from_dict(d)
                          for name, d in data.get('param_sets', {}).items()}
        pending = [step_to_command(s, new_param_sets)
                   for s in data.get('steps', [])
                   if s.get('status') == 'pending']

        # Immediate commands are consumed: clear them so the same instruction
        # is not executed again on the next poll.
        if commands:
            data['commands'] = []
            self._write(data)

        pos = data.get('positions', {})
        return {
            'param_sets': new_param_sets,
            'pending_commands': pending,
            'commands': commands,
            'positions': [tuple(p) for p in pos.get('coordinates', [])],
            'stage_rotation': pos.get('stage_rotation'),
            'motion_start_i': pos.get('motion_start_i', 0),
        }

    # -- runtime telemetry, written by the executor only --

    def update_runtime_status(self, status):
        """Write ``experiment_status.json``.

        A separate file on purpose: this is written every tick, and sharing a
        file with the agent-editable plan would race an edit against telemetry.
        A failed write is skipped rather than retried — the next tick supersedes
        it, and blocking the loop on a status line would be the wrong trade.
        """
        status = dict(status)
        status['last_tick'] = datetime.datetime.now().isoformat()
        path = self.json_path.parent / 'experiment_status.json'
        tmp = str(path) + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(status, f, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp, str(path))
        except OSError:
            pass

    # -- file I/O --

    def _write(self, data):
        """Write atomically: temp file then rename, so a reader never sees a
        half-written plan. Retried once, because a syncing client or scanner
        can hold the file open briefly."""
        tmp = str(self.json_path) + '.tmp'
        for attempt in range(2):
            try:
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, str(self.json_path))
                self._last_mtime = self.json_path.stat().st_mtime
                return
            except OSError:
                if attempt == 0:
                    import time
                    time.sleep(0.5)
                else:
                    print('Warning: could not write experiment.json (file locked)')
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    def _read(self):
        try:
            with open(self.json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._last_mtime = self.json_path.stat().st_mtime
            return data
        except (json.JSONDecodeError, OSError) as e:
            print(f'Warning: could not read experiment.json: {e}')
            return None
