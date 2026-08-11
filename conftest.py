import os
import sys

# The deposit is run from its own root (`pytest`, `pytest replay/`, `python -m
# replay.harness`), so the root has to be importable for `pace`, `analysis` and
# `replay` to resolve. pytest loads this before collecting anything.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
