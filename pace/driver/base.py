# -*- coding: utf-8 -*-
"""
The instrument boundary: everything the executor is allowed to ask for.

``InstrumentDriver`` is the public shape of the part of this system that is not
published. The production implementation wraps proprietary, vendor-internal
interfaces of the acquisition software and drives a shared user-facility
instrument where an incorrect command has physical consequences; it is not
redistributable. What *is* publishable is its boundary, and that boundary is
narrow on purpose — five methods, none of which returns data. A reader can see
exactly how much authority the orchestration layer has over the hardware by
reading this file, which is the point of putting it here rather than describing
it in prose.

The contract
------------
**A handler returns a ``NextAction`` and must not block.** Return
``NextAction.IDLE`` while the operation is still going and it will be called
again on the next tick; return ``NextAction.NEXT`` when it is done. The loop
that calls it is the same one that polls for the agent's stop command and writes
the telemetry a monitor reads, so a handler that blocks for a minute is a minute
in which the run cannot be stopped and nothing outside can see what it is doing.
Concurrency, if a driver needs any, belongs behind this interface.

**Handlers raise on failure.** There is no error return. The executor treats any
exception as a failed step and ends the run through its normal exit path, with
the cause recorded in the telemetry file. Anything a driver can survive — a
retried transport call, a re-read — it should survive silently, below here.

**Data does not come back through this interface.** A handler is told to acquire
and says when it is finished; the measurement lands wherever the driver puts it,
and the analysis layer reads it from there by name. That is why ``measure_now``
is given a ``filename`` rather than returning one — the name is decided by the
plan before the acquisition starts, so there is nothing left to reconcile
afterwards if the run is interrupted.

Adapting this to another instrument
-----------------------------------
Subclass, implement the four abstract methods, and add ``time_series`` if your
plans use one. The optional hooks below all have working defaults, so a minimal
driver is genuinely four methods. ``pace.driver.SimulatedInstrument`` is a
complete example that happens to have no hardware behind it.
"""

from abc import ABC, abstractmethod


class InstrumentDriver(ABC):
    """What a plan may ask of an instrument.

    Positions, filenames and parameter sets arrive already resolved: the
    executor looks a named parameter set up before dispatch, so a driver never
    has to know that the plan refers to parameters by name.
    """

    envelope = None

    def use_envelope(self, envelope):
        """Adopt the run's safety envelope. Called once, before the first tick.

        The executor bounds the values the **plan** asks for, but a driver that
        generates values of its own — every intermediate setpoint of a ramp, a
        retract height, a position resolved at runtime — must bound those
        itself, because they never pass through the executor. Enforcement is in
        two places because the values originate in two places.

        Those self-generated values follow the *automatic* rule: clamp to the
        nearest bound and log it, never reject. Rejecting would abort an
        unattended run over a value nobody asked for — the ramp's start, taken
        from the measured temperature, is the case that actually bites, since a
        stage sitting below the declared range would refuse its own first step.

        Handed over rather than declared separately so that the envelope in the
        record and the envelope being enforced cannot drift apart.
        """
        self.envelope = envelope

    # ── Required ─────────────────────────────────────────────────────

    @abstractmethod
    def measure_now(self, params, position, filename, sample):
        """Acquire one measurement.

        Parameters
        ----------
        params : MeasurementParams
            Already resolved from the plan's named set.
        position : int or None
            1-based index into the registered positions. ``None`` means the
            driver chooses — typically the next unused position of ``sample``.
        filename : str or None
            The name to record the measurement under, decided before the
            acquisition starts. ``None`` lets the driver name it.
        sample : str or None
            Which specimen on the holder this belongs to.
        """

    @abstractmethod
    def set_temperature(self, target, ramp_rate=None, ramp_time=None):
        """Drive the sample temperature to ``target`` °C and wait for arrival.

        At most one of ``ramp_rate`` (°C/min) or ``ramp_time`` (minutes for the
        whole change) may be given; they over-determine the same line. With
        neither, the setpoint is written once and the step waits.

        A ramped setpoint should be recomputed from the **wall clock** on every
        tick rather than accumulated per tick. The tick rate is not guaranteed,
        so an incremental ramp drifts by exactly the amount the loop ran late
        and quietly delivers a different rate than the one asked for — on the
        one parameter whose entire point is the rate at which it moves.
        """

    @abstractmethod
    def move_stage(self, axes):
        """Move the stage. ``axes`` maps axis name to target.

        Absolute (``x``) and relative (``dx``) targets for the same axis are
        mutually exclusive and a driver should refuse both together rather than
        pick one: commanding one and verifying the other fails by timing out
        against a target that was never aimed at.
        """

    @abstractmethod
    def shutdown(self):
        """Bring the instrument down in the order its hardware requires."""

    # ── Optional ─────────────────────────────────────────────────────

    def time_series(self, times, params, position, filename, sample):
        """Acquire a series of measurements at the given elapsed times.

        Optional because a plan need not contain one. The default refuses in a
        way that names the step, so a plan carrying one against a driver that
        cannot is a clear failure rather than a mystery.
        """
        raise NotImplementedError(
            f'{type(self).__name__} does not implement time_series, but the '
            f'plan contains one')

    def abort(self):
        """Stop whatever is in flight, now. Called when a run is ended
        immediately, so it should not wait for a scan to complete."""

    def stop_measurement(self):
        """End the measurement in progress early; the run then continues.

        Distinct from ``abort``: this finishes the current *command* cleanly —
        the data acquired so far is kept and post-processed — where ``abort``
        ends the whole run. The executor calls this only while a measurement
        command is the one running.
        """

    def on_tick(self):
        """Per-tick housekeeping: sample a controller, refresh a live view.

        Called once per pass of the loop in both the running and the idle
        branches, because a run holding at a checkpoint still has hardware worth
        recording.
        """

    def status(self):
        """Telemetry merged into ``experiment_status.json`` each tick.

        Report the readings *and* which channel produced them where a driver has
        more than one: a null reading means "the hardware is idle" on one and
        "this channel cannot read that at all" on another, and nothing outside
        can tell those apart from the number alone.
        """
        return {}
