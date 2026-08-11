# -*- coding: utf-8 -*-
"""
What a knowledge entry is, and the two rules that make one retrievable.

A knowledge base here is a **markdown guide**: a keyword table of contents
followed by ``## `` sections, some with ``### `` subsections. An agent does not
read the guide. It asks for one keyword and receives one section — so the unit
of knowledge is a section, and the retrieval keyword is part of the entry rather
than an index maintained beside it.

That shape is the whole schema, and it is deliberately thin. What makes it work
is not structure but two rules about the relationship between the table of
contents and the headings, both of which were learned by breaking them.

Rule 1 — an advertised keyword must appear in a heading
-------------------------------------------------------
``select`` looks at headings first and falls back to matching section *bodies*.
A keyword that matches no heading therefore falls through to the body net and
sweeps up every section that merely mentions the word. In the study's own guides
two keywords did: one advertised as ``save`` against a heading reading *"File
Saving"* — which does not contain "save" — returned 41 kB spanning four
unrelated sections instead of about 5 kB, and ``compare`` against *"Comparing
profiles"* returned 16 kB of every subsection naming a comparison function.

Both cost an agent thousands of tokens of mostly-wrong text on **every** lookup,
and neither raised anything. Fix a violation by putting the keyword in the
heading, never by renaming the keyword: the table of contents is what an agent
is told to use.

Rule 2 — a keyword must retrieve the section it advertises
-----------------------------------------------------------
Rule 1 is necessary and **not sufficient**, which is the sharper lesson. A
keyword can appear in a heading that belongs to a different section and be
captured by it: one guide advertised ``files`` for a standalone-file workflow,
and ``files`` is a substring of ``Profiles``, so a depth-profile section matched
first and the section the keyword advertised was never returned at all. An agent
asking about loose files got 12 kB about something else, with the answer
entirely absent — and a check of rule 1 alone passed the whole time, because the
keyword *did* occur in its own heading too.

So the check that matters is a round trip: ask for the keyword, and assert the
section you get back is the one the table of contents promised.

What belongs in an entry, and what does not
--------------------------------------------
An entry is **operating knowledge**: what to do, when, and what the failure looks
like if you get it wrong. It is not API documentation — the code documents
itself, and duplicating a signature here only creates something that can go
stale silently.

The distinction that matters most for reading this repository: an entry that
states a **judgement** — a threshold, an acceptance rule — is an *instruction*,
not an enforcement. During the study these lived here and the engine did not
check them. What the engine enforced is the safety envelope, and that is in code
(``pace/safety.py``). The quality rules an entry like ``quality`` describes are
published as executable code in ``pace/qc/`` precisely so the paper's claims do
not rest on prose.
"""

import re


def parse_toc(content):
    """Read the guide's table of contents: ``{keyword: section title}``.

    The table is markdown, and a keyword is written in backticks in its first
    column. Parsed rather than declared separately so the advertised set cannot
    drift from what the guide actually shows a reader.

    **Bounded to the contents section**, and that is not a detail. Scanning
    every table in the guide reads an entry's own parameter table as more
    contents rows — measured on this repository's example guide, a four-field
    parameter table inside one entry produced four phantom keywords, each of
    which then failed validation for naming no heading. The bug is the same
    shape as the one ``select`` documents about subsections: a structure that
    repeats inside sections has to be read within the section that owns it.
    """
    section = _contents_section(content)
    if section is None:
        return {}

    toc = {}
    for line in section.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) < 2:
            continue
        keyword = re.fullmatch(r'`([^`]+)`', cells[0])
        if keyword:
            toc[keyword.group(1)] = cells[1]
    return toc


def _contents_section(content):
    """The ``## `` section holding the keyword table, or None."""
    for chunk in content.split('\n## ')[1:]:
        if 'table of contents' in _first_line(chunk):
            return chunk
    return None


def headings(content):
    """Every ``## ``/``### `` heading, in order."""
    return [line.lstrip('#').strip() for line in content.splitlines()
            if line.startswith('## ') or line.startswith('### ')]


def select(content, topic):
    """Return the slice of the guide a keyword asks for.

    Heading-first at both levels, in this order, and the order is the design:

    1. a ``## `` section whose HEADING contains the topic;
    2. otherwise a ``### `` subsection whose heading contains it;
    3. otherwise a ``## `` section whose BODY contains it — the widest net, and
       the one rule 1 exists to keep an advertised keyword out of;
    4. otherwise a list of what is available, which is more useful to an agent
       than an empty string.

    A subsection candidate is bounded to its own ``## `` section. Splitting the
    whole document on ``### `` does not respect section boundaries, so a
    section's last subsection runs on until the next ``### `` anywhere in the
    file — swallowing every following section that has no subsections of its
    own. That is the mechanism behind the 41 kB result described above.
    """
    topic_lower = topic.lower()
    sections = content.split('\n## ')[1:]

    named = [s for s in sections if topic_lower in _first_line(s)]
    if named:
        return '\n\n## '.join([''] + _prefer_named_for(named, topic_lower)).strip()

    subsections = []
    for section in sections:
        subsections.extend(section.split('\n### ')[1:])

    named_sub = [s for s in subsections if topic_lower in _first_line(s)]
    if named_sub:
        return '\n\n### '.join([''] + _prefer_named_for(named_sub, topic_lower)).strip()

    body = [s for s in sections if topic_lower in s.lower()]
    if body:
        return '\n\n## '.join([''] + body).strip()

    return 'No section for that topic. Available:\n' + '\n'.join(
        f'  {h}' for h in headings(content))


def _first_line(chunk):
    return chunk.split('\n', 1)[0].lower()


def _prefer_named_for(chunks, topic_lower):
    """Narrow several heading matches to the section NAMED for the topic.

    Returning every heading match is usually right — a keyword can legitimately
    span two sections and dropping either answers half the question. It is wrong
    when the topic merely appears inside sections that are *about* something
    else, so the tie-break is narrow: it fires only when some heading *starts
    with* the topic. Leading numbering is skipped so ``Step 4: …`` can qualify.
    """
    def starts_with_topic(chunk):
        return _first_line(chunk).lstrip('0123456789.:# ').startswith(topic_lower)

    named = [c for c in chunks if starts_with_topic(c)]
    return named or chunks


# ── Validation ───────────────────────────────────────────────────────

class GuideError(AssertionError):
    """A guide breaks one of the two retrieval rules."""


def validate(content):
    """Check a guide against both rules. Returns the problems it found.

    A list rather than a raised exception, so a guide with several violations
    reports all of them: fixing headings one failure at a time is how a keyword
    gets renamed to match a heading instead of the other way round.
    """
    problems = []
    if _contents_section(content) is None:
        return ['The guide has no "Table of Contents" section, so nothing is '
                'retrievable by keyword.']
    toc = parse_toc(content)
    if not toc:
        return ['The guide\'s table of contents advertises no keywords, so '
                'nothing is retrievable.']

    all_headings = [h.lower() for h in headings(content)]

    for keyword, promised in sorted(toc.items()):
        if not any(keyword.lower() in h for h in all_headings):
            problems.append(
                f'{keyword!r} is advertised but appears in no heading, so it '
                f'falls through to the body net. Put the keyword in the '
                f'heading — do not rename the keyword.')
            continue

        # Rule 2: the round trip. Necessary because rule 1 passes when the
        # keyword is captured by some OTHER section's heading first.
        returned = select(content, keyword)
        if promised.lower() not in returned.lower():
            problems.append(
                f'{keyword!r} advertises {promised!r} but retrieves something '
                f'else — another heading matches first. Rename the OTHER '
                f'heading, or make this keyword more specific.')

    return problems


def validate_or_raise(content):
    problems = validate(content)
    if problems:
        raise GuideError('\n'.join(problems))
