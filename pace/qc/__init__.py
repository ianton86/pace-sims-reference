"""Quality-control criteria applied at each checkpoint.

``criteria.QualityCriteria`` holds the thresholds and judges a measurement's
metrics; ``pace.decisions`` turns the resulting findings into one outcome from
a closed menu. See ``criteria`` for the standing caveat about what these rules
were during the study — instructions the agent was given, not code the engine
enforced.
"""

from .criteria import (FAIL, FLAG, MEASUREMENT, PASS, REFERENCE, SURVEY,
                       Finding, QualityCriteria)

__all__ = ['QualityCriteria', 'Finding', 'PASS', 'FLAG', 'FAIL',
           'SURVEY', 'REFERENCE', 'MEASUREMENT']
