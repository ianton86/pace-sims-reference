# -*- coding: utf-8 -*-
"""
The quality criteria and the decision menu.

These are the rules the agent was instructed to apply, expressed as code so they
can be checked — not a recording of the code path that ran at the time, since
during the study they lived in the agent's knowledge base and the engine did not
enforce them. The tests are correspondingly about the *rules*: that each catches
what it exists to catch, and that a set of findings maps onto exactly one
outcome from a closed menu.

Two things are worth more than the rest.

**Repeat against escalate.** Getting it backwards costs either an unattended run
repeating a broken measurement until its beam time is gone, or a person woken
for one that would have been clean on the second attempt. The study did both in
sequence — an excursion was repeated, the repeat came back low, and *that*
escalated — and that sequence is reproduced below.

**A missing metric is not a pass.** A quality log that reports an unmeasured
quantity as fine is worse than one that omits it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace import Decision, QualityCriteria, decide
from pace.decisions import (ACCEPT, ACCEPT_WITH_FLAG, ESCALATE, MENU, REPEAT,
                            RETUNE_AND_REPEAT)
from pace.qc import FAIL, FLAG, MEASUREMENT, PASS, REFERENCE, SURVEY


CRITERIA = QualityCriteria()

# A measurement that passes everything. Individual tests spoil one field at a
# time, so what each is testing is the difference from this.
GOOD = {
    'layer_points': 56,
    'sputter_frames': 11,
    'counts_per_px_shot': 0.84,
    'uniformity': 0.86,
    'stopped_by': 'dynamic',
    'source_excursion': False,
    'layer_yield': 9880.0,
}
REFERENCE_METRICS = dict(GOOD, layer_yield=9211.0)


def verdicts(metrics, reference=REFERENCE_METRICS, criteria=CRITERIA, role=MEASUREMENT):
    return {f.rule: f.verdict
            for f in criteria.evaluate(metrics, reference, role)}


def outcome(metrics, reference=REFERENCE_METRICS, criteria=CRITERIA, role=MEASUREMENT):
    return decide(criteria.evaluate(metrics, reference, role))


# ── Roles ────────────────────────────────────────────────────────────

def test_a_reconnaissance_is_not_held_to_the_standard_it_establishes():
    """It is deliberately run to a fixed scan count and deliberately outside
    the sampling band — it is the run that MEASURES the sputter rate the band
    correction is computed from, so failing it for being outside the band is
    circular.

    This is not hypothetical: run against the study's own log, a role-blind
    standard rejected exactly two of its 35 measurements, and they were the
    reconnaissance and the reference.
    """
    recon = dict(GOOD, layer_points=14, sputter_frames=45,
                 stopped_by='scan_limit')
    assert outcome(recon, role=MEASUREMENT).outcome != ACCEPT
    assert outcome(recon, role=SURVEY).outcome == ACCEPT


def test_a_reference_may_be_run_to_a_fixed_scan_count():
    """It is configured to characterise the channel, not to be comparable with
    the block it anchors."""
    ref_run = dict(GOOD, stopped_by='scan_limit')
    assert verdicts(ref_run, role=REFERENCE).get('termination') is None
    assert outcome(ref_run, role=REFERENCE).outcome == ACCEPT


def test_a_reference_is_not_compared_against_a_reference():
    """It is one. Comparing it to another block's would report the difference
    between two channels as drift."""
    assert 'source_stability' not in verdicts(GOOD, role=REFERENCE)


def test_a_reconnaissance_is_still_judged_on_the_detector_and_the_crater():
    """Exempt from the standard it establishes, not exempt from everything —
    a saturated or badly aligned reconnaissance would set the wrong standard."""
    assert verdicts(dict(GOOD, counts_per_px_shot=11.0),
                    role=SURVEY)['saturation'] == FAIL
    assert verdicts(dict(GOOD, uniformity=1.5), role=SURVEY)['uniformity'] == FAIL


def test_an_unknown_role_is_refused():
    """Silently applying the full standard to a role somebody invented would
    reject exactly the runs that role was invented to exempt."""
    with pytest.raises(ValueError):
        CRITERIA.evaluate(GOOD, REFERENCE_METRICS, role='calibration')


# ── A clean measurement ──────────────────────────────────────────────

def test_a_good_measurement_passes_every_rule():
    assert set(verdicts(GOOD).values()) == {PASS}
    assert outcome(GOOD).outcome == ACCEPT


def test_every_rule_reports_even_once_one_has_failed():
    """One finding decides what to do; all of them explain the run, and the
    checkpoint log is what the study is read from afterwards."""
    spoiled = dict(GOOD, layer_points=48, stopped_by='scan_limit')
    assert len(CRITERIA.evaluate(spoiled, REFERENCE_METRICS)) == 5


# ── Depth sampling ───────────────────────────────────────────────────

@pytest.mark.parametrize('points', [50, 55, 60])
def test_the_band_is_inclusive(points):
    assert verdicts(dict(GOOD, layer_points=points))['depth_sampling'] == PASS


@pytest.mark.parametrize('points,frames,corrected', [
    (48, 11, 10),      # below the band
    (66, 11, 13),      # above it
    (45, 11, 9),
])
def test_being_outside_the_band_retunes_proportionally(points, frames, corrected):
    """The three real corrections the study made, and the arithmetic that
    produced them: frames scale with points, aimed at the band's midpoint."""
    result = outcome(dict(GOOD, layer_points=points, sputter_frames=frames))
    assert result.outcome == RETUNE_AND_REPEAT
    assert result.changes == {'sputter_frames': corrected}


def test_a_correction_that_would_change_nothing_is_not_offered():
    """Repeating with the identical parameter is not a remedy — it would fail
    the same way and loop."""
    result = outcome(dict(GOOD, layer_points=49, sputter_frames=1))
    assert result.changes == {}
    assert result.outcome == ESCALATE, \
        'a fault with no automatic fix belongs to a person'


def test_the_correction_never_goes_below_one_frame():
    result = outcome(dict(GOOD, layer_points=500, sputter_frames=2))
    assert result.changes.get('sputter_frames', 1) >= 1


# ── Saturation ───────────────────────────────────────────────────────

def test_saturation_at_the_ceiling_fails():
    assert verdicts(dict(GOOD, counts_per_px_shot=5.0))['saturation'] == FAIL


def test_the_watch_band_flags_without_rejecting():
    """The study ran quantitative channels at 3.0-4.2 and kept them."""
    assert verdicts(dict(GOOD, counts_per_px_shot=4.2))['saturation'] == FLAG
    assert outcome(dict(GOOD, counts_per_px_shot=4.2)).outcome == ACCEPT_WITH_FLAG


def test_a_saturated_channel_has_no_automatic_remedy():
    assert outcome(dict(GOOD, counts_per_px_shot=11.0)).outcome == ESCALATE


# ── Uniformity and termination ───────────────────────────────────────

def test_an_uneven_crater_fails():
    assert verdicts(dict(GOOD, uniformity=1.5))['uniformity'] == FAIL


def test_poisson_limited_uniformity_passes():
    assert verdicts(dict(GOOD, uniformity=1.0))['uniformity'] == PASS


def test_hitting_the_scan_ceiling_fails():
    """It stopped for an arbitrary reason, so its depth axis is not comparable
    with any other run's."""
    assert verdicts(dict(GOOD, stopped_by='scan_limit'))['termination'] == FAIL


def test_stopping_for_an_unrecognised_reason_fails_rather_than_passes():
    assert verdicts(dict(GOOD, stopped_by='operator'))['termination'] == FAIL


# ── Source stability: the distinction that matters ───────────────────

def test_an_excursion_during_the_acquisition_is_repeated():
    """Transient: the run is spoiled, a repeat is likely to be clean."""
    result = outcome(dict(GOOD, source_excursion=True, layer_yield=7655.0))
    assert result.outcome == REPEAT


def test_a_low_yield_with_no_excursion_is_escalated():
    """Sustained: a repeat reproduces it, so it needs a person."""
    result = outcome(dict(GOOD, source_excursion=False, layer_yield=7552.0))
    assert result.outcome == ESCALATE


def test_the_studys_actual_sequence_is_reproduced():
    """The three runs the study took to recover from one source fault.

    Repeat, then escalate, then accept — and it is the *second* one that has to
    escalate rather than repeat again, which is the whole point of separating
    the two conditions.
    """
    reference = {'layer_yield': 9211.0}
    dropout = dict(GOOD, source_excursion=True, layer_yield=7655.0)
    repeat_came_back_low = dict(GOOD, source_excursion=False, layer_yield=7552.0)
    after_realignment = dict(GOOD, source_excursion=False, layer_yield=10144.0)

    assert outcome(dropout, reference).outcome == REPEAT
    assert outcome(repeat_came_back_low, reference).outcome == ESCALATE
    assert outcome(after_realignment, reference).outcome == ACCEPT


def test_drift_within_the_flag_band_is_kept_with_a_reservation():
    """Usable for a ratio, on the record for an absolute comparison — the
    distinction is the reader's to make later, so it has to reach the record."""
    result = outcome(dict(GOOD, layer_yield=11179.0))     # +21 % on 9211
    assert result.outcome == ACCEPT_WITH_FLAG
    assert any('yield' in reason for reason in result.reasons)


def test_comparing_yields_from_different_channels_is_refused():
    """Discovered by running these rules over the study's own log with the
    yield taken from a "strongest species" column: the reference's strongest
    species was not the measurements', so the comparison was between two
    different ions and reported a several-fold drift that did not exist.

    It raises rather than flagging because the number it would otherwise
    produce is confident and plausible, and the fault is in whatever assembled
    the metrics rather than in the measurement.
    """
    with pytest.raises(ValueError) as excinfo:
        CRITERIA.evaluate(dict(GOOD, layer_yield_channel='Cs+ phase A'),
                          dict(REFERENCE_METRICS, layer_yield_channel='O- film'))
    assert 'different channels' in str(excinfo.value)


def test_matching_channels_compare_normally():
    findings = verdicts(dict(GOOD, layer_yield_channel='O- film'),
                        dict(REFERENCE_METRICS, layer_yield_channel='O- film'))
    assert findings['source_stability'] == PASS


def test_an_undeclared_channel_does_not_block_the_comparison():
    """Naming the channel is how a caller opts into the check; a metrics dict
    that predates it still works, and still gets a drift number."""
    assert verdicts(GOOD)['source_stability'] == PASS


def test_yield_is_not_judged_without_a_reference():
    """There is nothing to compare against, and inventing a baseline would
    make the first measurement of every run look like drift."""
    assert 'source_stability' not in verdicts(GOOD, reference=None)
    assert outcome(GOOD, reference=None).outcome == ACCEPT


# ── A missing metric is not a pass ───────────────────────────────────

@pytest.mark.parametrize('metric', ['layer_points', 'counts_per_px_shot',
                                    'uniformity', 'stopped_by'])
def test_an_unmeasured_quantity_is_not_reported_as_fine(metric):
    metrics = {k: v for k, v in GOOD.items() if k != metric}
    reported = verdicts(metrics)
    assert metric.split('_')[0] not in ' '.join(reported), \
        'an absent metric must produce no finding at all'
    assert len(reported) == 4


# ── The menu is closed ───────────────────────────────────────────────

def test_every_outcome_comes_from_the_menu():
    """The claim the closed menu makes: "what could it have done?" has an
    answer. A branch returning something outside MENU would break that."""
    cases = [
        GOOD,
        dict(GOOD, layer_points=48),
        dict(GOOD, counts_per_px_shot=4.2),
        dict(GOOD, counts_per_px_shot=11.0),
        dict(GOOD, uniformity=1.5),
        dict(GOOD, stopped_by='scan_limit'),
        dict(GOOD, source_excursion=True),
        dict(GOOD, layer_yield=7552.0),
        dict(GOOD, layer_yield=11179.0),
        {},
    ]
    for metrics in cases:
        assert outcome(metrics).outcome in MENU


def test_the_costliest_outcome_wins_when_findings_disagree():
    """Retuning a run that was going to be spoiled anyway wastes the
    correction, so the source fault decides."""
    result = outcome(dict(GOOD, layer_points=48, source_excursion=True))
    assert result.outcome == REPEAT


def test_a_losing_retune_does_not_travel_with_the_decision():
    """A run being escalated must not carry a parameter change: the person
    looking at it decides what changes, and a half-applied correction sitting
    in the plan is the state nobody expects to inherit."""
    result = outcome(dict(GOOD, layer_points=48, counts_per_px_shot=11.0))
    assert result.outcome == ESCALATE
    assert result.changes == {}


def test_the_reasons_survive_even_when_they_did_not_decide():
    result = outcome(dict(GOOD, layer_points=48, source_excursion=True))
    assert len(result.reasons) == 2, \
        'both failures explain the run, whichever one chose the outcome'


def test_no_findings_is_an_accept():
    assert decide([]).outcome == ACCEPT


# ── The standard is publishable ──────────────────────────────────────

def test_the_criteria_can_state_themselves():
    """The set of thresholds is the acceptance standard, and it has to be
    readable as a table rather than reconstructed from the code."""
    rows = CRITERIA.describe()
    assert len(rows) == 5
    assert all(len(row) == 3 for row in rows)
    assert any('50-60' in row[1] for row in rows)


def test_the_thresholds_are_all_adjustable():
    """They are properties of one sample, gun and geometry — not constants."""
    strict = QualityCriteria(points_band=(54, 58), uniformity_max=0.9)
    assert verdicts(GOOD, criteria=strict)['depth_sampling'] == PASS
    assert verdicts(dict(GOOD, layer_points=50),
                    criteria=strict)['depth_sampling'] == FAIL
    assert verdicts(GOOD, criteria=strict)['uniformity'] == PASS
