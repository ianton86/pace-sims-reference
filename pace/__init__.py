"""PACE-SIMS — reference implementation of checkpoint-gated orchestration.

See the repository README for what this does and does not contain.
"""

from .sequence import (
    Command, ExperimentState, MeasurementParams, MeasurementSequence,
    MeasurementStep, NextAction, Peak, Polarity, StopCondition,
)
from .state_store import ExperimentStateFile, command_to_step, step_to_command
from .state_machine import Executor
from .stop_conditions import dynamic_stop_scan
from .safety import EnvelopeViolation, SafetyEnvelope
from .qc import Finding, QualityCriteria
from .decisions import Decision, MENU, decide
from .driver import InstrumentDriver, SimulatedInstrument

__all__ = [
    'Command', 'ExperimentState', 'MeasurementParams', 'MeasurementSequence',
    'MeasurementStep', 'NextAction', 'Peak', 'Polarity', 'StopCondition',
    'ExperimentStateFile', 'command_to_step', 'step_to_command',
    'Executor', 'dynamic_stop_scan',
    'EnvelopeViolation', 'SafetyEnvelope',
    'QualityCriteria', 'Finding', 'Decision', 'decide', 'MENU',
    'InstrumentDriver', 'SimulatedInstrument',
]
