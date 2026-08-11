# -*- coding: utf-8 -*-
"""
The paper's central check: the published rules reproduce the study's decisions.

``pytest replay/`` runs this. It loads each of the study's 35 deposited depth
profiles, derives the quality-control metrics from them, runs the published
criteria and decision menu, and compares the outcome against what was decided at
the checkpoint at the time.

What agreement shows, and what it does not
------------------------------------------
It shows the decision sequence was a consequence of stated criteria applied to
the data, rather than of judgement that cannot be inspected.

It does not show the rules ran as code during the study — they did not; they
were prose in the agent's knowledge base and the engine did not enforce them.
Nor does it revalidate the science: the metrics are re-derived here by a simpler
reduction than the study's own.

Two measurements disagree on the finer grade and both are about the drift
threshold. They are asserted as known differences rather than tuned away, and
the reason is in the test: a threshold fitted to reproduce a set of free-text
labels is no longer a criterion.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace import QualityCriteria
from pace.decisions import ACCEPT, ACCEPT_WITH_FLAG, ESCALATE, REPEAT, RETUNE_AND_REPEAT
from replay.harness import replay


@pytest.fixture(scope='module')
def results():
    return replay()


# ── The central claim ────────────────────────────────────────────────

def test_every_measurement_is_reproduced_on_kept_or_rejected(results):
    """The unambiguous axis: the study's labels say INVALID or they do not."""
    disagreed = [r.id for r in results if not r.agrees_on_acceptance]
    assert disagreed == [], f'replay disagreed on {disagreed}'


def test_the_whole_study_is_replayed(results):
    assert len(results) == 35
    assert sum(r.decision.accepted for r in results) == 30
    assert sum(not r.decision.accepted for r in results) == 5


def test_the_five_rejections_are_reproduced_with_their_remedies(results):
    """Not just that these were rejected, but that the replay reaches the same
    remedy — a retune with the same corrected frame count, a repeat, an
    escalation. The remedy is the part that changes what happens next."""
    by_id = {r.id: r for r in results}

    for run_id, frames in (('S2_neg_r1', 10), ('S3_neg_r1', 13), ('S4_neg_r1', 9)):
        decision = by_id[run_id].decision
        assert decision.outcome == RETUNE_AND_REPEAT
        assert decision.changes == {'sputter_frames': frames}, (
            f'{run_id}: the replay proposes {decision.changes}, the study '
            f'corrected to {frames} sputter frames')

    assert by_id['S1_neg_r3'].decision.outcome == REPEAT
    assert by_id['S1_neg_r3redo'].decision.outcome == ESCALATE


def test_the_source_fault_recovery_runs_in_the_right_order(results):
    """Repeat, then escalate, then accept. The second one escalating rather
    than repeating again is the whole point of separating a transient fault
    from a sustained one, and it is the sequence the study actually took."""
    sequence = [(r.id, r.decision.outcome) for r in results
                if r.id.startswith('S1_neg_r3')]
    assert sequence == [('S1_neg_r3', REPEAT),
                        ('S1_neg_r3redo', ESCALATE),
                        ('S1_neg_r3redo2', ACCEPT)]


# ── The derivation, not just the verdict ─────────────────────────────

def test_the_layer_window_is_derived_and_lands_in_the_expected_band(results):
    """Every accepted measurement must be inside the 50-60 sampling band and
    every band rejection outside it — derived from the profile each time, with
    no recorded window used anywhere."""
    for r in results:
        if r.role != 'measurement':
            continue
        points = r.metrics['layer_points']
        rejected_for_band = (r.decision.outcome == RETUNE_AND_REPEAT)
        assert rejected_for_band != (50 <= points <= 60), (
            f'{r.id}: {points:.1f} points but outcome {r.decision.outcome}')


def test_the_three_band_rejections_have_the_widths_the_study_logged(results):
    """The study logged 48, 66 and 45 points for these. Derived independently
    from the profiles, within a scan."""
    by_id = {r.id: r for r in results}
    for run_id, logged in (('S2_neg_r1', 48), ('S3_neg_r1', 66),
                           ('S4_neg_r1', 45)):
        assert by_id[run_id].metrics['layer_points'] == pytest.approx(
            logged, abs=1.5)


def test_no_deposited_channel_came_close_to_saturating(results):
    """A LOWER BOUND on the study's saturation check, and it has to be read as
    one.

    The study's own busiest channel was the primary-related positive species at
    3.47 counts/px/shot, and that species is not among the deposited profiles —
    the export carries the isotope and matrix channels the measurement was
    about. So the value derived here (~1.2) is the maximum over the *published*
    channels, not over everything the instrument recorded.

    The conclusion survives the gap in both directions: the deposited channels
    are far under the ceiling, and the study reports its unpublished busiest
    channel under it too. What cannot be re-derived from this deposit alone is
    the study's own number.
    """
    peak = max(r.metrics['counts_per_px_shot'] for r in results)
    assert peak < 5.0
    assert peak == pytest.approx(1.2, abs=0.4), (
        'the busiest deposited channel; the study\'s busiest channel is not '
        'in the deposit and read 3.47')


def test_nothing_reads_the_recorded_decision(results):
    """Replaying off the study's own quality-control log would be circular —
    that file carries the metrics and the decisions beside them. The metrics a
    decision is made from must contain no trace of the answer."""
    for r in results:
        assert 'recorded_decision' not in r.metrics
        assert 'decision' not in r.metrics


# ── Known differences, stated rather than tuned away ─────────────────

KNOWN_DIFFERENCES = {
    # The study assessed drift on the PEAK count of the strongest channel; the
    # replay uses the mean over the layer, which is the steadier statistic and
    # gives +14.3 % where the peak gives +21 %. One is above the 20 % flag and
    # one below it. Neither is wrong; they are different statistics.
    'S1_neg_r2': (ACCEPT, ACCEPT_WITH_FLAG),
    # The mid-block anchor drifted +31.6 % from the reference and the replay
    # flags it. The study's own numbers agree it drifted — its report calls the
    # negative block unstable at +-20 % — but an anchor exists precisely to
    # measure drift, so its label did not also record a reservation.
    'anchor_neg_mid': (ACCEPT_WITH_FLAG, ACCEPT),
}


def test_the_only_finer_grade_differences_are_the_known_two(results):
    differences = {r.id: (r.decision.outcome, r.recorded)
                   for r in results if not r.agrees}
    assert differences == KNOWN_DIFFERENCES


def test_the_drift_threshold_is_not_fitted_to_the_labels(results):
    """Both known differences would vanish at some other flag threshold, and
    that is exactly why the threshold is not chosen that way. A criterion
    tuned to reproduce a set of free-text labels has stopped being a criterion.
    """
    assert QualityCriteria().drift_flag_frac == 0.20
    assert len(KNOWN_DIFFERENCES) == 2, (
        'if this grows, ask whether the reduction is wrong before touching a '
        'threshold')


# ── The comparisons the harness has to get right ─────────────────────

def test_drift_is_compared_within_a_sample(results):
    """The drift channel is an oxygen signal and the samples differ in oxygen
    content by design, so comparing across samples reports the measurand as
    drift. Two measurements of the most enriched sample were escalated for
    exactly that before this was fixed."""
    by_id = {r.id: r for r in results}
    for run_id in ('S3_pos_r1', 'S3_pos_r2'):
        assert by_id[run_id].decision.outcome == ACCEPT


def test_an_invalidated_run_never_becomes_a_baseline(results):
    """Otherwise a measurement rejected for being outside the band becomes the
    reference its own replacement is judged against, and every later run of
    that sample inherits the fault as its definition of normal."""
    by_id = {r.id: r for r in results}
    for run_id in ('S2_neg_r1redo', 'S3_neg_r1redo', 'S4_neg_r1redo'):
        assert by_id[run_id].decision.outcome == ACCEPT


def test_the_report_states_both_agreement_numbers(results):
    from replay.harness import report
    text = report(results)
    assert '35/35 agree on kept-or-rejected' in text
    assert '33/35 agree on the exact outcome' in text
