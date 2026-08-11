# -*- coding: utf-8 -*-
"""
The two retrieval rules, enforced on the shipped guide.

``pytest knowledge/`` runs this. The rules are cheap to state and were both
learned by breaking them, and the failure in each case is the same shape: an
agent asks for a keyword, receives thousands of tokens of the wrong section, and
nothing anywhere reports a problem. A guide that violates either is worse than
one with the entry missing, because a missing entry is visible.

The examples in ``example_guide.md`` are also the deposit's statement of what a
knowledge entry *is*, so a couple of tests below check that they are entries and
not API documentation in disguise.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from knowledge.schema import (GuideError, headings, parse_toc, select,
                              validate, validate_or_raise)

GUIDE = (ROOT / 'knowledge' / 'example_guide.md').read_text(encoding='utf-8')


# ── The shipped guide obeys its own schema ───────────────────────────

def test_the_shipped_guide_is_valid():
    assert validate(GUIDE) == []


def test_the_table_of_contents_is_parsed_from_the_guide_itself():
    """Parsed rather than declared separately, so the advertised set cannot
    drift from what the guide shows a reader."""
    toc = parse_toc(GUIDE)
    assert set(toc) == {'stop', 'quality'}


@pytest.mark.parametrize('keyword', ['stop', 'quality'])
def test_each_keyword_returns_its_own_section_and_only_that(keyword):
    section = select(GUIDE, keyword)
    other = {'stop': 'Quality bar', 'quality': 'Stop conditions'}[keyword]
    assert keyword in section.split('\n', 1)[0].lower(), \
        'the first line returned should be the heading that was asked for'
    assert other.lower() not in section.lower(), \
        'a lookup should not drag in the section next to it'


def test_a_lookup_returns_the_subsections_of_its_section():
    """A section is the unit of knowledge, so its subsections come with it —
    returning a heading without its content would make the guide useless."""
    section = select(GUIDE, 'stop')
    assert 'Trigger-based' in section
    assert 'easy to get wrong' in section


def test_an_unknown_keyword_lists_what_is_available():
    """More useful to an agent than an empty string, which reads as a failure
    of the guide rather than of the keyword."""
    answer = select(GUIDE, 'sputtering')
    assert 'Available' in answer
    assert 'Stop conditions' in answer


# ── Rule 1: an advertised keyword appears in a heading ───────────────

def test_a_keyword_that_matches_no_heading_is_rejected():
    """It falls through to the body net and sweeps up every section that
    mentions the word. In the study's guides this returned 41 kB across four
    unrelated sections for one keyword and 16 kB for another, on every lookup,
    silently."""
    broken = GUIDE.replace('| `stop` | Stop conditions |',
                           '| `saving` | Stop conditions |')
    problems = validate(broken)
    assert any('saving' in p and 'no heading' in p for p in problems)


def test_the_advice_is_to_fix_the_heading_not_the_keyword():
    """The table of contents is what an agent is told to use, so renaming the
    keyword to match a heading fixes the test and breaks the interface."""
    broken = GUIDE.replace('| `stop` | Stop conditions |',
                           '| `saving` | Stop conditions |')
    assert any('do not rename the keyword' in p.lower()
               for p in validate(broken))


# ── Rule 2: a keyword retrieves the section it advertises ────────────

def captured_guide():
    """Reproduce the real defect: a ``## `` heading capturing a keyword whose
    entry is a ``### `` subsection.

    ``count`` is advertised for the *Fixed count* subsection. Renaming a
    section heading to contain "accounting" makes a ``## `` heading match the
    same keyword — and because section headings are searched before subsection
    headings, the subsection is never reached. This is the study's own failure
    in miniature: there, a keyword for a standalone-file workflow was a
    substring of "Profiles", and an agent asking for it received a depth-profile
    section with the entry it wanted entirely absent.
    """
    return (GUIDE
            .replace('| `quality` | Quality bar and acceptance |',
                     '| `quality` | Quality bar and acceptance |\n'
                     '| `count` | Fixed count | Fixed-count acquisition |')
            .replace('## Quality bar and acceptance',
                     '## Quality bar and acceptance, accounting for drift'))


def test_a_keyword_captured_by_another_heading_is_rejected():
    """The sharper rule, and the one a check of rule 1 alone passes."""
    problems = validate(captured_guide())
    assert any(p.startswith("'count'") and 'retrieves something else' in p
               for p in problems)


def test_rule_one_alone_would_have_passed_that():
    """Stated as its own test because it is the whole reason rule 2 exists."""
    captured = captured_guide()
    all_headings = [h.lower() for h in headings(captured)]
    assert any('count' in h for h in all_headings), \
        'the keyword does appear in a heading — rule 1 is satisfied'
    assert 'Fixed count' not in select(captured, 'count'), \
        'and the section it advertises is not what comes back'
    assert validate(captured), 'so the guide is still broken'


def test_every_problem_is_reported_not_just_the_first():
    """Fixing headings one failure at a time is how a keyword ends up renamed
    to match a heading instead of the other way round."""
    broken = GUIDE.replace('| `stop` | Stop conditions |',
                           '| `saving` | Stop conditions |'
                           ).replace('| `quality` | Quality bar and acceptance |',
                                     '| `grading` | Quality bar and acceptance |')
    assert len(validate(broken)) == 2


def test_validate_or_raise_carries_every_problem(monkeypatch):
    broken = GUIDE.replace('| `stop` | Stop conditions |',
                           '| `saving` | Stop conditions |')
    with pytest.raises(GuideError, match='saving'):
        validate_or_raise(broken)


def test_a_guide_with_no_contents_table_is_rejected():
    """Nothing in it is reachable, which is a worse state than an empty guide
    and looks identical from outside."""
    assert validate('# A guide\n\n## A section\n\nText.\n')


# ── The examples are entries, not API documentation ──────────────────

def test_the_two_examples_are_one_of_each_kind():
    """The point of shipping two is that a reader sees both kinds: one
    procedural, one judgement. The judgement kind is what the paper's claims
    rest on."""
    stop, quality = select(GUIDE, 'stop'), select(GUIDE, 'quality')
    assert 'How to choose it' in stop or 'how to choose' in stop.lower()
    assert 'Judgement, not fact' in quality


def test_the_judgement_entry_says_it_was_not_enforced():
    """The distinction the whole deposit turns on: a bound an agent could not
    exceed is a different kind of guarantee from a rule it was asked to follow.
    An entry that states thresholds without saying which it is invites the
    stronger reading."""
    quality = select(GUIDE, 'quality')
    assert 'instructions' in quality.lower()
    assert 'did not check' in quality.lower() or 'not enforcement' in quality.lower()
    assert 'safety envelope' in quality.lower()


def test_the_judgement_entry_points_at_the_executable_rules():
    """Prose is not checkable; the rules the paper depends on are also code."""
    quality = select(GUIDE, 'quality')
    assert 'pace/qc' in quality and 'pace/decisions.py' in quality


def test_the_entries_do_not_restate_function_signatures():
    """An entry is operating knowledge. Duplicating a signature here creates
    something that goes stale silently, and the code documents itself."""
    assert 'def ' not in GUIDE
    assert '(self' not in GUIDE
