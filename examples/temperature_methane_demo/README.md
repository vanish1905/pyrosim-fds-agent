# TEMPERATURE methane flame end-to-end example

This example stores the unmodified official `methane_flame_simple_2` FDS input, the completed solver output, and the `theng` post-processing products from one local Agent/MCP workflow.

## Reproduction record

- Workflow ID: `df7a0dc462db`
- Case: `methane_flame_simple_2` (official FDS example; staged into the case workspace without editing the input)
- Solver: FDS `FDS-6.9.1-0-g889da6a-release`
- Requested/actual end time: 10.0 s
- Solver steps: 1000
- Solver exit: 0 (`STOP: FDS completed successfully`)
- Solver elapsed time reported by the workbench: 12.48 s
- `theng` analysis: successful with `theng.FDSModel` / theng 0.1.8
- Peak HRR from `analysis/summary.json`: 130.08987 kW at 0.64999998 s; 1000 HRR samples
- TEMPERATURE and HRRPUV slice outputs are included among the `.sf`/`.s3d` files.

## Files

- `run/methane_flame_simple_2.fds`: input used by the successful run
- `run/methane_flame_simple_2.smv`: Smokeview case descriptor
- `run/methane_flame_simple_2_hrr.csv`: HRR time series
- `run/analysis/summary.json`: machine-readable `theng` summary
- `run/analysis/hrr.html`: interactive HRR plot
- Other FDS logs, CSVs, and slice files are retained in `run/`.

Open `run/methane_flame_simple_2.smv` with Smokeview/Results to inspect slice data. In this validation, `open_simulation_visualization` requested the viewer launch, but `results_ui_inspect` found zero Results windows; therefore the sample records successful solver and post-processing, but does not claim the Results GUI load or rendered layer was verified.

To rerun through the Agent, open the workbench and ask it to use the fixed official methane flame example for a TEMPERATURE/HRRPUV workflow. The Agent should preflight and prepare first, then wait for approval before starting FDS.
