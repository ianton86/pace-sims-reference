# -*- coding: utf-8 -*-
"""
The layer-window finder, on profiles built to isolate one failure each.

This is the only reduction between the deposited data and a decision with any
judgement in it, so it is the one worth testing hardest. Three shapes below are
not hypothetical — each is why the obvious simpler rule was rejected, and each
occurs in the study's own profiles:

* a **spike at the first interface**, which puts half-of-maximum above the
  layer's own level and closes the window up to 35 scans early;
* a **marker that is low in the layer and high on both sides**, for which no
  single global threshold separates the layer from both neighbours;
* a **two-humped layer**, whose mid-layer dip closes the window on itself if
  the crossings are searched outward from the centre.

The finder is also checked against the study's recorded windows, but that check
needs the deposited profiles and lives in the replay harness; what is here is
the behaviour it must have on data whose right answer is known by construction.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import (HIGH, LOW, Profile, channel_yield, counts_per_px_shot,
                      layer_window, load_profile, peak_counts_per_scan)


def make(marker, name='test', **channels):
    """A profile from explicit per-scan series."""
    n = len(marker)
    return Profile(name, np.arange(n, dtype=float), np.arange(n, dtype=float),
                   {'marker': np.asarray(marker, float),
                    **{k: np.asarray(v, float) for k, v in channels.items()}})


def layered(cap=10.0, layer=1000.0, substrate=10.0,
            cap_scans=40, layer_scans=55, substrate_scans=30):
    """A clean three-region marker: cap, layer, substrate."""
    return ([cap] * cap_scans + [layer] * layer_scans
            + [substrate] * substrate_scans)


# ── The straightforward case ─────────────────────────────────────────

def test_it_finds_a_clean_layer():
    window = layer_window(make(layered()), 'marker', HIGH)
    # Each interface sits half-way between the last scan of one region and the
    # first of the next, so the width is the layer's scan count.
    assert window.points == pytest.approx(55, abs=1.0)
    assert window.start == pytest.approx(39.5, abs=1.0)


def test_the_bounds_are_fractional():
    """An interface falls between two scans far more often than on one, and
    rounding each before subtracting loses up to a scan of width for nothing."""
    ramp = [10.0] * 40 + [500.0] + [1000.0] * 54 + [10.0] * 30
    window = layer_window(make(ramp), 'marker', HIGH)
    assert window.start != round(window.start)


def test_the_window_selects_the_right_scans():
    window = layer_window(make(layered()), 'marker', HIGH)
    mask = window.scan_mask(125)
    assert mask[45] and mask[90]
    assert not mask[10] and not mask[110]


# ── The three shapes that rejected the simpler rules ─────────────────

def test_a_spike_at_the_first_interface_does_not_close_the_window_early():
    """Half of the marker's MAXIMUM is above the layer's own level when the
    interface spikes, so the window closes as soon as the layer settles — up
    to 35 scans early on a real profile."""
    spiked = ([10.0] * 40 + [8000.0] * 3 + [1000.0] * 52 + [10.0] * 30)
    window = layer_window(make(spiked), 'marker', HIGH)
    assert window.points == pytest.approx(55, abs=2.0), \
        'the window closed on the spike rather than on the layer'


def test_a_marker_low_in_the_layer_and_high_on_both_sides():
    """The normal situation for a positive-polarity marker: no single global
    threshold separates the layer from both of its neighbours."""
    inverted = [1300.0] * 40 + [5.0] * 55 + [6400.0] * 30
    window = layer_window(make(inverted), 'marker', LOW)
    assert window.points == pytest.approx(55, abs=2.0)
    assert window.start == pytest.approx(39.5, abs=1.5)


def test_a_two_humped_layer_is_not_cut_at_its_dip():
    """A real profile here has a layer whose marker dips in the middle. Searching
    the crossings outward from the centre closes the window on the dip."""
    humped = ([10.0] * 40 + [5800.0] * 18 + [2000.0] * 19 + [4100.0] * 18
              + [10.0] * 30)
    window = layer_window(make(humped), 'marker', HIGH)
    assert window.points == pytest.approx(55, abs=2.0), \
        'the window closed on the mid-layer dip'


# ── Refusals ─────────────────────────────────────────────────────────

def test_a_marker_that_does_not_mark_the_layer_is_refused():
    """Without this the crossings still return numbers — from noise — and a
    window derived from noise is indistinguishable from a real one further
    down."""
    flat = list(np.random.default_rng(0).normal(100.0, 1.0, 125))
    with pytest.raises(ValueError, match='does not separate'):
        layer_window(make(flat), 'marker', HIGH)


def test_declaring_the_wrong_sense_is_refused_not_inverted():
    """Passing the wrong sense is easy, and silently inverting would produce a
    window somewhere in the surrounding material rather than an error."""
    with pytest.raises(ValueError, match='declared low'):
        layer_window(make(layered()), 'marker', LOW)
    with pytest.raises(ValueError, match='declared high'):
        layer_window(make([1300.0] * 40 + [5.0] * 55 + [6400.0] * 30),
                     'marker', HIGH)


def test_an_unknown_sense_is_refused():
    with pytest.raises(ValueError, match='sense must be'):
        layer_window(make(layered()), 'marker', 'inverted')


def test_a_profile_too_short_to_hold_two_interfaces_is_refused():
    with pytest.raises(ValueError, match='too few'):
        layer_window(make([10.0, 1000.0, 10.0]), 'marker', HIGH)


def test_an_unknown_channel_names_what_is_there():
    profile = make(layered())
    with pytest.raises(KeyError, match='marker'):
        profile['WO3-']


# ── The other reductions ─────────────────────────────────────────────

def test_counts_are_normalised_to_the_detectors_own_units():
    """A saturation ceiling is per pixel per shot, so two measurements at
    different rasters saturate at completely different per-scan counts. This is
    the study's own arithmetic: 9880 counts on a 64x64 raster at 4 shots per
    pixel is 0.603."""
    assert counts_per_px_shot(9880, 64, 4) == pytest.approx(0.603, abs=0.001)


def test_a_raster_of_nothing_is_refused():
    with pytest.raises(ValueError):
        counts_per_px_shot(100, 0, 4)


def test_the_yield_is_averaged_inside_the_layer_only():
    """Including the substrate would make the yield depend on how long the
    acquisition happened to run past the interface."""
    profile = make(layered(), tracer=[1.0] * 40 + [100.0] * 55 + [1.0] * 30)
    window = layer_window(profile, 'marker', HIGH)
    assert channel_yield(profile, 'tracer', window) == pytest.approx(100.0, rel=0.02)


def test_the_peak_count_is_taken_inside_the_layer_too():
    profile = make(layered(), tracer=[9999.0] * 40 + [100.0] * 55 + [1.0] * 30)
    window = layer_window(profile, 'marker', HIGH)
    assert peak_counts_per_scan(profile, window, 'tracer') == pytest.approx(100.0)


def test_the_peak_defaults_to_the_strongest_recorded_channel():
    """The saturation question is whether ANY channel approached the ceiling,
    so naming one in advance means naming the wrong one as soon as a different
    species turns out to be the strongest."""
    profile = make(layered(layer=1000.0), tracer=[1.0] * 40 + [7000.0] * 55 + [1.0] * 30)
    window = layer_window(profile, 'marker', HIGH)
    assert peak_counts_per_scan(profile, window) == pytest.approx(7000.0)


def test_a_derived_ratio_column_is_excluded_from_the_peak(tmp_path):
    """A ratio has no counts, so letting one into a saturation comparison would
    be meaningless — and a ratio near 1.0 would quietly hide a real peak."""
    path = tmp_path / 'r.txt'
    rows = ['# depth_nm\tscan\tA\tratio = A/(A+1)']
    for i, a in enumerate(layered(cap=1.0, layer=900.0, substrate=1.0)):
        rows.append(f'{i * 0.5}\t{i}\t{a}\t{a / (a + 1):.4f}')
    path.write_text('\n'.join(rows) + '\n', encoding='utf-8')

    profile = load_profile(str(path))
    window = layer_window(profile, 'A', HIGH)
    assert 'ratio' in profile.channels and 'ratio' not in profile.recorded
    assert peak_counts_per_scan(profile, window) == pytest.approx(900.0)


# ── Reading a file ───────────────────────────────────────────────────

def test_a_profile_round_trips_through_the_export_format(tmp_path):
    path = tmp_path / 'run_a.txt'
    path.write_text(
        '# depth_nm\tscan\tA\tB\tratio = A/(A+B)\n'
        '0\t0\t10\t1\t0.909\n'
        '0.5\t1\t20\t2\t0.909\n'
        '1.0\t2\t30\t3\t0.909\n', encoding='utf-8')

    profile = load_profile(str(path))
    assert profile.name == 'run_a'
    assert profile.n_scans == 3
    assert list(profile['A']) == [10, 20, 30]
    assert profile.depth_nm[-1] == 1.0
    assert 'ratio' in profile.channels, \
        'a derived column is kept so a reader can see it'
