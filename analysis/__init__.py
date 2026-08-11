"""Data reduction — only what the decision rules consume.

Deliberately narrow. The full analysis stack the study used is not published
(see the repository README); what is here is the path from a deposited depth
profile to the numbers ``pace.qc`` judges, because every reduction between the
data and a decision is something a reader has to take on trust, and a short
path is a cheaper thing to trust.
"""

from .profiles import (HIGH, LOW, LayerWindow, Profile, channel_yield,
                       counts_per_px_shot, layer_window, load_profile,
                       peak_counts_per_scan)

__all__ = ['Profile', 'LayerWindow', 'load_profile', 'layer_window',
           'channel_yield', 'counts_per_px_shot', 'peak_counts_per_scan',
           'HIGH', 'LOW']
