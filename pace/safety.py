# -*- coding: utf-8 -*-
"""
The safety envelope: bounds the engine enforces, whatever is steering it.

This is the part of the system that does **not** trust the agent. The
orchestration above may be a person, a script, a language model or nothing at
all — a stalled, buggy or absent controller must not be able to drive the
instrument outside a range that was declared before the run started. So the
bounds live here, below every decision-making layer, and every path to a
destructive parameter goes through them.

Two design choices carry the weight.

**Nothing is defaulted.** There is no built-in safe temperature range, no
built-in stage travel. The safe range depends on the holder, the sample and the
hardware fitted, so a default would either permit something destructive or
silently constrain an experiment to a range nobody chose — and being wrong here
actuates hardware. An undeclared bound is *unconstrained*, which is the
pre-envelope behaviour and is visible as such in the record.

**Reject or clamp is decided by PROVENANCE, not by how far out of range the
value is.** An operator's out-of-range setpoint is **rejected**, so the mistake
surfaces instead of quietly producing the wrong experiment. An automatically
generated one is **clamped** to the nearest bound and logged, because aborting
an unattended run is worse than running at the edge of a declared range. Neither
silently exceeds. Unknown provenance is treated as the operator's — the strict
reading — so a caller that has not been taught about the envelope fails closed.

That asymmetry is only expressible because ``Command.origin`` exists and is
stamped at a single choke point (``Executor.insert``). It is the one place in
this codebase where the *source* of an instruction changes what the instrument
does with it.

What is enforced here, and what is not
--------------------------------------
Enforced at this layer: the temperature setpoint, including every intermediate
value of a ramp, and **absolute** stage targets.

Not enforced here, deliberately: a **relative** stage move, because resolving
one to an absolute position needs to know where the stage currently is, and this
layer does not. It is bounded by the driver, which does. Stating it rather than
half-checking it — a bound that covers some moves and not others reads as a
bound that covers all of them.
"""


class EnvelopeViolation(RuntimeError):
    """A declared bound was exceeded by a value that may not be clamped."""


class SafetyEnvelope:
    """The declared bounds for one run.

    Constructed empty — every bound is caller-supplied. Persisted into the plan
    file's ``meta`` and re-applied on resume, so the limits travel with the plan
    rather than living in whatever session happened to declare them.
    """

    def __init__(self, temperature=None, stage=None, withdraw_z=None):
        self.temperature = None
        self.stage = {}
        self.withdraw_z = None
        if temperature is not None:
            self.set_temperature_limits(temperature)
        if stage:
            self.set_stage_limits(**stage)
        if withdraw_z is not None:
            self.set_withdraw_z(withdraw_z)

    # ── Declaring bounds ─────────────────────────────────────────────

    def set_temperature_limits(self, limits):
        """Allowed setpoint range in °C, or ``None`` to clear."""
        self.temperature = _pair('temperature', limits)

    def set_stage_limits(self, **axes):
        """Allowed travel per axis, in the axis's own unit.

        Each call REPLACES the whole set: an axis not named becomes
        unconstrained. Replacing rather than merging so that the declaration in
        front of you is the whole of what is enforced — a merge would let an
        axis stay bounded by a call made somewhere else entirely.
        """
        self.stage = {}
        for axis, limits in axes.items():
            if limits is not None:
                self.stage[axis] = _pair(axis, limits)

    def set_withdraw_z(self, z):
        """A height to retract to before lateral motion, or ``None``.

        Declared here because it belongs with the run's other bounds and must
        survive a resume, but **applied by the driver** — it is a motion
        sequence, not a range check.
        """
        self.withdraw_z = float(z) if z is not None else None

    # ── Enforcing them ───────────────────────────────────────────────

    def check_temperature(self, target, clamp=False):
        """Return the setpoint to apply, or raise.

        Rejecting is the default on purpose: a caller that has not been taught
        about the envelope fails closed rather than silently exceeding it.
        """
        if self.temperature is None:
            return target
        lo, hi = self.temperature
        if lo <= target <= hi:
            return target
        if not clamp:
            raise EnvelopeViolation(
                f'Temperature envelope: setpoint {target:.1f} °C is outside the '
                f'declared range [{lo:.1f}, {hi:.1f}] °C. Widen the envelope if '
                f'this is intended.')
        return lo if target < lo else hi

    def check_stage(self, **coords):
        """Raise if any *absolute* coordinate is outside its declared travel.

        Only axes that have both a bound and a value are checked, so an
        undeclared axis is unconstrained rather than implicitly zero.
        """
        for axis, value in coords.items():
            if value is None or axis not in self.stage:
                continue
            lo, hi = self.stage[axis]
            if not lo <= value <= hi:
                raise EnvelopeViolation(
                    f'Stage envelope: {axis}={value:.4f} is outside the '
                    f'declared range [{lo:.4f}, {hi:.4f}]')

    def resolve(self, target, origin):
        """Apply the provenance rule to a temperature setpoint.

        Returns ``(value, was_clamped)``. ``origin='auto'`` clamps; anything
        else — including an unrecognised or missing origin — rejects.
        """
        automatic = origin == 'auto'
        value = self.check_temperature(target, clamp=automatic)
        return value, value != target

    # ── Persistence ──────────────────────────────────────────────────

    def to_dict(self):
        return {
            'temperature': list(self.temperature) if self.temperature else None,
            'stage': {a: list(v) for a, v in self.stage.items()} or None,
            'withdraw_z': self.withdraw_z,
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild from the plan file. Returns ``None`` for a missing record.

        A **malformed** record raises rather than degrading to unconstrained:
        the whole point of persisting the envelope is that a resumed run is
        bounded the same way the first half was, and quietly resuming with no
        bounds at all is the one outcome that must not happen silently.
        """
        if not data:
            return None
        return cls(temperature=data.get('temperature'),
                   stage=data.get('stage') or {},
                   withdraw_z=data.get('withdraw_z'))

    def __repr__(self):
        return (f'SafetyEnvelope(temperature={self.temperature}, '
                f'stage={self.stage}, withdraw_z={self.withdraw_z})')


def _pair(name, limits):
    if limits is None:
        return None
    lo, hi = float(limits[0]), float(limits[1])
    if lo >= hi:
        raise ValueError(f'Invalid {name} limits: min ({lo}) must be below '
                         f'max ({hi})')
    return (lo, hi)


# ── Applying the envelope to a command ───────────────────────────────

def guard_command(command, envelope, log=None):
    """Bound a command before it is dispatched. Raises to refuse it.

    Called by the executor for every command, so this function is the whole of
    what the envelope covers — anything not named here is unbounded at this
    layer, which is worth knowing when reading it.

    A clamped value is written **back into the command's arguments**, not merely
    passed on. Three things depend on that: the log and the telemetry report what
    the instrument was actually asked for, the persisted plan records it, and a
    resumed run is not then rejected on the stale out-of-range value it would
    otherwise re-read from the file — a resumed step counts as operator-planned,
    so it would be refused rather than clamped the second time round.
    """
    if envelope is None:
        return

    if command.name == 'set_temperature' and command.args:
        origin = getattr(command, 'origin', None)
        value, clamped = envelope.resolve(command.args[0], origin)
        if clamped:
            if log is not None:
                log(f'Safety envelope: automatic setpoint '
                    f'{command.args[0]:.1f} °C clamped to {value:.1f} °C')
            command.args[0] = value

    elif command.name == 'move_stage' and command.args:
        axes = command.args[0] or {}
        # Absolute targets only — see the module docstring for why a relative
        # move is the driver's to bound rather than this layer's.
        envelope.check_stage(**{a: v for a, v in axes.items()
                                if not a.startswith('d')})
