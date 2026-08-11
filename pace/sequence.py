# -*- coding: utf-8 -*-
"""
The vocabulary a plan is written in: steps, parameter sets, commands.

A run is an ordered list of **steps**, authored before it starts and editable
while it runs. The executor turns each step into a **command** and dispatches
it; the state store (``pace.state_store``) is what serialises the two so an
external agent can read the plan, amend it, and see how far it has got.

Two things here carry weight beyond bookkeeping.

**A command records where it came from.** ``Command.origin`` is ``'plan'`` for a
step the operator authored and ``'auto'`` for one generated at runtime, and the
safety envelope resolves an out-of-range value differently for each: an
operator's mistake is rejected so it surfaces, an automatic one is clamped and
logged, because aborting an unattended run is worse than running at the edge of
the declared range. See ``pace.safety``.

**Parameter sets are named and reused.** A step refers to a set by name rather
than carrying a copy, so amending the set mid-run changes every step that has
not run yet — which is what makes the adaptive correction in ``pace.decisions``
expressible as an edit rather than a rewrite.

Scope
-----
This is the generic vocabulary, not the full one used on our instrument. Steps
that are meaningful only for a specific ion source, cooling stage or column
alignment procedure are omitted, along with their parameters; adding one is a
member on ``MeasurementStep`` plus a branch in ``pace.state_store``. See the
repository README for what is deliberately not published here.
"""

from dataclasses import dataclass, field, asdict, fields
from enum import Enum
from typing import Any, Dict, List, Literal, Optional


# ── Enumerations ─────────────────────────────────────────────────────

class Polarity(Enum):
    """Secondary-ion polarity. Switching it is slow, so a plan groups by it."""
    POSITIVE = 1
    NEGATIVE = 2


class NextAction(Enum):
    """What the executor should do after a command handler returns.

    The two-value protocol is the whole of the loop's control flow: a handler
    that has not finished returns ``IDLE`` and is called again on the next tick
    (this is how a long operation stays non-blocking without threads leaking
    into the loop), and one that is done returns ``NEXT``.
    """
    IDLE = 0
    NEXT = 1


class ExperimentState(Enum):
    """Which kind of acquisition the run is currently in."""
    NONE = 0
    SINGLE_MEASUREMENT = 1
    TIME_SERIES = 2


# ── Species and stop conditions ──────────────────────────────────────

@dataclass
class Peak:
    """A species to record, as a label and the mass it is integrated at.

    ``label`` is the name every later stage keys on — the reductions, the
    quality-control rules and the plan all refer to a species by this string,
    so it must match exactly wherever it appears.
    """
    label: str
    mass: float

    def to_dict(self) -> Dict[str, Any]:
        return {'label': self.label, 'mass': self.mass}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Peak':
        return cls(label=d['label'], mass=float(d['mass']))


@dataclass
class StopCondition:
    """When to stop acquiring.

    ``Static`` stops after a fixed number of scans. ``Dynamic`` watches one
    species and stops once it has crossed a threshold, with ``max_scans`` as the
    backstop — the backstop is not optional, because a dynamic condition that
    never triggers would otherwise acquire until something else intervened.

    ``trigger_count`` counts crossing EVENTS, not scans above the threshold: a
    below→above transition for ``'rise'``, above→below for ``'fall'``. Counting
    scans instead would make the condition depend on acquisition speed.
    """
    kind: Literal['Static', 'Dynamic'] = 'Static'
    max_scans: int = 100
    label: Optional[str] = None
    threshold: Optional[float] = None
    trigger: Literal['rise', 'fall'] = 'fall'
    trigger_count: int = 1
    post_scans: int = 0
    ignore_first_scans: int = 0

    def __post_init__(self):
        if self.kind == 'Dynamic':
            if self.label is None or self.threshold is None:
                raise ValueError(
                    "a Dynamic stop condition needs both `label` and "
                    "`threshold` — without them there is nothing to watch")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'StopCondition':
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ── Measurement parameters ───────────────────────────────────────────

@dataclass
class MeasurementParams:
    """One named acquisition configuration.

    Referred to by name from a step, so amending it mid-run affects every step
    that has not run yet.

    Only the parameters the published logic actually reads are modelled here.
    A production driver will have many more — beam and column settings, source
    selection, timing — and they belong behind ``pace.driver.InstrumentDriver``
    rather than in a schema the orchestration layer reasons about.
    """
    name: str = 'default'
    polarity: Polarity = Polarity.NEGATIVE

    # Geometry, in micrometres.
    analysis_field_um: float = 100.0
    sputter_field_um: float = 300.0
    resolution_px: int = 128

    # Depth profiling. `sputter_time_s` is the sputter interval between
    # analysis frames; zero means a surface measurement with no depth axis.
    sputter_time_s: float = 0.0

    # Species to record. The QC rules and reductions key on Peak.label.
    peaks: List[Peak] = field(default_factory=list)
    peak_margin: float = 0.05

    stop_condition: StopCondition = field(default_factory=StopCondition)

    def __post_init__(self):
        if isinstance(self.polarity, str):
            self.polarity = Polarity[self.polarity.upper()]
        self.peaks = [p if isinstance(p, Peak) else Peak.from_dict(p)
                      for p in self.peaks]
        if isinstance(self.stop_condition, dict):
            self.stop_condition = StopCondition.from_dict(self.stop_condition)

    @property
    def labels(self) -> List[str]:
        """Species labels, in the order the reductions return their columns."""
        return [p.label for p in self.peaks]

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'polarity': self.polarity.name.lower(),
            'analysis_field_um': self.analysis_field_um,
            'sputter_field_um': self.sputter_field_um,
            'resolution_px': self.resolution_px,
            'sputter_time_s': self.sputter_time_s,
            'peaks': [p.to_dict() for p in self.peaks],
            'peak_margin': self.peak_margin,
            'stop_condition': self.stop_condition.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'MeasurementParams':
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ── Commands ─────────────────────────────────────────────────────────

@dataclass
class Command:
    """One unit of work for the executor.

    ``origin`` is not bookkeeping. The safety envelope resolves an
    out-of-envelope value by provenance: an operator's planned value is
    **rejected**, so the mistake surfaces before it becomes a wrong experiment;
    a value generated at runtime is **clamped** to the nearest bound and logged,
    because aborting an unattended run is worse than running at the edge of a
    range the operator declared. Neither silently exceeds it.
    """
    name: str
    args: List[Any] = field(default_factory=list)
    origin: Literal['plan', 'auto'] = 'plan'


# ── Steps and sequences ──────────────────────────────────────────────

@dataclass
class MeasurementStep:
    """One entry in a plan. `name` is the action; `params` its arguments."""
    name: str
    params: Dict[str, Any] = field(default_factory=dict)

    # -- factories, one per action the executor understands --

    @classmethod
    def measure_now(cls, params: str = None, position: int = None,
                    filename: str = None, sample: str = None):
        """Acquire once at a position."""
        p = {}
        if params is not None:
            p['params'] = params
        if position is not None:
            p['position'] = int(position)
        if filename is not None:
            p['filename'] = filename
        if sample is not None:
            p['sample'] = sample
        return cls('measure_now', p)

    @classmethod
    def measure_time_series(cls, times_min: List[float], params: str = None,
                            position: int = None, filename: str = None,
                            sample: str = None):
        """Acquire repeatedly at one position, at the given elapsed times."""
        p = {'times_min': list(times_min)}
        if params is not None:
            p['params'] = params
        if position is not None:
            p['position'] = int(position)
        if filename is not None:
            p['filename'] = filename
        if sample is not None:
            p['sample'] = sample
        return cls('measure_time_series', p)

    @classmethod
    def set_temperature(cls, temperature: float, ramp_rate: float = None,
                        ramp_time: float = None):
        """Change the sample temperature, optionally as a ramp.

        ``ramp_rate`` (per minute) or ``ramp_time`` (total minutes), never both.
        The setpoint is recomputed from the wall clock on every tick rather than
        accumulated per tick: the tick rate is not guaranteed, so an incremental
        ramp would drift by however late the loop ran and silently deliver a
        different rate than the one asked for.
        """
        if ramp_rate is not None and ramp_time is not None:
            raise ValueError('give ramp_rate or ramp_time, not both')
        p = {'temperature': float(temperature)}
        if ramp_rate is not None:
            p['ramp_rate'] = float(ramp_rate)
        if ramp_time is not None:
            p['ramp_time'] = float(ramp_time)
        return cls('set_temperature', p)

    @classmethod
    def wait_a_while(cls, minutes: float):
        return cls('wait_a_while', {'time': float(minutes)})

    @classmethod
    def move_stage(cls, **motion):
        """Move the stage. Absolute (`x`/`y`/`z`) or relative (`dx`/`dy`/`dz`).

        Giving both an absolute and a relative value for the same axis is
        refused rather than resolved: the two disagree about where the stage
        should end up, and picking one silently means commanding one move while
        verifying another.
        """
        for axis in ('x', 'y', 'z'):
            if motion.get(axis) is not None and motion.get('d' + axis) is not None:
                raise ValueError(
                    f"move_stage got both {axis}= and d{axis}= — give an "
                    f"absolute position or a relative step, not both")
        return cls('move_stage', dict(motion))

    @classmethod
    def change_measurement_params(cls, params: str):
        """Switch the active parameter set for the steps that follow."""
        return cls('change_measurement_params', {'params': params})

    @classmethod
    def pause(cls, message: str = ''):
        """Hold until released. This is the checkpoint gate.

        The executor stays on this command, returning ``IDLE``, until a
        ``continue_experiment`` arrives — which is what lets a decision be made
        between measurements without the run proceeding in the meantime.
        """
        return cls('pause', {'message': message} if message else {})

    @classmethod
    def shutdown_instrument(cls):
        return cls('shutdown_instrument', {})


class MeasurementSequence:
    """Builder for a plan. Every method appends and returns ``self``."""

    def __init__(self):
        self.steps: List[MeasurementStep] = []

    def _add(self, step: MeasurementStep) -> 'MeasurementSequence':
        self.steps.append(step)
        return self

    def measure_now(self, **kw):
        return self._add(MeasurementStep.measure_now(**kw))

    def measure_time_series(self, times_min, **kw):
        return self._add(MeasurementStep.measure_time_series(times_min, **kw))

    def set_temperature(self, temperature, **kw):
        return self._add(MeasurementStep.set_temperature(temperature, **kw))

    def wait_a_while(self, minutes):
        return self._add(MeasurementStep.wait_a_while(minutes))

    def move_stage(self, **motion):
        return self._add(MeasurementStep.move_stage(**motion))

    def change_measurement_params(self, params):
        return self._add(MeasurementStep.change_measurement_params(params))

    def pause(self, message=''):
        return self._add(MeasurementStep.pause(message))

    def shutdown_instrument(self):
        return self._add(MeasurementStep.shutdown_instrument())

    def __len__(self):
        return len(self.steps)

    def __iter__(self):
        return iter(self.steps)
