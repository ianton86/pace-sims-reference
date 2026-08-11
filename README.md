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
pytest replay/
jupyter lab notebooks/demo_run.ipynb
```

TODO: fill in once `replay/` and the demo notebook exist.

## Replaying the study

TODO — one command; explain that this reproduces every run-phase decision
reported in the paper from the deposited quality-control log.

## Architecture map

TODO — figures 2/3/4 of the paper with file-level pointers.

## Adapting this to another instrument

TODO — implement `InstrumentDriver`, point the analysis reductions at your raw
store, write your own knowledge entries.

## A note on what the decision code is

The quality-control rules in `pace/qc/` and the decision menu in
`pace/decisions.py` are the rules the agent was **instructed to apply** during
the study, expressed here as executable code. During the run they lived in the
agent's knowledge base rather than in the execution engine, and the engine did
not enforce them — the safety envelope in `pace/safety.py` is the part that was
enforced engine-side. The replay harness is therefore a faithful re-derivation
of the decisions from the logged metrics, not a recording of the code path that
made them at the time.
