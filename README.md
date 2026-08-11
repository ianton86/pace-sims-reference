# PACE-SIMS — reference implementation

> **Status: work in progress, not yet released.** This repository is being
> prepared as the code-availability deposit for a forthcoming paper. It is
> private, carries no licence yet (see `LICENSE`), and sections marked TODO are
> not finished. Do not make it public until the licence is in place and the
> pre-publication checks have been run.

## What this is

A reference implementation of the checkpoint-gated orchestration described in
[paper — DOI TODO]: the execution **state machine**, the **quality-control
criteria** and **decision framework** applied at each checkpoint, the **safety
envelope** the engine enforces, the **experiment state schema**, and a
**simulator** that stands in for the instrument so the whole lifecycle runs with
no hardware.

The centrepiece for a reader is `replay/`: it re-runs the real decision code
over the study's logged measurement metrics and reproduces the run's decision
sequence, so the paper's central claims are checkable without an instrument and
without our data.

## What this deliberately is not

Three things are omitted, and it is worth being precise about which:

* **The production instrument driver.** It wraps proprietary, vendor-internal
  interfaces of the acquisition software, and drives a shared user-facility
  instrument where an incorrect command has physical consequences. It is not
  redistributable. Its public boundary is the `InstrumentDriver` abstract base
  class in `pace/driver/base.py`; pseudocode for the production implementation
  is in the paper's supporting information [SI section TODO].
* **The curated knowledge corpus.** The full experiment- and analysis-knowledge
  bases encode facility-specific operating procedure. What the paper's claims
  depend on — the quality-control rules the agent was instructed to apply — is
  published here as executable code in `pace/qc/` and `pace/decisions.py`;
  the surrounding corpus is not. `knowledge/` gives the entry schema and a
  couple of sanitised examples so the granularity is clear.
* **The live plotting and analysis stack.** Not load-bearing for any claim in
  the paper. `analysis/` contains only the reductions the decision rules
  consume.

## Quickstart

```bash
conda env create -f environment.yml && conda activate pace-sims
pytest                       # the whole suite, no hardware needed
pytest replay/               # just the study replay
python -m replay             # the replay, as a readable table
jupyter lab notebooks/demo_run.ipynb
```

`notebooks/demo_run.ipynb` runs one checkpoint-gated experiment end to end
against the simulator: a plan, the safety envelope refusing an operator's
out-of-range setpoint and clamping an automatic one, an acquisition ending on
its dynamic trigger, a hold at a checkpoint, the quality rules and the decision
menu, and a retune written back through the plan file. It carries no stored
output — `tests/test_notebook.py` executes it instead, so what it claims cannot
go stale.

## Replaying the study

`python -m replay` re-decides all 35 of the study's measurements from
the deposited depth profiles in `data/profiles/`, using the same criteria
(`pace/qc/`) and the same decision menu (`pace/decisions.py`) the run was
steered by. It reproduces **35/35** of the accept-or-reject decisions and
**33/35** of the finer outcomes, including all five rejections with the remedy
each one called for — the three sputter-rate corrections at the frame counts the
study actually used, the repeat after the ion-source excursion, and the
escalation when the repeat came back low.

The two finer-grade differences are stated in `replay/test_replay.py` rather
than tuned away, because a threshold fitted to reproduce a set of labels has
stopped being a criterion. Both concern the drift flag: one measurement the
study flagged sits just under the threshold when drift is measured as the mean
over the layer instead of the peak, and one drift anchor the study labelled
plainly is flagged here.

**What this shows.** That the decision sequence was a consequence of stated
criteria applied to the data, rather than of judgement that cannot be
inspected. **What it does not show:** that the rules ran as code at the time —
they did not (see the note at the end of this README) — nor a revalidation of
the science, since the metrics are re-derived here by a simpler reduction than
the study's own.

Everything the decisions turn on is **derived from the profiles**: the layer
window, the points across the layer, counts per pixel per shot, the channel
yield. `data/measurements.csv` carries only what no profile can contain — the
raster, the stop reason, whether an ion-source excursion was recorded, the
crater uniformity, and each measurement's role. Its `recorded_decision` column
is compared against and never read as input; replaying off the study's own
quality-control log would be circular, since that file holds the metrics and
the decisions side by side.

## Architecture map

```
                    an agent, a notebook, a person
                              |
                    pace/client.py            reads and amends the plan;
                              |               compare-and-swap on every write
                    experiment.json  <---->  pace/state_store.py
                              |               the file IS the protocol
                    pace/state_machine.py     one command per tick; holds at a
                              |               checkpoint instead of running on
        +---------------------+---------------------+
        |                     |                     |
  pace/safety.py      pace/driver/base.py     pace/stop_conditions.py
  bounds every        the instrument          when to stop acquiring
  destructive         boundary: 4 methods
  parameter                  |
                    pace/driver/simulator.py  (the production driver is not
                                               published — see above)

  after each measurement, at a checkpoint:

     analysis/  ->  pace/qc/  ->  pace/decisions.py  ->  back to the client
     reduce a       judge it      one outcome from        as a plan edit
     profile                      a closed menu
```

Reading order, if you only read three files: `pace/state_machine.py` for the
control flow, `pace/safety.py` for what the engine enforces regardless of what
is steering it, and `replay/harness.py` for how the study's decisions are
checked.

| Concept | Where it lives |
|---|---|
| Checkpoint-gated execution | `pace/state_machine.py` — `Executor.run`, the persistent idle-wait |
| The plan as a two-writer file | `pace/state_store.py` and `pace/client.py`, taken together |
| Mechanically enforced bounds | `pace/safety.py` |
| Provenance deciding reject vs clamp | `pace/sequence.py` (`Command.origin`), applied in `pace/safety.py` |
| Quality-control criteria | `pace/qc/criteria.py` |
| The closed decision menu | `pace/decisions.py` |
| The instrument boundary | `pace/driver/base.py` |
| Reproducing the study's decisions | `replay/` |

## Adapting this to another instrument

The work is one file. Subclass `InstrumentDriver` (`pace/driver/base.py`) and
implement four methods — `measure_now`, `set_temperature`, `move_stage`,
`shutdown` — plus `time_series` if your plans use one. Two rules matter more
than the rest, and both are in that file's docstring: a handler returns
`NextAction.IDLE` until it is finished and **must not block**, because the same
loop polls for the stop command and writes the telemetry anything watching the
run depends on; and a handler raises on failure rather than returning an error.
`pace/driver/simulator.py` is a complete worked example.

Then three things need declaring rather than porting, because each is a
property of your instrument and sample rather than of this code:

* **The safety envelope.** Nothing is defaulted here on purpose — an undeclared
  bound is unconstrained rather than a guess at what is safe for hardware
  nobody described. Declare yours with `SafetyEnvelope`.
* **The quality thresholds.** Every number in `QualityCriteria` is a
  constructor argument and every default is the study's own, measured on one
  sample with one ion source. `criteria.describe()` prints the whole standard
  as a table; treat that as the thing you replace.
* **The reductions.** `analysis/` turns a depth profile into the metrics the
  criteria judge. If your data does not arrive as a per-scan table of species
  intensities, this is the layer to repoint — `layer_window` in particular
  needs a marker species and whether it is high or low inside your layer.

What does *not* need changing: the executor, the plan file, the decision menu,
and the provenance rule.

## A note on what the decision code is

The quality-control rules in `pace/qc/` and the decision menu in
`pace/decisions.py` are the rules the agent was **instructed to apply** during
the study, expressed here as executable code. During the run they lived in the
agent's knowledge base rather than in the execution engine, and the engine did
not enforce them — the safety envelope in `pace/safety.py` is the part that was
enforced engine-side. The replay harness is therefore a faithful re-derivation
of the decisions from the logged metrics, not a recording of the code path that
made them at the time.
