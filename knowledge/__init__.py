"""The curated knowledge corpus, by example.

The full base the study used is not published -- it encodes facility-specific
operating procedure. ``example_guide.md`` gives two sanitised entries in the
real format so the granularity is clear, and ``schema.py`` states the two rules
that make a guide retrievable at all.
"""

from .schema import GuideError, parse_toc, select, validate, validate_or_raise

__all__ = ["select", "validate", "validate_or_raise", "parse_toc", "GuideError"]
