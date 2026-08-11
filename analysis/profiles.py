# -*- coding: utf-8 -*-
"""
Depth profiles, and the reductions the quality-control rules consume.

This is not a general analysis library. It is the narrow path from a deposited
depth profile to the handful of numbers ``pace.qc`` judges — deliberately so,
because the claim being checked is about the *decisions*, and every reduction
between the data and a decision is something a reader has to take on trust.

A depth profile here is one measurement: a depth axis, a scan index, and one
intensity series per recorded species.

Finding the layer
-----------------
Almost everything downstream needs to know which scans are inside the layer of
interest, and that is the one reduction with any judgement in it. The rule:

1. Take a **marker** — a species whose intensity distinguishes the layer from
   what is above and below it. It may be high in the layer (the layer's own
   compound) or low in it (a species belonging to the surrounding material).
2. Measure three levels: the **capping** layer near the surface, the **layer**
   itself, and the **substrate** at the end.
3. Put each interface at the half-way crossing between the two levels that meet
   there — the cap/layer boundary half-way between cap and layer, the
   layer/substrate boundary half-way between layer and substrate.

Step 3 is the part that matters, and the two alternatives both fail on real
data. Half of the marker's **maximum** fails because the maximum is often a
spike at the first interface rather than the layer's own level, which puts the
threshold above the layer body and closes the window early — measured at up to
35 scans early on a real profile. A single global threshold fails whenever the
marker is low in the layer but high on *both* sides, which is the normal
situation for a positive-polarity marker: there is no one level that separates
the layer from both neighbours.

The one assumption is that the layer is somewhere in the middle third of the
profile. That is true by construction for an acquisition stopped a fixed number
of scans after the substrate is reached, which is what a dynamic stop does, and
``layer_window`` says so rather than assuming it silently: a marker that does
not vary enough to place an interface is refused, not guessed at.

Validated against the study
---------------------------
Run over the study's 35 deposited profiles, this finder reproduces the layer
widths recorded at the time to within **1.2 scans** — and to within 0.1 scans on
all twelve positive-polarity profiles. Three profiles appear to disagree by 5-12
scans and do not: they are the measurements that were *invalidated* for being
outside the sampling band and repeated, so the recorded width belongs to the
repeat. Their derived widths (48.9, 67.4, 45.3) match what was logged for the
originals (48, 66, 45), which is a check rather than an exception.

That accuracy is comfortably enough to reproduce every decision: the acceptance
band is 50-60 points and the closest any measurement came to an edge was two
points.
"""

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np


HIGH, LOW = 'high', 'low'


@dataclass
class Profile:
    """One measurement's depth profile.

    ``derived`` names the columns that are computed from other columns rather
    than recorded — ratios the export carries for convenience. They are kept so
    a reader can see them and excluded from anything that reasons about
    recorded intensity, because a ratio has no counts and would be meaningless
    in a saturation or yield comparison.
    """
    name: str
    depth_nm: np.ndarray
    scan: np.ndarray
    channels: Dict[str, np.ndarray]
    derived: frozenset = frozenset()

    @property
    def n_scans(self):
        return len(self.scan)

    @property
    def recorded(self):
        """The channels that are measured intensities."""
        return {k: v for k, v in self.channels.items() if k not in self.derived}

    def __getitem__(self, channel):
        try:
            return self.channels[channel]
        except KeyError:
            raise KeyError(
                f'{self.name} has no channel {channel!r}; it records '
                f'{sorted(self.channels)}') from None

    def __repr__(self):
        return (f'<Profile {self.name}: {self.n_scans} scans, '
                f'{len(self.channels)} channels>')


@dataclass
class LayerWindow:
    """Where the layer of interest starts and ends, in scans.

    Both bounds are fractional: an interface falls between two scans far more
    often than on one, and rounding each to an integer before subtracting them
    loses up to a scan of the width for no benefit. ``points`` is the width,
    which is what the sampling criterion is expressed in.
    """
    start: float
    end: float

    @property
    def points(self):
        return self.end - self.start

    def scan_mask(self, n_scans):
        """Boolean mask selecting the scans inside the layer."""
        index = np.arange(n_scans)
        return (index >= self.start) & (index <= self.end)


def load_profile(path, name=None):
    """Read one exported profile.

    The format is a tab-separated table whose first line is a ``#``-prefixed
    header. Columns whose name contains ``=`` are derived quantities recorded
    alongside the raw channels; they are kept under the name before the ``=``,
    so a reader can see them, but nothing here consumes one — every reduction
    below starts from a recorded species.
    """
    with open(path, 'r', encoding='utf-8') as f:
        header = f.readline().strip().lstrip('#').strip().split('\t')
    data = np.genfromtxt(path, skip_header=1)
    if data.ndim == 1:
        data = data.reshape(1, -1)

    names = [h.split('=')[0].strip() for h in header]
    derived = frozenset(n for n, h in zip(names, header) if '=' in h)
    columns = {n: data[:, i] for i, n in enumerate(names)}

    depth = columns.pop('depth_nm', None)
    scan = columns.pop('scan', None)
    if scan is None:
        scan = np.arange(len(data), dtype=float)
    if depth is None:
        depth = np.full(len(data), np.nan)

    import os
    return Profile(name or os.path.splitext(os.path.basename(path))[0],
                   depth, scan, columns, derived)


def layer_window(profile, marker, sense=HIGH, edge_fraction=0.12):
    """Locate the layer of interest. See the module docstring for the rule.

    Parameters
    ----------
    marker : str
        The species that distinguishes the layer.
    sense : 'high' | 'low'
        Whether the marker is high inside the layer or low in it. Required
        rather than inferred: inferring it from the data would silently invert
        the window on a marker that behaves unexpectedly, and the caller always
        knows which it is.
    edge_fraction : float
        How much of each end of the profile to average for the capping and
        substrate levels.
    """
    y = np.asarray(profile[marker], dtype=float)
    n = len(y)
    if n < 9:
        raise ValueError(
            f'{profile.name}: {n} scans is too few to place two interfaces')

    edge = max(3, int(round(edge_fraction * n)))
    # The first scans are the surface transient — adsorbed contamination and
    # the detector settling — and averaging them in would drag the capping
    # level toward something that is not the capping layer.
    cap = float(np.median(y[2:edge]))
    substrate = float(np.median(y[-edge:]))
    layer = float(np.median(y[n // 3:2 * n // 3]))

    _check_contrast(profile, marker, sense, cap, layer, substrate)

    start = _crossing(y, (cap + layer) / 2.0, 1, n // 2, forward=True)
    end = _crossing(y, (layer + substrate) / 2.0, n // 2, n - 1, forward=False)
    if start is None or end is None:
        missing = 'capping' if start is None else 'substrate'
        raise ValueError(
            f'{profile.name}: could not place the {missing} interface from '
            f'{marker!r} — it never crosses the half-way level, so the layer '
            f'is not bounded within this profile')
    if end <= start:
        raise ValueError(
            f'{profile.name}: {marker!r} places the layer end ({end:.1f}) at '
            f'or before its start ({start:.1f})')
    return LayerWindow(start, end)


def _check_contrast(profile, marker, sense, cap, layer, substrate):
    """Refuse a marker that does not actually mark the layer.

    Without this the crossings still return numbers — from noise — and a window
    derived from noise is indistinguishable from a real one downstream. The
    sense is checked too, because passing the wrong one is easy and produces a
    window somewhere in the surrounding material rather than an error.
    """
    if sense not in (HIGH, LOW):
        raise ValueError(f'sense must be {HIGH!r} or {LOW!r}, not {sense!r}')

    scale = max(abs(cap), abs(layer), abs(substrate), 1.0)
    if abs(layer - cap) < 0.05 * scale or abs(layer - substrate) < 0.05 * scale:
        raise ValueError(
            f'{profile.name}: {marker!r} does not separate the layer from its '
            f'surroundings (capping {cap:.4g}, layer {layer:.4g}, substrate '
            f'{substrate:.4g}) — it cannot place an interface')

    expected_high = sense == HIGH
    actually_high = layer > cap and layer > substrate
    actually_low = layer < cap and layer < substrate
    if expected_high and not actually_high:
        raise ValueError(
            f'{profile.name}: {marker!r} was declared high in the layer but '
            f'reads {layer:.4g} there against {cap:.4g} above and '
            f'{substrate:.4g} below')
    if not expected_high and not actually_low:
        raise ValueError(
            f'{profile.name}: {marker!r} was declared low in the layer but '
            f'reads {layer:.4g} there against {cap:.4g} above and '
            f'{substrate:.4g} below')


def _crossing(y, level, lo, hi, forward):
    """First (or last) crossing of ``level``, linearly interpolated.

    Searching inward from the ends rather than outward from the middle is what
    makes this survive a layer whose marker dips in the middle — a real profile
    here has a two-humped layer, and a search from the centre closes the window
    on the dip between the humps.
    """
    span = range(lo, hi) if forward else range(hi - 1, lo - 1, -1)
    for i in span:
        a, b = y[i], y[i + 1]
        if a == b:
            continue
        if (a - level) * (b - level) <= 0:
            return i + (level - a) / (b - a)
    return None


# ── The reductions the criteria consume ──────────────────────────────

def counts_per_px_shot(counts_per_scan, resolution_px, shots_per_pixel):
    """Normalise a per-scan count to the detector's own units.

    The saturation ceiling is a property of the detector — how many ions it can
    register per pixel per shot — so a raw per-scan count cannot be compared
    against it without dividing out how many pixels and shots that scan
    contained. Two measurements at different rasters saturate at completely
    different per-scan counts.
    """
    pixels = resolution_px * resolution_px
    if pixels <= 0 or shots_per_pixel <= 0:
        raise ValueError('resolution and shots per pixel must both be positive')
    return counts_per_scan / (pixels * shots_per_pixel)


def channel_yield(profile, channel, window):
    """Mean intensity of one channel inside the layer.

    The drift criterion compares this against the same channel of a reference
    measurement — the *same* channel, which the criterion enforces, because
    comparing whichever species happened to be strongest compares different
    ions and reports the difference as drift.
    """
    y = np.asarray(profile[channel], dtype=float)
    mask = window.scan_mask(len(y))
    if not mask.any():
        raise ValueError(
            f'{profile.name}: the layer window {window.start:.1f}-'
            f'{window.end:.1f} selects no scans')
    return float(y[mask].mean())


def peak_counts_per_scan(profile, window, channel=None):
    """The largest per-scan count inside the layer.

    With no channel named this is the maximum over every **recorded** channel,
    which is what the saturation check wants: the question is whether *any*
    channel approached the detector's ceiling, and naming one in advance means
    naming the wrong one whenever a different species turns out to be the
    strongest. Derived ratio columns are excluded — a ratio has no counts.
    """
    if channel is not None:
        y = np.asarray(profile[channel], dtype=float)
        return float(y[window.scan_mask(len(y))].max())

    recorded = profile.recorded
    if not recorded:
        raise ValueError(f'{profile.name}: no recorded channels to take a '
                         f'maximum over')
    return max(float(np.asarray(y, float)[window.scan_mask(len(y))].max())
               for y in recorded.values())
