"""``python -m replay`` — re-decide the study and print the table.

A separate entry point rather than a ``__main__`` block inside ``harness``:
running a module that the package has already imported makes Python execute it
twice under two names, which it warns about, and the warning is the first thing
a reader would see.
"""

from .harness import replay, report

print(report(replay()))
