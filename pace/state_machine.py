# -*- coding: utf-8 -*-
"""
The execution state machine: one command at a time, amendable while it runs.

``Executor.run()`` is the whole of the run's control flow. Everything that
touches hardware is behind an injected **driver** (see ``pace.driver``), so the
loop below can be run end to end against a simulator — which is what the replay
harness and the test-suite do.

The tick
--------
Each pass of the loop does the same four things in the same order:

1. **Poll the plan file** for external edits and for the agent's immediate
   commands. This is what makes a run amendable rather than merely observable:
   a step inserted while the run is going is picked up here.
2. **Dispatch one command**, or idle-wait if the queue has drained and the run
   is persistent.
3. **Record the outcome** — the plan back to ``experiment.json``, telemetry to
   ``experiment_status.json``.
4. **Let the driver do its per-tick work** (sampling a controller, refreshing a
   live view) and pace the loop.

The handler protocol
--------------------
A handler returns ``NextAction.IDLE`` to be called again on the next tick, or
``NextAction.NEXT`` to advance. That two-value protocol is how a long operation
stays non-blocking without the loop growing any notion of concurrency: a
measurement handler submits its work somewhere and returns ``IDLE`` until it is
finished. The consequence to hold onto is that **no handler may block**, because
the same loop is also polling for the agent's stop command and writing the
telemetry a monitor reads. A handler that blocks for a minute is a minute in
which the run cannot be stopped.

Ending a run: three different things
------------------------------------
The distinction matters more than it looks, and conflating any two of them
loses something real.

``end_experiment_now``      Immediate. Checked at the **top** of the tick, so it
                            fires mid-command, and it asks the driver to abort
                            whatever is in flight.
``end_experiment_after``    Graceful. Checked only at a **command boundary**, so
                            it never interrupts an acquisition, and the steps
                            that have not run stay pending in the file.
``stop_measurement``        Ends the current measurement *command* only; the run
                            then continues to the next step.

Scope
-----
Two things in the production engine are deliberately absent. The study-application
dispatch (a registered application owning its own multi-step actions, and the
extra ``NextAction`` values that jump to its markers) is out of scope for the
published subset. So is the hardware-recovery path a failed adjustment takes,
which reaches into the vendor UI. See the repository README.
"""

import datetime
import time
import traceback
from collections import deque

from .sequence import Command, NextAction


# For these commands the plan names a parameter set and the executor resolves it
# to the set's contents before dispatch, at the argument index given here. The
# driver is handed measurement parameters and never has to know that the plan
# refers to them by name — which is what keeps "amend the set, every step that
# has not run picks it up" an executor property rather than a driver one.
_PARAMS_ARG = {'measure_now': 0, 'time_series': 1}


class Executor:
    """Runs a plan, one command per tick, against an injected driver.

    Parameters
    ----------
    store : ExperimentStateFile
        The plan file. Read for external edits, written at each step boundary.
    driver : object
        Supplies a method per hardware-touching command — ``measure_now``,
        ``time_series``, ``set_temperature``, ``move_stage``, ``shutdown`` —
        each returning a ``NextAction``. Three further methods are optional and
        feature-detected: ``abort()`` (stop what is in flight, for an immediate
        end), ``status()`` (a dict merged into the telemetry file) and
        ``on_tick()`` (per-tick housekeeping). ``pace.driver.base`` declares the
        contract; anything satisfying it will do.
    param_sets : dict
        ``{name: MeasurementParams}``, the sets steps refer to by name.
    persistent : bool
        What happens when the queue drains — see ``run``.
    tick_interval : float
        Seconds to sleep per tick when the driver does not pace the loop itself.
        Set to 0 in tests.
    """

    def __init__(self, store, driver, *, param_sets=None, persistent=False,
                 log=None, tick_interval=0.5, meta=None):
        self.store = store
        self.driver = driver
        self.param_sets = dict(param_sets or {})
        self.persistent = persistent
        self.tick_interval = tick_interval
        self.meta = dict(meta or {})

        self.queue = []
        self.index = 0
        self.active_param_set = 'default'

        self._log_func = log
        self._recent_events = deque(maxlen=20)
        self._last_command_result = None

        # Run-control flags. Each is read in exactly one place in `run`; the
        # comments there are the specification.
        self._terminate = False
        self._stop_after_current = False
        self.stop_measurement_requested = False
        self._paused = False
        self._pause_entered = False
        self._persistent_idle = False
        self._abort_reason = None

        self._wait_start = None
        self._draining = False

    # ── Plan ─────────────────────────────────────────────────────────

    def load(self, commands):
        """Set the queue before the run starts."""
        self.queue = list(commands)
        self.index = 0

    def insert(self, commands):
        """Insert commands just after the one running, and record the plan.

        This is the **single choke point** for every runtime insertion — a live
        command, an agent's decision, a study application — which is what makes
        it the right place to stamp provenance. Commands inserted here are
        ``origin='auto'``, and the safety envelope resolves an out-of-range
        value differently for those than for an operator's own: clamped and
        logged rather than rejected, because aborting an unattended run is worse
        than running at the edge of the declared range. A caller that has
        already set an explicit origin is left alone.
        """
        for cmd in commands:
            if getattr(cmd, 'origin', 'plan') == 'plan':
                cmd.origin = 'auto'
        self.queue[self.index + 1:self.index + 1] = commands
        self.export()

    def params_for(self, name):
        """Resolve a parameter-set name; ``None`` means the active set."""
        if name is None:
            name = self.active_param_set
        if not isinstance(name, str):
            return name          # an inline set, passed through as-is
        return self.param_sets.get(name)

    # ── The loop ─────────────────────────────────────────────────────

    def run(self):
        """Execute until the queue drains or the run is told to end.

        One loop serves both run modes; they differ only in what happens when
        the queue empties. With ``persistent=False`` the run ends there, which
        is the plain batch behaviour. With ``persistent=True`` it **idle-waits**
        instead — holding the instrument where it is, still polling the file,
        still writing telemetry — until an ``end_experiment_*`` command arrives.
        That is the mechanism a checkpoint gate is built on: the run reaches a
        decision point and holds, rather than running on or tearing down, while
        whatever is steering it decides what happens next.
        """
        while True:
            self.poll()

            # Immediate end is checked here, before anything else, so it takes
            # effect mid-command rather than at the next boundary.
            if self._terminate:
                break

            if self.index < len(self.queue):
                self._persistent_idle = False
                if not self._run_one():
                    break
            elif self.persistent:
                if self._stop_after_current:
                    break
                self._persistent_idle = True
                self._idle()
            else:
                break

        # Record the terminal state on the way out, so the last thing on disk is
        # where the run actually stopped rather than the last boundary it
        # happened to cross. Guarded, for the same reason the abort path is: this
        # is the last write the run will make, and failing it must not turn a
        # completed run into an exception raised at the caller.
        self._record_state()
        return self._abort_reason

    def _run_one(self):
        """Dispatch the command at ``index``. Returns False to end the run."""
        prev_index = self.index
        command = self.queue[self.index]

        # A failing handler must not escape the loop. Letting it propagate would
        # skip the caller's whole teardown — the data store never closed, the log
        # never flushed — turning a failed step into a corrupted record of the
        # steps that had already succeeded. So the run still ends, but through
        # the normal exit path, with the cause recorded where a monitor can see
        # it. This is a backstop under the driver's own error handling, not a
        # substitute for it: anything survivable should be handled below.
        #
        # Deciding the next index is INSIDE the guard, not after it. A handler
        # returning something that is not a ``NextAction`` is a handler failure
        # like any other, and leaving that decision outside would let exactly
        # that case escape the loop — past the backstop written to stop it.
        try:
            action = self._dispatch(command)
            next_index = self._next_index(action)
        except Exception as exc:
            self._log(f'FATAL: command "{command.name}" failed: '
                      f'{type(exc).__name__}: {exc}')
            self._log(traceback.format_exc())
            self._log('Aborting run (through the normal exit path, so the '
                      'record and the logs are flushed)')
            self._abort_reason = f'{command.name}: {type(exc).__name__}: {exc}'
            self._record_state()
            return False

        self.index = next_index

        if self.index != prev_index:
            self.stop_measurement_requested = False   # scoped to one command
            self.export()
            # Graceful end fires here and nowhere else — at a boundary, with
            # the remaining steps left pending in the file.
            if self._stop_after_current:
                return False

        self._tick()
        return True

    def _record_state(self):
        """Write both records, each guarded independently, telemetry first.

        Independently because they fail for different reasons, and telemetry
        first because it is what a monitor polls: a failing plan export must not
        be able to suppress the one message that says the run has stopped.
        """
        for record, what in ((self.write_status, 'status'),
                             (self.export, 'experiment.json')):
            try:
                record()
            except Exception as exc:
                self._log(f'Warning: could not record the run state to '
                          f'{what}: {exc}')

    def _idle(self):
        """One pass of the persistent idle-wait.

        Telemetry keeps being written and the driver keeps ticking: a run that
        is holding at a checkpoint still has a heater running, and a trace that
        stopped when the queue drained would have a hole in it exactly where the
        decision was being made.
        """
        self._tick()

    def _tick(self):
        """Per-tick housekeeping, shared by the running and idle branches."""
        on_tick = getattr(self.driver, 'on_tick', None)
        if on_tick is not None:
            on_tick()
        self.write_status()
        if self.tick_interval:
            time.sleep(self.tick_interval)

    def _dispatch(self, command):
        """Resolve a command to its handler and call it.

        Executor-owned handlers win over the driver's. Only two are: ``pause``
        and ``wait_a_while`` are pure control flow with nothing to actuate, and
        a driver that had to implement them would be implementing the loop.
        """
        own = getattr(self, '_cmd_' + command.name, None)
        if own is not None:
            return own(*command.args)

        handler = getattr(self.driver, command.name, None)
        if handler is None:
            raise RuntimeError(
                f'No handler for command "{command.name}": the executor does '
                f'not own it and the driver ({type(self.driver).__name__}) '
                f'does not implement it')

        args = list(command.args)
        idx = _PARAMS_ARG.get(command.name)
        if idx is not None and len(args) > idx:
            args[idx] = self.params_for(args[idx])
        return handler(*args)

    def _next_index(self, action):
        if action == NextAction.IDLE:
            return self.index
        if action == NextAction.NEXT:
            return self.index + 1
        raise RuntimeError(f'Handler returned {action!r}, which is not a NextAction')

    # ── Executor-owned command handlers ──────────────────────────────

    def _cmd_pause(self, message=''):
        """Hold the run here until ``continue_experiment`` arrives.

        The loop keeps ticking throughout — polling the file, writing telemetry
        — which is the point: an agent pauses precisely so that it can look at
        what has been acquired and amend the plan before releasing it.

        Two flags rather than one, because "am I holding?" and "have I entered
        this pause?" are different questions: the release clears the first, and
        without the second the handler would re-enter on the next tick and log
        a fresh pause over the release it just processed.
        """
        if not self._pause_entered:
            self._pause_entered = True
            self._paused = True
            self._log(f'Paused: {message}' if message else 'Paused')
        if self._paused:
            return NextAction.IDLE
        self._pause_entered = False
        self._log('Resumed from pause')
        return NextAction.NEXT

    def _cmd_wait_a_while(self, seconds):
        """Wait, without blocking the loop — one tick at a time."""
        if self._wait_start is None:
            self._log(f'Waiting for {seconds:.0f} s')
            self._wait_start = time.time()
            return NextAction.IDLE
        if time.time() - self._wait_start < seconds:
            return NextAction.IDLE
        self._wait_start = None
        self._log('Wait complete')
        return NextAction.NEXT

    def _cmd_change_measurement_params(self, name):
        """Make a named parameter set the default for later measurements."""
        if name in self.param_sets:
            self.active_param_set = name
            self._log(f'Measurement parameters switched to "{name}"')
        else:
            self._log(f'Unknown parameter set "{name}" — keeping '
                      f'"{self.active_param_set}"')
        return NextAction.NEXT

    # ── Live commands ────────────────────────────────────────────────
    #
    # The agent's inbox. Each name here is what may appear in the plan file's
    # `commands` list; anything else is rejected by name, with the valid set
    # named in the rejection, because a silently ignored stop command is the
    # worst failure this interface has.

    _live_commands = {
        'stop_measurement':            '_live_stop_measurement',
        'stop_measurement_with_pause': '_live_stop_measurement_with_pause',
        'pause_after':                 '_live_pause_after',
        'end_experiment_now':          '_live_end_experiment_now',
        'end_experiment_after':        '_live_end_experiment_after',
        'continue_experiment':         '_live_continue_experiment',
    }

    def _live_end_experiment_now(self, **kwargs):
        """End immediately, aborting whatever is in flight."""
        self._log('Live command: ending the run now')
        abort = getattr(self.driver, 'abort', None)
        if abort is not None:
            abort()
        self.index = len(self.queue)
        self._terminate = True      # exits the persistent idle-wait too

    def _live_end_experiment_after(self, **kwargs):
        """End at the next command boundary, leaving the rest pending."""
        self._log('Live command: ending the run after the current command')
        self._stop_after_current = True

    def _live_stop_measurement(self, **kwargs):
        """End the current measurement command, then continue the run.

        Gated on a measurement actually being the current command: raising the
        flag against a temperature step would leave it set for whatever
        measurement came next, stopping a measurement nobody asked to stop.
        """
        current = self.queue[self.index] if self.index < len(self.queue) else None
        if current is not None and current.name in ('measure_now', 'time_series'):
            self.stop_measurement_requested = True
            # Told to the driver rather than left as a flag for it to notice:
            # the driver owns the acquisition, and a flag it has to poll is one
            # it can miss. The flag stays because it is what the telemetry file
            # reports and what scopes the request to this one command.
            stop = getattr(self.driver, 'stop_measurement', None)
            if stop is not None:
                stop()
            self._log('Live command: stopping the current measurement')
        else:
            self._log('Live command: stop_measurement ignored '
                      '(no measurement command is running)')

    def _live_stop_measurement_with_pause(self, **kwargs):
        """Stop the measurement and hold before the next command."""
        self._live_stop_measurement()
        self.insert([Command('pause',
                             ['measurement stopped — awaiting continue_experiment'])])

    def _live_pause_after(self, **kwargs):
        """Hold once the current command completes."""
        self._log('Live command: will pause after the current command')
        self.insert([Command('pause',
                             ['paused after the current command — '
                              'awaiting continue_experiment'])])

    def _live_continue_experiment(self, **kwargs):
        """Release a pause."""
        if self._paused:
            self._log('Live command: continuing from pause')
            self._paused = False
        else:
            self._log('Live command: continue_experiment ignored (not paused)')

    # ── The plan file ────────────────────────────────────────────────

    def poll(self):
        """Read an external edit and dispatch any immediate commands."""
        result = self.store.import_updates(self.param_sets)
        if result is None:
            return

        if result['param_sets']:
            self.param_sets = result['param_sets']
            self._log('Plan file: parameter sets updated externally')

        # Rebuild the not-yet-run part of the queue from the file. Completed and
        # running commands are kept from memory: the file's copy of a running
        # step cannot be more current than ours, and adopting it would restart
        # the step in progress.
        if result['pending_commands']:
            self.queue = (self.queue[:self.index + 1]
                          + list(result['pending_commands']))
            self._log(f'Plan file: {len(result["pending_commands"])} pending '
                      f'steps updated')

        for entry in result.get('commands', []):
            self._dispatch_live(entry)

    def _dispatch_live(self, entry):
        """Run one immediate command from the inbox and acknowledge it.

        The acknowledgement is the deliverable as much as the effect is: the
        sender has no return channel other than the telemetry file, so a command
        that was rejected has to say so there, by name, or it is
        indistinguishable from one that was never read.
        """
        if isinstance(entry, str):
            name, kwargs = entry, {}
        elif isinstance(entry, dict):
            name = next(iter(entry))
            kwargs = entry[name] if isinstance(entry[name], dict) else {}
        else:
            self._log(f'Invalid command format: {entry!r}')
            return

        handler_name = self._live_commands.get(name)
        if handler_name is None:
            valid = ', '.join(sorted(self._live_commands))
            self._log(f'Unknown command: {name}. Valid commands: {valid}')
            self._acknowledge(name, 'unknown',
                              f'unknown command; valid: {valid}')
            return

        self._log(f'Plan file command: {name}')
        try:
            getattr(self, handler_name)(**kwargs)
            self._acknowledge(name, 'ok')
        except Exception as exc:
            self._log(f'Command {name} failed: {exc}')
            self._acknowledge(name, 'error', str(exc))

    def _acknowledge(self, name, status, error=None):
        self._last_command_result = {
            'command': name, 'status': status,
            'time': datetime.datetime.now().isoformat(),
        }
        if error is not None:
            self._last_command_result['error'] = error

    def export(self):
        """Write the plan, applying any unread external edit first.

        The drain is the engine's half of the concurrency contract in
        ``pace.state_store``. This method writes the whole file from memory, so
        an edit that landed after the last poll would be destroyed — and
        destroyed *invisibly*, because our own write refreshes the modification
        time the poll compares against, so the change is never seen again. It
        presents as "my edit did nothing": an inserted step that never runs, a
        parameter set that reverts.

        Reentrant by construction, and the flag makes that explicit rather than
        load-bearing: draining dispatches live commands, a live command may
        ``insert``, and ``insert`` exports again.
        """
        if not self._draining and self.store.unread_external_change():
            self._draining = True
            try:
                self._log('Plan file: external edit found before export — '
                          'applying it first so the export cannot overwrite it')
                self.poll()
            except Exception as exc:
                # Never let this take down an export: losing the record of the
                # run is worse than losing one external edit.
                self._log(f'Warning: could not apply the external edit before '
                          f'export ({exc}); it may be overwritten')
            finally:
                self._draining = False

        self.store.export(self.queue, self.index, self.param_sets,
                          meta={**self.meta, 'persistent': self.persistent})

    def write_status(self):
        """Write the telemetry file a monitor polls."""
        status = {
            'step': self.index,
            'n_steps': len(self.queue),
            'current_command': (self.queue[self.index].name
                                if self.index < len(self.queue) else None),
            'paused': self._paused,
            'stop_measurement_requested': self.stop_measurement_requested,
            'persistent': self.persistent,
            'persistent_idle': self._persistent_idle,
            'active_param_set': self.active_param_set,
            'recent_events': list(self._recent_events),
        }
        driver_status = getattr(self.driver, 'status', None)
        if driver_status is not None:
            status.update(driver_status())
        if self._last_command_result is not None:
            status['last_command'] = self._last_command_result
        # Set only when a handler failed, so a monitor can tell a clean finish
        # from an abort. A run that ended normally carries neither key.
        if self._abort_reason:
            status['aborted'] = True
            status['abort_reason'] = self._abort_reason
        self.store.update_runtime_status(status)

    # ── Logging ──────────────────────────────────────────────────────

    def _log(self, text):
        """Log, and keep the line in the ring the telemetry file publishes.

        Both, always: a line that reached the log file but not the status file
        is invisible to whatever is steering the run, and the lines that matter
        most — a rejected command, an abort — are exactly the ones it needs.
        """
        stamped = f'{datetime.datetime.now().strftime("%H:%M:%S")} {text}'
        self._recent_events.append(stamped)
        if self._log_func is not None:
            self._log_func(text)
