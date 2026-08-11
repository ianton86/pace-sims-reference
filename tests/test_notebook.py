# -*- coding: utf-8 -*-
"""
The demo notebook has to actually run.

The README points a first-time reader at it, so a notebook that raises halfway
is worse than no notebook: it is the first thing anyone tries and the first
impression of whether the deposit works at all. Nothing else in this suite
would catch it — the notebook is the one place where the pieces are composed the
way a reader composes them.

Executed rather than merely parsed, and executed against the repository as it
is rather than a recorded copy of its outputs. The cells carry no stored output
for that reason: a stored output is a claim that can go stale silently, and this
test is the claim instead.
"""

import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NOTEBOOK = ROOT / 'notebooks' / 'demo_run.ipynb'


def code_cells():
    nb = json.loads(NOTEBOOK.read_text(encoding='utf-8'))
    return [(i, '\n'.join(c['source'])) for i, c in enumerate(nb['cells'])
            if c['cell_type'] == 'code']


def test_the_notebook_exists_and_has_code():
    assert NOTEBOOK.exists(), 'the README points readers here'
    assert len(code_cells()) >= 5


def test_every_cell_runs(monkeypatch, capsys):
    """Run the cells in order, in one namespace, as a reader would."""
    monkeypatch.chdir(NOTEBOOK.parent)
    namespace = {'__name__': '__main__'}
    for index, source in code_cells():
        try:
            exec(compile(source, f'<cell {index}>', 'exec'), namespace)
        except Exception as exc:
            captured = capsys.readouterr()
            pytest.fail(f'cell {index} raised {type(exc).__name__}: {exc}\n'
                        f'--- cell ---\n{source}\n--- output ---\n'
                        f'{captured.out}')


def test_no_stored_outputs():
    """A stored output is a claim that can go stale without anything failing;
    `test_every_cell_runs` is the claim instead."""
    nb = json.loads(NOTEBOOK.read_text(encoding='utf-8'))
    stored = [i for i, c in enumerate(nb['cells'])
              if c.get('cell_type') == 'code' and c.get('outputs')]
    assert stored == [], f'cells {stored} carry stored output'
