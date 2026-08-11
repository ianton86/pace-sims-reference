# -*- coding: utf-8 -*-
"""
When to stop acquiring: a fixed scan count, or a feature in the data.

A depth profile has no natural length. Stopping at a fixed number of scans means
either cutting the profile short on a thicker film or spending beam time in the
substrate on a thinner one — and the layer thickness is usually the thing being
measured, so it cannot be known in advance. A **dynamic** stop instead watches a
species and ends the run a set number of scans after it does something: the
substrate marker rising is the usual trigger.

This module is the rule, as a pure function of the signal. It is here rather
than inside a driver because both a real driver and the simulator need it, and
because its semantics are subtle enough to be worth testing on their own.

Three of those semantics are easy to get wrong, and each has a distinct failure:

* **``trigger_count`` counts crossing EVENTS, not scans above the threshold.**
  One event is a below→above transition. Reading it as "N consecutive scans
  above" makes a single noisy scan pair look like a layer boundary.
* **The last scan is excluded.** It may still be acquiring, so its counts are
  partial — and a partial scan reads as a *fall*, which on a falling trigger is
  a crossing that never happened.
* **The start of the examined window counts as not-yet-crossed.** A signal that
  is already above the threshold on the first scan examined is therefore the
  first event. Without that, a layer beginning inside the ignored surface region
  is silently missed and the run goes to its scan ceiling.
"""


def median3(values):
    """Three-point median filter, edges unchanged.

    Debounces a single-scan spike so it cannot register as a crossing, which is
    what makes an event count meaningful on real counting noise. Endpoints are
    left alone because a three-wide window reflected at the edge is
    ``[x0, x0, x1]``, whose median is ``x0``.
    """
    values = list(values)
    if len(values) < 3:
        return values
    out = list(values)
    for i in range(1, len(values) - 1):
        out[i] = sorted(values[i - 1:i + 2])[1]
    return out


def dynamic_stop_scan(signal, condition):
    """The scan count to stop at, or ``None`` while the trigger has not fired.

    Parameters
    ----------
    signal : sequence of float
        Per-scan intensity of the species named by ``condition.label``, in
        acquisition order, including the scan currently being acquired.
    condition : StopCondition
        Read for ``threshold``, ``trigger``, ``trigger_count``, ``post_scans``
        and ``ignore_first_scans``.

    Returns
    -------
    int or None
        A **1-based scan count**, so it can be compared directly against the
        number of scans acquired: ``post_scans`` is the number acquired after
        the triggering scan.
    """
    n = len(signal)
    if n < 2:
        return None

    # Drop the scan in progress — see the module docstring.
    profile = median3(signal[:n - 1])
    n -= 1

    start = condition.ignore_first_scans
    if start >= n:
        return None

    rising = condition.trigger == 'rise'

    def crossed(value):
        return value >= condition.threshold if rising else value <= condition.threshold

    was_crossed = False       # the window opens "not yet crossed"
    events = 0
    for i in range(start, n):
        now_crossed = crossed(profile[i])
        if now_crossed and not was_crossed:
            events += 1
            if events >= condition.trigger_count:
                return i + 1 + condition.post_scans     # 0-based index → count
        was_crossed = now_crossed

    return None
