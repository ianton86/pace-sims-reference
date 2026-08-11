# -*- coding: utf-8 -*-
"""
The dynamic stop rule, on signals built to isolate one semantic each.

This rule decides how long a measurement runs, so getting it wrong is expensive
in a way that is hard to see afterwards: a profile cut short is missing the part
that was being measured, and one that never triggers spends its whole scan
ceiling in the substrate. Neither leaves an error anywhere.

Every test below uses a hand-built signal rather than a simulated one, because
the point is to pin a boundary — the scan a trigger fires on, the crossing that
should not count — and a signal with noise in it cannot pin a boundary.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pace import StopCondition
from pace.stop_conditions import dynamic_stop_scan, median3


def rising(threshold=100, **kwargs):
    return StopCondition(kind='Dynamic', label='marker', threshold=threshold,
                         trigger='rise', **kwargs)


# ── Counting events, not scans ───────────────────────────────────────

def test_one_crossing_fires_a_single_event_trigger():
    signal = [0] * 10 + [500] * 10
    # +1 for the scan in progress, which is excluded.
    assert dynamic_stop_scan(signal, rising()) == 11


def test_staying_above_the_threshold_is_still_only_one_event():
    """The failure this rules out: reading trigger_count as "N scans above"
    turns one layer boundary into as many events as there are scans past it."""
    signal = [0] * 10 + [500] * 30
    assert dynamic_stop_scan(signal, rising(trigger_count=2)) is None


def test_two_separate_crossings_fire_a_two_event_trigger():
    signal = [0] * 5 + [500] * 5 + [0] * 5 + [500] * 5
    assert dynamic_stop_scan(signal, rising(trigger_count=2)) == 16


def test_post_scans_are_acquired_after_the_triggering_scan():
    signal = [0] * 10 + [500] * 10
    assert dynamic_stop_scan(signal, rising(post_scans=30)) == 41


# ── The scan in progress ─────────────────────────────────────────────

def test_the_last_scan_is_excluded():
    """It may still be acquiring, so its counts are partial — and a partial
    scan reads as a fall, which on a falling trigger is a crossing that never
    happened."""
    just_crossed = [0] * 10 + [500]
    assert dynamic_stop_scan(just_crossed, rising()) is None, \
        'the crossing is in the scan still being acquired'

    confirmed = just_crossed + [500]
    assert dynamic_stop_scan(confirmed, rising()) == 11


def test_a_signal_too_short_to_judge_returns_none():
    assert dynamic_stop_scan([], rising()) is None
    assert dynamic_stop_scan([500], rising()) is None


# ── The ignored surface region ───────────────────────────────────────

def test_scans_inside_the_ignore_window_cannot_trigger():
    """Surface contamination is the reason the window exists."""
    signal = [500] * 3 + [0] * 10 + [0]
    assert dynamic_stop_scan(signal, rising(ignore_first_scans=5)) is None


def test_a_signal_already_above_when_the_window_opens_is_the_first_event():
    """The window opens "not yet crossed", so a layer that began inside the
    ignored region is caught rather than silently missed — without this the run
    goes to its scan ceiling with nothing to say why."""
    signal = [500] * 20
    assert dynamic_stop_scan(signal, rising(ignore_first_scans=5)) == 6


def test_an_ignore_window_longer_than_the_data_returns_none():
    assert dynamic_stop_scan([0] * 5, rising(ignore_first_scans=20)) is None


# ── Falling triggers ─────────────────────────────────────────────────

def test_a_falling_trigger_counts_above_to_below():
    signal = [500] * 10 + [0] * 10
    condition = StopCondition(kind='Dynamic', label='marker', threshold=100,
                              trigger='fall')
    assert dynamic_stop_scan(signal, condition) == 11


# ── Debouncing ───────────────────────────────────────────────────────

def test_a_single_scan_spike_does_not_count_as_a_crossing():
    """Without the median filter this is a false layer boundary, and the
    measurement stops in the middle of the film."""
    signal = [0] * 5 + [900] + [0] * 10
    assert dynamic_stop_scan(signal, rising()) is None


def test_a_two_scan_excursion_does_count():
    """The filter debounces one scan, not a real feature two wide. Stated so
    the limit of the debouncing is on the record rather than assumed."""
    signal = [0] * 5 + [900, 900] + [0] * 10
    assert dynamic_stop_scan(signal, rising()) == 6      # the 6th scan, index 5


def test_the_median_filter_leaves_the_endpoints_alone():
    """A three-wide window reflected at the edge is [x0, x0, x1], whose median
    is x0 — so the first and last samples pass through unchanged."""
    assert median3([9, 1, 1, 1, 9]) == [9, 1, 1, 1, 9]
    assert median3([1, 9, 1]) == [1, 1, 1]
    assert median3([5, 3]) == [5, 3], 'too short to filter'
