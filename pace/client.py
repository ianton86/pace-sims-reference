# -*- coding: utf-8 -*-
"""
The other side of the plan file: reading and amending a run from outside it.

The executor owns ``experiment.json`` and rewrites it from memory at every step
boundary. This is what everything else uses — a notebook, a tool an agent calls,
a person with an editor — to read where the run has got to and to change what it
does next. There is no socket and no session; the file is the protocol, which is
what makes an agent's authority over the run exactly enumerable: it is the set
of methods below.

Everything the study's checkpoints did is here. A quality-control decision to
retune becomes ``update_param_set``; a decision to repeat becomes
``insert_step``; releasing a checkpoint becomes ``send_command('continue_
experiment')``.

The concurrency contract
------------------------
Two writers, no lock, and the failure is silent in both directions. The half
that lives here is the **editor overwriting the executor**.

Every mutating method below is read → change → write, and the executor rewrites
the same file wholesale in between. A write that lands in that window makes this
copy stale, so writing it back erases the executor's update — step statuses, the
measurement counter — and can resurrect a completed step as pending, because the
executor rebuilds its queue from what it reads.

So a write here is a **compare-and-swap against a hash of the file's bytes**,
and a refused write is retried by re-running the whole method. That is safe
rather than clever: every method begins by reading, and expresses its change in
terms of the caller's arguments rather than of the values just read — so even
``send_command``, which appends, appends exactly once to the freshly-read list,
since the refused write never landed.

**The version is the content, not the modification time.** Two writes inside one
filesystem timestamp tick are indistinguishable by time, and this file is
rewritten once per step, so that window is real rather than theoretical. The
hash is taken from the same bytes that were parsed, so a write landing between
the parse and the stamp cannot be mistaken for the version in hand — and a
rewrite with identical content is correctly *not* a conflict, since nothing can
be lost by writing over bytes that did not change.

**The asymmetry with the executor is deliberate.** It writes unconditionally,
because it is the authority on the run's own state and must never be blocked by
an edit; it protects the other direction by draining an unread external change
before exporting. An editor is never the authority, so it can be asked to retry.
Neither mechanism is a lock: each closes the window between a read and a write
in the *other* process, not the one inside the rename, and nothing serialises
two editors against each other.
"""

import functools
import hashlib
import json
import os
from pathlib import Path


WRITE_ATTEMPTS = 4


class ConcurrentEdit(RuntimeError):
    """The executor wrote the plan between this client's read and its write."""


def retries_on_concurrent_edit(method):
    """Re-run a read-modify-write method when the plan changed underneath it."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        for attempt in range(WRITE_ATTEMPTS):
            try:
                return method(self, *args, **kwargs)
            except ConcurrentEdit:
                if attempt == WRITE_ATTEMPTS - 1:
                    raise RuntimeError(
                        f'{method.__name__}: the plan file kept changing under '
                        f'this edit ({WRITE_ATTEMPTS} attempts). The executor '
                        f'is writing unusually often — retry in a moment.'
                    ) from None
        return None
    return wrapper


class ExperimentClient:
    """Read and amend a run through its plan file.

    Parameters
    ----------
    path : str or Path
        The experiment directory — where ``experiment.json`` lives.
    """

    def __init__(self, path):
        self.json_path = Path(path) / 'experiment.json'
        self.status_path = Path(path) / 'experiment_status.json'
        self._read_version = None

    # ── Reading ──────────────────────────────────────────────────────

    def runtime_status(self):
        """The executor's telemetry, or ``None`` if it has not written any.

        A separate file from the plan, and read separately, because it is
        rewritten every tick: sharing one file would race a status line against
        an agent's edit of the plan.
        """
        if not self.status_path.exists():
            return None
        try:
            with open(self.status_path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            # Being caught mid-write is normal at this rate and says nothing;
            # the next tick supersedes it.
            return None

    def status(self):
        """A summary of the plan: where the run is, and what is left."""
        data = self._read()
        steps = data.get('steps', [])
        counts = {}
        for step in steps:
            counts[step.get('status', 'pending')] = counts.get(
                step.get('status', 'pending'), 0) + 1
        return {
            'current_step': data.get('meta', {}).get('current_step'),
            'n_steps': len(steps),
            'steps_by_status': counts,
            'param_sets': sorted(data.get('param_sets', {})),
            'meta': data.get('meta', {}),
        }

    def list_steps(self, status=None):
        """Every step, or only those with the given status."""
        steps = self._read().get('steps', [])
        out = []
        for i, step in enumerate(steps):
            if status is None or step.get('status') == status:
                out.append(dict(step, index=i))
        return out

    def get_step(self, index):
        steps = self._read().get('steps', [])
        try:
            return dict(steps[index], index=index)
        except IndexError:
            raise IndexError(f'No step {index}; the plan has {len(steps)}'
                             ) from None

    def list_param_sets(self):
        return sorted(self._read().get('param_sets', {}))

    def get_param_set(self, name):
        sets = self._read().get('param_sets', {})
        try:
            return sets[name]
        except KeyError:
            raise KeyError(f'No parameter set {name!r}; the plan has '
                           f'{sorted(sets)}') from None

    # ── Amending ─────────────────────────────────────────────────────

    @retries_on_concurrent_edit
    def update_param_set(self, name, **changes):
        """Change fields of a named parameter set.

        This is what a retune decision becomes. Steps refer to a set by name, so
        one edit here changes every step that has not run yet — and leaves the
        ones that have alone, which is what makes the correction expressible as
        an edit rather than a rewrite of the plan.

        An unknown field is refused rather than added: a typo would otherwise be
        written to the file, ignored by the executor, and look exactly like a
        change that was applied.
        """
        data = self._read()
        sets = data.setdefault('param_sets', {})
        if name not in sets:
            raise KeyError(f'No parameter set {name!r}; the plan has '
                           f'{sorted(sets)}')
        unknown = [k for k in changes if k not in sets[name]]
        if unknown:
            raise KeyError(
                f'{name} has no field(s) {unknown}; it has '
                f'{sorted(sets[name])}')
        sets[name].update(changes)
        self._write(data)
        return sets[name]

    @retries_on_concurrent_edit
    def insert_step(self, index, action, params=None, args=None, **fields):
        """Insert a pending step. This is what a repeat decision becomes.

        Inserting into the part of the plan that has already run is refused: a
        step before the executor's position is never dispatched, so it would sit
        in the file looking scheduled and silently never happen.
        """
        data = self._read()
        steps = data.setdefault('steps', [])
        current = data.get('meta', {}).get('current_step', 0) or 0
        if index <= current and steps:
            raise ValueError(
                f'Cannot insert at {index}: the executor is on step {current}, '
                f'so a step there would never be dispatched. Insert after it.')
        step = {'action': action, 'status': 'pending'}
        if params is not None:
            step['params'] = params
        if args:
            step['args'] = args
        step.update(fields)
        steps.insert(index, step)
        self._write(data)
        return step

    @retries_on_concurrent_edit
    def remove_step(self, index):
        """Remove a **pending** step.

        Removing one that has run or is running is refused: the plan is also the
        run's record, and deleting a completed step falsifies it — the
        measurement still happened and its data is still on disk.
        """
        data = self._read()
        steps = data.get('steps', [])
        try:
            step = steps[index]
        except IndexError:
            raise IndexError(f'No step {index}; the plan has {len(steps)}'
                             ) from None
        if step.get('status') != 'pending':
            raise ValueError(
                f'Step {index} is {step.get("status")}, not pending. The plan '
                f'is the run\'s record; removing a step that ran would falsify '
                f'it while its data stays on disk.')
        removed = steps.pop(index)
        self._write(data)
        return removed

    @retries_on_concurrent_edit
    def update_step(self, index, **fields):
        """Change fields of a pending step, refusing the same way."""
        data = self._read()
        steps = data.get('steps', [])
        try:
            step = steps[index]
        except IndexError:
            raise IndexError(f'No step {index}; the plan has {len(steps)}'
                             ) from None
        if step.get('status') != 'pending':
            raise ValueError(f'Step {index} is {step.get("status")}, not pending')
        step.update(fields)
        self._write(data)
        return step

    @retries_on_concurrent_edit
    def send_command(self, name, **kwargs):
        """Append an immediate command to the executor's inbox.

        The executor drains and clears these on its next tick, and acknowledges
        each by name in the telemetry file — including rejecting an unknown one,
        which is the only way a sender learns its command was not understood.
        """
        data = self._read()
        commands = data.setdefault('commands', [])
        commands.append({name: kwargs} if kwargs else name)
        self._write(data)
        return name

    # ── File access ──────────────────────────────────────────────────

    def _version(self, raw=None):
        """A hash of the file's bytes — see the module docstring."""
        if raw is None:
            raw = self.json_path.read_bytes()
        return hashlib.sha256(raw).hexdigest()

    def _read(self):
        """Read the plan, remembering the version read.

        Hashed from the SAME bytes that are parsed, so a write landing between
        the parse and the stamp cannot be mistaken for the version in hand.
        """
        if not self.json_path.exists():
            raise FileNotFoundError(f'No experiment.json at {self.json_path}')
        raw = self.json_path.read_bytes()
        data = json.loads(raw.decode('utf-8'))
        self._read_version = self._version(raw)
        return data

    def _write(self, data):
        """Write atomically, refusing to overwrite a version we did not read.

        Checked immediately before the rename, so the only window it cannot see
        is the rename itself.
        """
        if self._read_version is not None:
            try:
                if self._version() != self._read_version:
                    raise ConcurrentEdit(str(self.json_path))
            except OSError:
                pass          # cannot read it — fall through and write
        tmp = str(self.json_path) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(self.json_path))
        try:
            self._read_version = self._version()
        except OSError:
            self._read_version = None
