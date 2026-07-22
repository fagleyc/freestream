# Example run sheets

`Freestream_RunSheet_Template.xlsx` is the **standardized run-sheet workbook** —
the primary import format for Freestream. Import it from the Sweep Planner
(**Import Run Sheet…**) or **File → Import Run Sheet…**; a selection dialog lets
you pick a run (or "all enabled runs") and loads its expanded test matrix into
the planner.

`freestream_sweep_parser.py` is a tiny standalone reference implementation of
the sweep-cell grammar (the canonical version now lives in
`freestream/sweepgrammar.py`).

## The workbook (5 sheets)

| Sheet | Purpose |
| --- | --- |
| **Guide** | Documentation of the grammar and execution order (read-only). |
| **Test Info** | Header fields (facility, model, engineer, dates, objectives…) plus the **model reference dimensions** (`Sref`, `cref`, `bref`, `MRC_x/y/z`) used downstream for coefficient reduction. These are merged into the recorded metadata on import. |
| **Run Matrix** | One row per run: `run, enable(Y/N), alpha, beta, mach, samples, sample_rate_hz, config, notes`. `alpha`/`beta`/`mach` cells use the sweep grammar below. `samples`/`sample_rate_hz` override the global acquisition for that run. |
| **Model Configs** | One row per named configuration (`clean`, `flaps10`, …). Columns are model-specific and **extensible** — add as many as the model needs; every column rides verbatim into the per-point metadata. Reference a config by name in the Run Matrix. |
| **Named Arrays** | Reusable sweep definitions (e.g. `alpha_fine = -4:1:16`) referenced from any axis cell as `@alpha_fine`. |

## Sweep grammar (`alpha`, `beta`, `mach` cells)

| Spec | Example | Expands to |
| --- | --- | --- |
| single number | `5` | `[5]` |
| comma list | `0,2,4` | `[0, 2, 4]` |
| range `start:delta:end` | `-4:2:8` | `[-4, -2, 0, 2, 4, 6, 8]` — the **middle** value is the step/delta; end is inclusive |
| return sweep (`R` suffix) | `0:2:10R` | `[0, 2, …, 10, 8, …, 0]` — hysteresis; the two legs are tagged `up`/`dn` in filenames and metadata (`sweep_dir`, `alpha_dot`) |
| mix | `-4:2:8, 10, 12` | comma-join any of the above |
| named | `@alpha_fine` | a row on the Named Arrays tab |
| file | `csv:aoa.csv` | one column read from an external CSV |

Notes:

* Range is `start:delta:end` (MATLAB-style colon). If the delta does not hit
  the end exactly, the last partial point is dropped (`0:2:9` → `0,2,4,6,8`).
* **Mach**: `0` (air-off) is **automatically prepended** to every Mach array, so
  each configuration records a wind-off reference first — you don't type the 0
  yourself. `mach = 0.3,0.5,0.7` runs at M = 0, 0.3, 0.5, 0.7.
* **Execution order** within a run: Mach is the outer loop (air-off first, then
  ascending), then beta, then alpha innermost/fastest. A return (`R`) sweep
  reverses only the axis it is written on.

## Flat single-sheet fallback

`freestream.runsheet.load_runsheet()` still loads a **flat** single-sheet
`.csv`/`.xlsx` run sheet (row 1 = headers; recognized columns `alpha`, `beta`,
`mach`, `dwell_s`, `samples`, `air_state`, `rpm`; every other column inherited
verbatim into metadata). This legacy path uses the same axis grammar but does
**not** auto-prepend the air-off Mach point. The workbook above is the primary
path.

Per-point output filenames follow
`run_NNNN[_alpha_{a}][_beta_{b}][_mach_{m:.2f}][_up|_dn].h5`,
e.g. `run_0012_alpha_2.0_beta_0.0_mach_0.30.h5`.
