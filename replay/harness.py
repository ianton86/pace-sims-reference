# -*- coding: utf-8 -*-
"""
Re-run the study's checkpoint decisions from its deposited profiles.

This is the repository's centrepiece. For each of the study's measurements it
loads the depth profile, **derives** the metrics the quality-control rules
consume, runs the real rules and the real decision menu over them, and compares
the outcome against what was decided at the time.

What is derived and what is given
---------------------------------
The distinction is the whole value of the exercise, so it is enforced by the
shape of the data rather than by good intentions.

**Derived from the profile** — the layer window, the number of points across the
layer, the peak counts per pixel per shot, the yield of the named drift channel.
These are what the decisions actually turn on.

**Given in ``data/measurements.csv``** — facts about the *acquisition* that no
profile can contain: the raster, whether the run stopped on its trigger or its
ceiling, whether an ion-source excursion was recorded, the crater uniformity
(measured from a map, which is not part of the deposited reduction), and the
measurement's role in the study.

**Compared against, never used** — ``recorded_decision``. Replaying off the
study's own quality-control log would be circular: that file carries the metrics
*and* the decisions the agent wrote beside them. Nothing here reads it.

What agreement here does and does not show
------------------------------------------
It shows that the published rules, applied to the published data, reproduce the
published decisions — that the decision sequence was a consequence of stated
criteria rather than of judgement that cannot be inspected.

It does **not** show the rules ran as code during the study. They did not: they
were prose in the agent's knowledge base, and the engine did not enforce them.
It also does not revalidate the science; the metrics are re-derived here by a
simpler reduction than the study's own, and where the two differ the difference
is reported rather than absorbed.

One limit is worth naming because it is invisible otherwise: the **saturation**
metric derived here is a lower bound. The study's busiest channel was a
primary-related positive species that the deposited profiles do not carry — the
export holds the isotope and matrix channels the measurement was *about* — so
what is computed here is the maximum over the published channels. Both the
published channels and the study's own unpublished figure sit far under the
ceiling, so the conclusion holds; the study's number simply cannot be
re-derived from this deposit alone.
"""

import csv
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from analysis import (channel_yield, counts_per_px_shot, layer_window,
                      load_profile, peak_counts_per_scan)
from pace import QualityCriteria, decide


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'data')


@dataclass
class Replayed:
    """One measurement, re-decided."""
    id: str
    order: int
    role: str
    polarity: str
    metrics: Dict[str, Any]
    decision: Any
    recorded: str
    recorded_label: str
    findings: List[Any] = field(default_factory=list)

    @property
    def agrees(self):
        """Did the replay reach the same outcome as the run did?"""
        return self.decision.outcome == self.recorded

    @property
    def agrees_on_acceptance(self):
        """The coarser question: kept or rejected.

        Reported separately because it is the one the study's own labels answer
        unambiguously — a label beginning INVALID is a rejection and nothing
        else — whereas the finer grade between accepting cleanly and accepting
        with a reservation is a reading of free text.
        """
        accepted_now = self.decision.accepted
        accepted_then = self.recorded in ('accept', 'accept_with_flag')
        return accepted_now == accepted_then

    def __str__(self):
        mark = '  ' if self.agrees else ('~ ' if self.agrees_on_acceptance else 'XX')
        return (f'{mark} {self.id:16} {self.role:11} '
                f'{self.decision.outcome:18} vs {self.recorded:18} '
                f'({self.recorded_label})')


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def derive_metrics(profile, record):
    """Reduce one profile to the numbers the criteria judge.

    Note what is *not* here: nothing reads the recorded decision, and nothing
    reads a layer window recorded at the time. The window is found from the
    profile every time, because it is the reduction the sampling criterion
    turns on and taking it as given would hand the harness its answer.
    """
    window = layer_window(profile, record['layer_marker'],
                          record['layer_marker_sense'])
    peak = peak_counts_per_scan(profile, window)

    return {
        'layer_points': window.points,
        'sputter_frames': _number(record['sputter_frames']),
        'counts_per_px_shot': counts_per_px_shot(
            peak, int(record['resolution_px']), int(record['shots_per_pixel'])),
        'uniformity': _number(record['uniformity']),
        'stopped_by': record['stopped_by'],
        'source_excursion': record['source_excursion'] == 'true',
        'layer_yield': channel_yield(profile, record['yield_channel'], window),
        'layer_yield_channel': record['yield_channel'],
        'layer_window': window,
    }


def replay(data_dir=DATA_DIR, criteria=None):
    """Re-decide every measurement, in acquisition order.

    Choosing what each measurement is compared against is the part of this
    harness with judgement in it, and two rules govern it. Both were found by
    running the replay and reading its disagreements, not by reasoning in
    advance.

    **A baseline is per polarity AND per sample.** Yields are not comparable
    across polarities — the criteria refuse that outright once the channels are
    named. Less obviously, they are not comparable across *samples* either: the
    drift channel here is an oxygen signal, and the samples differ in oxygen
    content by design, so judging one sample against another reports the
    measurand as drift. Two measurements of the study's most enriched sample
    were escalated for exactly that before this was fixed. It is also why the
    study put its drift anchors and its final sentinel on a spare position of
    *one* sample rather than wherever was convenient.

    **A rejected measurement cannot become a baseline.** Otherwise a run that
    was invalidated for being outside the sampling band becomes the reference
    its own replacement is judged against, and every later measurement of that
    sample inherits the fault as its definition of normal.
    """
    criteria = criteria or QualityCriteria()
    with open(os.path.join(data_dir, 'measurements.csv'), encoding='utf-8') as f:
        records = list(csv.DictReader(f))

    references = {}
    results = []
    for record in sorted(records, key=lambda r: int(r['order'])):
        path = os.path.join(data_dir, 'profiles', record['id'] + '.txt')
        metrics = derive_metrics(load_profile(path), record)

        role = record['role']
        key = (record['polarity'], record['sample'])
        findings = criteria.evaluate(metrics, references.get(key), role)
        decision = decide(findings)

        if role == 'reference':
            references[key] = metrics
        elif key not in references and decision.accepted:
            # The first ACCEPTED measurement of a sample becomes its baseline
            # when no reference run covers it. Stated rather than left implicit:
            # without a baseline the sample's drift would go unexamined, which
            # is worse than an imperfect one.
            references[key] = metrics

        results.append(Replayed(
            record['id'], int(record['order']), role, record['polarity'],
            metrics, decision, record['recorded_decision'],
            record['recorded_label'], findings))
    return results


def report(results):
    """Human-readable summary — what the paper cites."""
    exact = sum(r.agrees for r in results)
    coarse = sum(r.agrees_on_acceptance for r in results)
    lines = [str(r) for r in results]
    lines.append('')
    lines.append(f'{coarse}/{len(results)} agree on kept-or-rejected')
    lines.append(f'{exact}/{len(results)} agree on the exact outcome')
    return '\n'.join(lines)


