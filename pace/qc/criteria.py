# -*- coding: utf-8 -*-
"""
The quality-control criteria applied at every checkpoint.

Each rule takes the metrics reduced from one measurement and returns a
``Finding``: pass, flag, or fail, with the reason in words and — where there is
one — the remedy. What to *do* with a set of findings is ``pace.decisions``;
this module only judges.

Read the standing caveat first
------------------------------
During the study these rules lived in the agent's knowledge base as prose it was
instructed to apply, not in the execution engine, and the engine did not enforce
them. Expressing them here as executable code is what makes them checkable — it
is a faithful re-derivation of the judgement from the logged metrics, not a
recording of the code path that produced it at the time. The part that *was*
mechanically enforced is ``pace.safety``, and the distinction matters: a safety
bound the agent could not exceed is a different kind of claim from a quality
rule it was told to follow.

Why these five
--------------
They are not a general theory of data quality. Each exists because it catches a
failure that is invisible in the finished profile — something that produces a
plausible-looking measurement of the wrong thing:

* **Depth sampling** — too few points across the layer and its shape is
  under-sampled; too many and beam time is spent for no extra information. Both
  produce a perfectly clean profile.
* **Saturation** — a detector at its ceiling reports a number that is a property
  of the detector rather than of the sample, and it looks like a strong signal.
* **Lateral uniformity** — a crater sampling the edge of the analysed area
  mixes depths, which reads as a broadened interface rather than as a
  misalignment.
* **Termination** — a measurement that hit its scan ceiling instead of its
  trigger stopped for an arbitrary reason, so its depth axis means something
  different from every other run's.
* **Source stability** — an ion-source excursion changes the yield mid-run,
  which reads as a real change in composition with depth.

Thresholds
----------
Every number below is a default carried from the study that produced them, and
every one is a constructor argument. They are properties of *that* sample, gun
and geometry, not constants of nature — the docstring for each says where it
came from so a reader can judge whether it transfers.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


PASS, FLAG, FAIL = 'pass', 'flag', 'fail'


# What a measurement was FOR, which decides which rules apply to it. Judging
# every acquisition by the same standard is a category error, and a specific
# one: the two runs whose whole purpose is to establish the standard cannot be
# held to it.
#
# ``SURVEY``      A reconnaissance. Deliberately run to a fixed scan count and
#                 deliberately under-sampled — it is the run that *measures*
#                 the sputter rate the band correction is computed from, so
#                 failing it for being outside the band is circular.
# ``REFERENCE``   Establishes the baseline the block is judged against. It is
#                 configured to characterise the channel, so it may be run to a
#                 fixed scan count, and it cannot be compared against a
#                 reference because it is one.
# ``MEASUREMENT`` Everything else: the full standard.
SURVEY, REFERENCE, MEASUREMENT = 'survey', 'reference', 'measurement'


@dataclass
class Finding:
    """One rule's verdict on one measurement."""
    rule: str
    verdict: str                       # PASS | FLAG | FAIL
    detail: str
    remedy: Optional[Dict[str, Any]] = None

    @property
    def failed(self):
        return self.verdict == FAIL

    def __str__(self):
        return f'{self.rule}: {self.verdict} — {self.detail}'


@dataclass
class QualityCriteria:
    """The thresholds, in one place, so the whole standard is one object.

    Collected rather than scattered through the rules because the set of
    numbers *is* the acceptance standard: it is what a reader has to see to
    judge the study, and what has to be re-declared to apply this to a
    different sample.
    """

    # Points across the layer of interest. The study tuned the sputter rate to
    # land inside this band and rejected anything outside it — 50 points was
    # judged the fewest that resolves the profile shape, 60 the most that is
    # worth the beam time.
    points_band: tuple = (50, 60)

    # Retuning aims at the middle of the band rather than the nearest edge, so
    # a correction has room to be slightly wrong in either direction without
    # immediately failing again.
    points_target: float = 55.0

    # Detector ceiling in counts per pixel per shot. Above this the reading is
    # a property of the detector. The flag level is where the study started
    # watching a channel rather than trusting it.
    saturation_ceiling: float = 5.0
    saturation_flag: float = 3.0

    # Lateral uniformity U = std/sqrt(mean) over the layer-integrated map.
    # U ~ 1 is Poisson-limited, i.e. as uniform as counting statistics allow;
    # the study observed 0.80-0.94 throughout and set the bound well above it.
    uniformity_max: float = 1.3

    # Fractional change in yield against the reference measurement. Beyond this
    # the run is flagged rather than rejected: absolute yield drifts for
    # reasons that do not invalidate a ratio, and the study's own conclusion
    # was that the measurand held to 2.5 % across a 32 % yield swing.
    drift_flag_frac: float = 0.20

    # A yield this far below the reference, with no source excursion recorded
    # to explain it, is a sustained condition rather than a transient one --
    # so it is escalated instead of simply repeated. In the study a repeat at
    # this level was what triggered the operator's realignment.
    yield_low_frac: float = 0.15

    def evaluate(self, metrics, reference=None, role=MEASUREMENT):
        """Judge one measurement. Returns every finding, in declaration order.

        Every rule that applies runs, even after one has already failed. A
        single finding is enough to decide what to do next but not enough to
        explain the run afterwards — and the checkpoint log is the record the
        study is read from.

        ``role`` selects which rules apply; see the constants above. It is not
        a convenience. Checked against the study's own log, a role-blind
        standard rejected exactly two of its 35 measurements, and they were the
        reconnaissance and the reference — the two runs whose purpose is to
        establish the standard the other 33 are held to.

        A metric that is absent is **not judged**: its rule returns nothing
        rather than a pass. Reporting an unmeasured quantity as passing is the
        one failure mode a quality log must not have.

        Choosing the right ``reference`` is the caller's job, and it is not
        merely bookkeeping: yields are not comparable across acquisition
        polarities, so a reference from the wrong block reads as a several-fold
        drift. The replay harness selects per block for that reason.
        """
        rules = self._rules_for(role)
        findings = []
        for rule in rules:
            finding = rule(metrics, reference)
            if finding is not None:
                findings.append(finding)
        return findings

    def _rules_for(self, role):
        if role == SURVEY:
            # It has no band to be inside and no trigger to have stopped on;
            # what can still be wrong with it is the detector and the crater.
            return (self._saturation, self._uniformity)
        if role == REFERENCE:
            return (self._depth_sampling, self._saturation, self._uniformity)
        if role == MEASUREMENT:
            return (self._depth_sampling, self._saturation, self._uniformity,
                    self._termination, self._source_stability)
        raise ValueError(
            f'Unknown measurement role {role!r}; expected one of '
            f'{SURVEY!r}, {REFERENCE!r}, {MEASUREMENT!r}')

    # ── The rules ────────────────────────────────────────────────────

    def _depth_sampling(self, metrics, reference):
        points = metrics.get('layer_points')
        if points is None:
            return None
        low, high = self.points_band
        if low <= points <= high:
            return Finding('depth_sampling', PASS,
                           f'{points} points across the layer, inside '
                           f'[{low}, {high}]')

        remedy = None
        frames = metrics.get('sputter_frames')
        if frames:
            # Proportional: points scale with the number of sputter frames per
            # scan, so the correction is the ratio to the band's midpoint. It is
            # a first-order correction, which is why the corrected run is
            # re-checked rather than assumed good.
            corrected = max(1, round(frames * points / self.points_target))
            if corrected != frames:
                remedy = {'sputter_frames': corrected}
        return Finding('depth_sampling', FAIL,
                       f'{points} points across the layer, outside '
                       f'[{low}, {high}]', remedy)

    def _saturation(self, metrics, reference):
        level = metrics.get('counts_per_px_shot')
        if level is None:
            return None
        if level >= self.saturation_ceiling:
            return Finding('saturation', FAIL,
                           f'{level:.2f} counts/px/shot at or above the '
                           f'{self.saturation_ceiling} ceiling — the reading '
                           f'is a property of the detector')
        if level >= self.saturation_flag:
            return Finding('saturation', FLAG,
                           f'{level:.2f} counts/px/shot, above the '
                           f'{self.saturation_flag} watch level but under the '
                           f'ceiling')
        return Finding('saturation', PASS, f'{level:.2f} counts/px/shot')

    def _uniformity(self, metrics, reference):
        u = metrics.get('uniformity')
        if u is None:
            return None
        if u > self.uniformity_max:
            return Finding('uniformity', FAIL,
                           f'U = {u:.2f} above {self.uniformity_max} — the '
                           f'crater is sampling unevenly, which broadens every '
                           f'interface in the profile')
        return Finding('uniformity', PASS,
                       f'U = {u:.2f} (1.0 would be counting-statistics '
                       f'limited)')

    def _termination(self, metrics, reference):
        stopped_by = metrics.get('stopped_by')
        if stopped_by is None:
            return None
        if stopped_by == 'dynamic':
            return Finding('termination', PASS,
                           'stopped on its trigger, so the depth axis is '
                           'anchored on the sample')
        if stopped_by == 'scan_limit':
            return Finding('termination', FAIL,
                           'reached the scan ceiling instead of its trigger — '
                           'it stopped for an arbitrary reason, so its depth '
                           'axis is not comparable with the others')
        return Finding('termination', FAIL,
                       f'stopped by {stopped_by!r}, which is neither its '
                       f'trigger nor its ceiling')

    def _source_stability(self, metrics, reference):
        """Two conditions that look alike in the data and need opposite responses.

        A recorded excursion during the acquisition is transient: the run is
        spoiled, a repeat is likely to be clean. A yield that is simply low,
        with no excursion to account for it, is a *sustained* change in the
        source — a repeat reproduces it, so it needs a person.

        Distinguishing them is the whole value of this rule, and getting it
        wrong costs either an unnecessary escalation or an unattended run
        repeating a bad measurement until its beam time is gone.
        """
        if metrics.get('source_excursion'):
            return Finding('source_stability', FAIL,
                           'an ion-source excursion was recorded during the '
                           'acquisition, so the yield changed mid-run',
                           {'action': 'repeat'})

        yield_now = metrics.get('layer_yield')
        yield_ref = (reference or {}).get('layer_yield')
        if yield_now is None or not yield_ref:
            return None

        # The two yields must be the SAME channel, and this refuses loudly
        # rather than comparing anyway. Found by running these rules over the
        # study's own log with the yield taken from a "strongest species"
        # column: the reference's strongest species was not the measurements'
        # strongest species, so the comparison was between two different ions
        # and reported a several-fold drift that did not exist. It produced a
        # confident, plausible number — which is why this raises instead of
        # flagging. A mis-specified comparison is an error in whatever assembled
        # the metrics, not a property of the measurement, and a replay that
        # quietly reports the wrong drift is worse than one that stops.
        channel = metrics.get('layer_yield_channel')
        channel_ref = (reference or {}).get('layer_yield_channel')
        if channel is not None and channel_ref is not None and channel != channel_ref:
            raise ValueError(
                f'Cannot compare yields from different channels: this '
                f'measurement reports {channel!r} and the reference reports '
                f'{channel_ref!r}. Drift must be tracked on one named channel; '
                f'"whichever species was strongest" is not one.')

        change = (yield_now - yield_ref) / yield_ref
        if change <= -self.yield_low_frac:
            return Finding('source_stability', FAIL,
                           f'yield {change:+.1%} against the reference with no '
                           f'excursion recorded — a sustained condition, which '
                           f'a repeat would reproduce',
                           {'action': 'escalate'})
        if abs(change) >= self.drift_flag_frac:
            return Finding('source_stability', FLAG,
                           f'yield {change:+.1%} against the reference — '
                           f'usable, but the drift is on the record')
        return Finding('source_stability', PASS,
                       f'yield {change:+.1%} against the reference')

    def describe(self):
        """The acceptance standard as a table. This is what belongs in a paper."""
        low, high = self.points_band
        return [
            ('depth_sampling', f'{low}-{high} points across the layer',
             f'retune sputter frames toward {self.points_target:g}'),
            ('saturation', f'< {self.saturation_ceiling} counts/px/shot '
                           f'(flag from {self.saturation_flag})', 'reject'),
            ('uniformity', f'U <= {self.uniformity_max}', 'reject'),
            ('termination', 'stopped on its trigger, not its ceiling', 'reject'),
            ('source_stability',
             f'no excursion; yield within {self.drift_flag_frac:.0%} of the '
             f'reference (flag) and not {self.yield_low_frac:.0%} below it '
             f'(escalate)', 'repeat or escalate'),
        ]
