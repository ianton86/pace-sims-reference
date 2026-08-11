"""The instrument boundary, and a stand-in that satisfies it.

``base.InstrumentDriver`` is the public shape of the part of this system that is
not published; ``simulator.SimulatedInstrument`` is a complete implementation
with no hardware behind it, which is what lets the whole lifecycle run here.
"""

from .base import InstrumentDriver
from .simulator import Measurement, SimulatedInstrument

__all__ = ['InstrumentDriver', 'Measurement', 'SimulatedInstrument']
