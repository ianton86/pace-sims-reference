# Deposited measurement data

**Licence: CC BY 4.0** — see `../LICENSE-DATA`. This is *not* the MIT licence
that covers the rest of the repository; see `../LICENSE` for why the two halves
differ.

What is here is what `replay/` re-decides. Everything a decision turns on is
derived from these profiles; the CSV carries only what no profile can contain.

## `profiles/` — 35 depth profiles, one per measurement

Tab-separated, one row per scan, with a `#`-prefixed header. The first two
columns are the depth axis and the scan index; the rest are per-scan intensities
of each recorded species, followed by derived ratios (a column whose name
contains `=`). Nothing in the replay consumes a derived column — every reduction
starts from a recorded species.

The file name is the measurement id, and it is the key `measurements.csv` joins
on.

## `measurements.csv` — one row per measurement

Facts about the **acquisition**, which a profile cannot carry:

| Column | Meaning |
|---|---|
| `id`, `order` | measurement id and its position in the acquisition sequence |
| `sample`, `polarity` | which specimen, and the acquisition polarity |
| `role` | `survey`, `reference` or `measurement` — which quality criteria apply |
| `resolution_px`, `shots_per_pixel` | the raster, needed to normalise counts to the detector's own units |
| `sputter_frames` | the parameter a sampling correction adjusts |
| `layer_marker`, `layer_marker_sense` | which species bounds the layer, and whether it is high or low inside it |
| `yield_channel` | the single named channel drift is tracked on |
| `stopped_by` | `dynamic` (stopped on its trigger) or `scan_limit` (hit its ceiling) |
| `source_excursion` | whether an ion-source excursion was recorded during the acquisition |
| `uniformity` | lateral uniformity from the layer-integrated map, where measured |
| `recorded_decision` | **the comparison target** — what was decided at the checkpoint |
| `recorded_label` | the free-text label written at the time |

## `recorded_decision` is compared against, never read

It is the answer, not an input. Replaying from the study's own quality-control
log would be circular — that log holds the metrics and the decisions side by
side — which is why this pair of files exists instead of a copy of it. A test in
`replay/` asserts no decision-relevant metric carries any trace of it.

## One limit worth knowing before you use these

The **saturation** figure derivable from these profiles is a lower bound. The
study's busiest channel was a primary-related positive species that this export
does not carry — it holds the isotope and matrix channels the measurement was
*about*. Both the published channels and the study's own unpublished figure sit
far under the detector ceiling, so the conclusion holds; the study's number
simply cannot be re-derived from this deposit alone.
