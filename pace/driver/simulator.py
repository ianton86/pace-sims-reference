# -*- coding: utf-8 -*-
"""
A driver with no hardware behind it, so the whole lifecycle runs anywhere.

What this is for: exercising the orchestration end to end — a plan runs, a
measurement takes several ticks, a dynamic stop fires, a checkpoint holds, an
agent amends the plan and releases it — with nothing installed and nothing at
risk. Every test in this repository that involves a run uses it, and so does the
demo notebook.

**What it is not: a model of the instrument.** The profiles it emits have the
shape a layered sample gives — a capping layer, a film carrying a marker, a
substrate whose signal rises underneath — because a stop condition needs
something to trigger on and a quality-control rule needs something to judge.
The numbers are not physical and no conclusion should be drawn from them. The
paper's claims are checked against the *logged* metrics of the real study in
``replay/``, never against this.

It is deliberately deterministic: seed it and two runs are identical, which is
what makes a test that asserts on a decision meaningful.
"""

import math
import random

from ..sequence import NextAction
from ..stop_conditions import dynamic_stop_scan
from .base import InstrumentDriver


class Measurement:
    """One acquisition's record: what was asked for, and what came out."""

    def __init__(self, filename, sample, position, params):
        self.filename = filename
        self.sample = sample
        self.position = position
        self.params = params
        self.temperature = None
        self.profile = {}        # label → per-scan intensity
        self.scans = 0
        self.stopped_by = None   # 'scan_limit' | 'dynamic' | 'operator' | 'abort'

    def __repr__(self):
        return (f'<Measurement {self.filename} {self.scans} scans, '
                f'stopped by {self.stopped_by}>')


class SimulatedInstrument(InstrumentDriver):
    """A stand-in instrument. See the module docstring for what it is not.

    Parameters
    ----------
    seed : int
        Fixes the counting noise, so a run is reproducible.
    substrate_scan : int
        Which scan the substrate marker rises at — i.e. how thick the film is,
        in scans. This is what a dynamic stop condition triggers on.
    settle_ticks : int
        How many ticks a temperature change takes to arrive. A stand-in for a
        thermal time constant, not a measurement of one.
    """

    def __init__(self, *, seed=0, substrate_scan=40, settle_ticks=5,
                 log=None):
        self.substrate_scan = substrate_scan
        self.settle_ticks = settle_ticks
        self._rng = random.Random(seed)
        self._log_func = log

        self.position = {'x': 0.0, 'y': 0.0, 'z': 0.0}
        self.temperature = 25.0
        self.setpoint = 25.0

        self.measurements = []
        self.ticks = 0
        self.shutdown_complete = False

        self._acquiring = None       # the Measurement in progress
        self._stop_requested = False
        self._settling = 0

    # ── Acquisition ──────────────────────────────────────────────────

    def measure_now(self, params, position, filename, sample):
        if self._acquiring is None:
            self._acquiring = Measurement(
                filename or f'measurement_{len(self.measurements) + 1}',
                sample, position, params)
            self._acquiring.temperature = self.temperature
            self._log(f'Acquiring {self._acquiring.filename} '
                      f'at position {position}')
            return NextAction.IDLE

        acq = self._acquiring
        self._acquire_one_scan(acq)

        stop = self._stop_reason(acq)
        if stop is None:
            return NextAction.IDLE

        acq.stopped_by = stop
        self.measurements.append(acq)
        self._acquiring = None
        self._stop_requested = False
        self._log(f'{acq.filename}: {acq.scans} scans, stopped by {stop}')
        return NextAction.NEXT

    def _stop_reason(self, acq):
        """Why this acquisition should end, or None to keep going.

        Order matters: an operator's stop wins over the conditions, because it
        is a decision about *this* run rather than a rule that applies to every
        one of them.
        """
        if self._stop_requested:
            return 'operator'

        condition = acq.params.stop_condition
        if condition is None:
            return 'scan_limit' if acq.scans >= 1 else None

        if condition.kind == 'Dynamic' and condition.label in acq.profile:
            target = dynamic_stop_scan(acq.profile[condition.label], condition)
            if target is not None and acq.scans >= target:
                return 'dynamic'

        if condition.max_scans and acq.scans >= condition.max_scans:
            return 'scan_limit'
        return None

    def _acquire_one_scan(self, acq):
        """Append one scan's counts for every peak the parameters name."""
        acq.scans += 1
        s = acq.scans
        for i, peak in enumerate(acq.params.peaks or []):
            acq.profile.setdefault(peak.label, []).append(
                self._counts(peak, i, s, acq.params))

    def _counts(self, peak, index, scan, params):
        """A layered-sample shape, with counting noise. Not physical.

        Three components, chosen so a stop condition and a quality-control rule
        each have something real to act on:

        * the species the stop condition names rises at ``substrate_scan``;
        * the first peak carries a marker peaked in the middle of the film;
        * everything else decays gently from the surface.
        """
        depth = self.substrate_scan
        condition = params.stop_condition
        marked = condition is not None and peak.label == condition.label

        if marked:
            level = 40.0 + 4000.0 / (1.0 + math.exp(-(scan - depth) / 2.0))
        elif index == 0:
            level = 200.0 + 3000.0 * math.exp(-((scan - depth / 2.0) ** 2)
                                              / (2.0 * (depth / 5.0) ** 2))
        else:
            level = 500.0 * math.exp(-scan / (2.0 * depth)) + 20.0

        # Counting noise, so a median filter and an event count have something
        # to be robust against.
        return max(0.0, self._rng.gauss(level, math.sqrt(max(level, 1.0))))

    def stop_measurement(self):
        if self._acquiring is not None:
            self._stop_requested = True
            self._log(f'{self._acquiring.filename}: stop requested')

    def abort(self):
        if self._acquiring is not None:
            acq = self._acquiring
            acq.stopped_by = 'abort'
            self.measurements.append(acq)
            self._acquiring = None
            self._stop_requested = False
            self._log(f'{acq.filename}: aborted at scan {acq.scans}')

    # ── Everything else ──────────────────────────────────────────────

    def set_temperature(self, target, ramp_rate=None, ramp_time=None):
        if ramp_rate is not None and ramp_time is not None:
            raise ValueError('give ramp_rate or ramp_time, never both — they '
                             'over-determine the same line')
        # Bounded here as well as by the executor: a real driver's ramp writes
        # values the plan never named, and those follow the automatic rule —
        # clamped and logged, never rejected. Demonstrated on the target so the
        # contract is exercised rather than only described.
        if self.envelope is not None:
            bounded = self.envelope.check_temperature(target, clamp=True)
            if bounded != target:
                self._log(f'envelope clamped {target} °C to {bounded} °C')
                target = bounded
        if self.setpoint != target:
            self.setpoint = target
            self._settling = self.settle_ticks
            self._log(f'Setpoint {target} °C')
        if self._settling > 0:
            self._settling -= 1
            # Approach the setpoint rather than jumping to it, so a plan that
            # measures during a ramp sees a temperature that is on its way.
            self.temperature += (self.setpoint - self.temperature) / max(
                self._settling + 1, 1)
            return NextAction.IDLE
        self.temperature = self.setpoint
        return NextAction.NEXT

    def move_stage(self, axes):
        absolute = {a for a in axes if not a.startswith('d')}
        relative = {a[1:] for a in axes if a.startswith('d')}
        clash = absolute & relative
        if clash:
            raise ValueError(
                f'axis {sorted(clash)} was given both an absolute and a '
                f'relative target; commanding one and verifying the other '
                f'fails against a target that was never aimed at')
        for axis, value in axes.items():
            if axis.startswith('d'):
                self.position[axis[1:]] = self.position.get(axis[1:], 0.0) + value
            else:
                self.position[axis] = value
        self._log(f'Stage at {self.position}')
        return NextAction.NEXT

    def shutdown(self):
        self.shutdown_complete = True
        self._log('Instrument shut down')
        return NextAction.NEXT

    def on_tick(self):
        self.ticks += 1

    def status(self):
        acq = self._acquiring
        return {
            'temperature': round(self.temperature, 1),
            'setpoint': round(self.setpoint, 1),
            'stage': dict(self.position),
            'measuring': acq.filename if acq else None,
            'scan': acq.scans if acq else 0,
            'measurements_completed': len(self.measurements),
        }

    def _log(self, text):
        if self._log_func is not None:
            self._log_func(f'[simulator] {text}')
