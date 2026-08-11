# Example knowledge guide

Two entries from a curated knowledge base, in the real format, sanitised. The
full base the study used is not published — it encodes facility-specific
operating procedure — and these are here so the **granularity** is clear: what
one entry covers, how much of it is judgement rather than fact, and what an
agent is expected to do with it.

They are chosen as one of each kind. `stop` is **procedural**: how a mechanism
behaves and how to configure it. `quality` is **judgement**: what to accept and
what to do about a rejection. The second kind is what the paper's claims rest
on, which is why those particular rules are also published as executable code in
`pace/qc/` and `pace/decisions.py` — see the note at the end of that entry.

See `knowledge/schema.py` for the two rules a guide has to satisfy to be
retrievable at all, and `knowledge/test_knowledge.py` for them enforced on this
file.

## Quick Reference (Table of Contents)

Ask for one keyword; you get one section. Do not read the whole guide.

| Topic keyword | Section | What's in it |
|--------------|---------|-------------|
| `stop` | Stop conditions | Fixed-count vs trigger-based acquisition, the three semantics that are easy to get wrong, choosing a threshold |
| `quality` | Quality bar and acceptance | The five acceptance criteria, what each one catches, the decision menu, and what to do at a checkpoint |

## Stop conditions

**A depth profile has no natural length.** The layer you are measuring is
usually the thing whose thickness you do not yet know, so a fixed scan count
either cuts the profile short or spends beam time in the substrate. Prefer a
trigger.

### Fixed count

Stops after a set number of scans. Use it for a **reconnaissance** run, where
the point is to find out how fast the material sputters and there is nothing yet
to trigger on, and for a **reference** run configured to characterise a channel.
Do not use it for a measurement you intend to compare with others: it stopped
for an arbitrary reason, so its depth axis is not anchored on anything in the
sample.

### Trigger-based

Watches one species and stops a set number of scans after that species does
something — typically the substrate marker rising as the layer is sputtered
through. Configure four things:

| Field | What it does | How to choose it |
|---|---|---|
| `label` | which species is watched | the substrate's marker, not the layer's |
| `threshold` | the level counted as crossed | well above the in-layer background of that species, well below its substrate level; look at a reconnaissance profile before choosing |
| `trigger_count` | how many crossing **events** are required | 1 for a single clean interface; 2 if the profile has a feature that can cross once spuriously |
| `post_scans` | scans acquired after the trigger fires | enough to establish the substrate level; this is also what makes the profile's end comparable between runs |

Always set a `max_scans` ceiling as a backstop. Reaching it means the trigger
never fired, which is a fault to investigate rather than a result — see the
termination criterion under `quality`.

### Three semantics that are easy to get wrong

These cost real measurements when misread, and none of them announces itself.

1. **`trigger_count` counts crossing EVENTS, not scans above the threshold.**
   One event is a below→above transition. Reading it as "N consecutive scans
   above" makes a single noisy pair of scans look like a layer boundary, and the
   acquisition stops in the middle of the layer.
2. **The scan in progress is excluded.** Its counts are still accumulating, so
   they are partial — and a partial scan reads as a *fall*, which on a falling
   trigger is a crossing that never happened.
3. **The start of the examined window counts as not-yet-crossed.** A signal
   already above the threshold on the first scan examined is therefore the first
   event. Without that, a layer beginning inside an ignored surface region is
   missed silently and the run goes to its ceiling.

The rule is implemented in `pace/stop_conditions.py`, and each of those three
has its own test there.

## Quality bar and acceptance

**Judgement, not fact.** Everything below is a decision rule you are being asked
to apply, with thresholds that belong to one sample, one ion source and one
geometry. Re-derive them for different hardware. Read the note at the end before
treating any of it as enforced.

### The five criteria

Each exists because it catches a failure that is **invisible in the finished
profile** — something that produces a plausible-looking measurement of the wrong
thing.

| Criterion | Catches | Typical bar |
|---|---|---|
| Depth sampling | a layer under-sampled (shape unresolved) or over-sampled (beam time spent for nothing) | 50–60 points across the layer |
| Saturation | a detector at its ceiling, which reports a property of the detector and looks like a strong signal | under 5 counts/pixel/shot; watch from 3 |
| Lateral uniformity | a crater sampling unevenly, which reads as a broadened interface rather than as misalignment | U ≤ 1.3, where U ≈ 1 is counting-statistics limited |
| Termination | a run that hit its scan ceiling instead of its trigger, so its depth axis means something different from every other run's | stopped on its trigger |
| Source stability | an ion-source excursion changing the yield mid-run, which reads as a real change in composition with depth | no excursion; yield within 20 % of the reference |

**Judge a measurement by its role.** A reconnaissance run is deliberately
under-sampled and deliberately fixed-count — it is the run that *measures* the
sputter rate the sampling correction is computed from, so failing it for being
outside the band is circular. A reference run establishes the baseline and
cannot be compared against one. Only ordinary measurements face the full
standard.

**Compare drift within a sample.** Yields are not comparable across acquisition
polarities, and they are not comparable across samples either when the drift
channel is related to the quantity being measured — judging one sample against
another then reports the measurand as drift. This is why drift anchors belong on
a spare position of one sample rather than wherever is convenient.

**Never let a rejected measurement become a baseline.** Otherwise a run
invalidated for being out of band becomes the reference its own replacement is
judged against, and everything after it inherits the fault as normal.

### Deciding what happens next

Choose exactly one outcome. Keeping the set closed is what makes a run's
decisions auditable afterwards — "what could it have done?" has an answer.

| Outcome | When |
|---|---|
| Accept | every criterion passed |
| Accept with a flag | usable for the ratio being measured, but carrying drift that would matter to an absolute comparison. Record the reservation; the distinction is the reader's to make later |
| Retune and repeat | a parameter can be corrected — e.g. sputter rate scaled toward the middle of the sampling band. Re-check the corrected run rather than assuming it landed |
| Repeat | the cause was **transient**: an excursion was recorded during the acquisition, so a repeat is likely to be clean |
| Escalate | the cause is **sustained**: no excursion was recorded and the yield is still low, so a repeat would reproduce it. Ask a person |

The last two are the pair worth getting right. Repeating a sustained fault burns
beam time until it runs out and produces nothing; escalating a transient one
wakes somebody for a measurement that would have been clean on the second
attempt.

### Report the metric alongside the verdict

State the number, not just the judgement, and state it explicitly when a bar was
**not** met. A measurement accepted by default that missed its requirement is
the exact failure the bar exists to prevent, and it is indistinguishable
afterwards from one that passed.

### What this entry is, and is not

These rules were **instructions**, not enforcement. During the study they lived
in a knowledge base like this one, an agent was told to apply them, and the
execution engine did not check any of them. What the engine did enforce is the
safety envelope — the bounds on temperature and stage motion — and that
distinction matters when reading any claim about the run: a bound an agent could
not exceed is a different kind of guarantee from a rule it was asked to follow.

Because the paper's claims depend on these rules, they are additionally
published as executable code — `pace/qc/criteria.py` for the criteria and
`pace/decisions.py` for the menu — so they can be checked rather than taken on
trust. `replay/` re-runs them over the study's deposited data.
