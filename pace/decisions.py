# -*- coding: utf-8 -*-
"""
The decision menu: what a checkpoint may do about what it found.

``pace.qc`` judges a measurement; this decides what happens next. They are
separate because they answer different questions and are wrong in different
ways — a criterion can be mis-calibrated, a decision can be mis-scoped — and
because a closed **menu** of outcomes is itself a claim the paper makes.

Why a closed menu
-----------------
The set of things a checkpoint may do is fixed and enumerable, and that is a
design decision rather than a description. An open-ended controller can respond
to a bad measurement in unlimited ways, and "what could it have done?" then has
no answer. With five outcomes the run's decision sequence is a sequence of
labels: it can be logged, replayed, compared against what a person would have
chosen, and audited afterwards. Every branch below ends in exactly one of them.

The five
--------
``ACCEPT``              Keep the measurement; continue with the plan unchanged.
``ACCEPT_WITH_FLAG``    Keep it and record a reservation. Used where a
                        measurement is usable for the ratio being measured but
                        carries drift that would matter to an absolute
                        comparison — the distinction is the reader's to make
                        later, so it must reach the record.
``RETUNE_AND_REPEAT``   Reject it, change an acquisition parameter, run it
                        again. The only outcome that edits the plan.
``REPEAT``              Reject it and run it again unchanged, because the cause
                        was transient.
``ESCALATE``            Stop and ask a person. The cause is sustained, so
                        repeating reproduces it.

The distinction that costs the most to get wrong is ``REPEAT`` against
``ESCALATE``. Repeating a sustained fault burns beam time until it runs out and
produces nothing; escalating a transient one wakes somebody at 3 a.m. for a
measurement that would have been clean on the second attempt. The study did both
in sequence — a dropout was repeated, the repeat came back low, and *that*
escalated — which is what the rule below encodes.

Not enforced, and deliberately so
---------------------------------
These are the decisions the agent was **instructed** to make, written as code so
they can be checked. During the study the engine did not enforce them; it held
the run at the checkpoint and the agent decided. The one thing that *was*
enforced is the safety envelope (``pace.safety``), and conflating the two would
overstate the result: the claim is that a bounded agent made good decisions, not
that it was unable to make bad ones.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .qc.criteria import FAIL, FLAG


ACCEPT = 'accept'
ACCEPT_WITH_FLAG = 'accept_with_flag'
RETUNE_AND_REPEAT = 'retune_and_repeat'
REPEAT = 'repeat'
ESCALATE = 'escalate'

MENU = (ACCEPT, ACCEPT_WITH_FLAG, RETUNE_AND_REPEAT, REPEAT, ESCALATE)

# Which outcome wins when findings disagree. Ordered by how much they cost to
# get wrong rather than by severity of the fault: continuing on a bad
# measurement is worse than repeating a good one, and repeating something a
# person needs to look at is worse than either.
_PRECEDENCE = {ACCEPT: 0, ACCEPT_WITH_FLAG: 1, RETUNE_AND_REPEAT: 2,
               REPEAT: 3, ESCALATE: 4}


@dataclass
class Decision:
    """What the checkpoint decided, and everything needed to justify it later."""
    outcome: str
    reasons: List[str] = field(default_factory=list)
    changes: Dict[str, Any] = field(default_factory=dict)
    findings: List[Any] = field(default_factory=list)

    @property
    def accepted(self):
        return self.outcome in (ACCEPT, ACCEPT_WITH_FLAG)

    @property
    def repeats(self):
        return self.outcome in (RETUNE_AND_REPEAT, REPEAT)

    def __str__(self):
        summary = self.outcome
        if self.changes:
            summary += ' (' + ', '.join(f'{k}={v}' for k, v in
                                        sorted(self.changes.items())) + ')'
        return summary + ''.join(f'\n  - {r}' for r in self.reasons)


def decide(findings):
    """Turn a set of findings into one outcome from the menu.

    Every failing finding is considered, not just the first: a measurement can
    be out of its depth-sampling band *and* have had a source excursion, and
    repeating it at a corrected sputter rate would waste the correction on a run
    that was going to be spoiled anyway. The most costly-to-get-wrong outcome
    wins, and every finding's reason is carried whether or not it decided the
    result — the record has to explain the measurement, not just the verdict.
    """
    outcome = ACCEPT
    reasons = []
    changes = {}

    for finding in findings:
        if finding.verdict == FLAG:
            reasons.append(str(finding))
            outcome = _worse(outcome, ACCEPT_WITH_FLAG)
        elif finding.verdict == FAIL:
            reasons.append(str(finding))
            outcome = _worse(outcome, _outcome_for(finding, changes))

    # A parameter change only survives if the decision it belongs to won. A
    # run being escalated must not carry a retune with it: the person looking
    # at it decides what changes, and a half-applied correction sitting in the
    # plan is exactly the state nobody expects to inherit.
    if outcome != RETUNE_AND_REPEAT:
        changes = {}

    return Decision(outcome, reasons, changes, list(findings))


def _outcome_for(finding, changes):
    """The outcome one failing finding calls for."""
    remedy = finding.remedy or {}

    action = remedy.get('action')
    if action == 'escalate':
        return ESCALATE
    if action == 'repeat':
        return REPEAT

    # A remedy naming parameters is a retune; one naming nothing is a fault
    # with no automatic fix, which is a person's problem.
    parameters = {k: v for k, v in remedy.items() if k != 'action'}
    if parameters:
        changes.update(parameters)
        return RETUNE_AND_REPEAT
    return ESCALATE


def _worse(current, candidate):
    return candidate if _PRECEDENCE[candidate] > _PRECEDENCE[current] else current
