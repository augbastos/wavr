# Benchmarks

A small, rerunnable harness for the Core's cost. It exists so that no
performance change in this repository is argued from intuition: every one of
them is a before/after pair of files in `results/`.

```powershell
pip install -e backend[dev,bench]                       # the harness's own interpreter
python -m venv .bench-base; .bench-base\Scripts\pip install ./backend   # a BASE install to measure
python benchmarks/bench.py --python .bench-base/Scripts/python.exe --label <name>
python benchmarks/bench.py --only pipeline --label <name>-pipeline      # needs httpx: dev interpreter
python benchmarks/bench.py --lan ...                    # also sweep the real local /24
```

| File | What it measures |
|---|---|
| `bench.py` | Imports, start-up phases, idle cost per scenario, install footprint. Orchestrates the rest. |
| `_core.py` | Launches a real Core, faking only the hardware edge (ARP sweep, BLE radio) on request. |
| `pipeline.py` | In-process: event → hub latency, fusion, SQLite, API payload size and latency. |
| `footprint.py` | Installed size per distribution of the interpreter it runs in. |
| `ab_imports.py` | Interleaved A/B of `import wavr.app` between two installs. |
| `ab_pipeline.py` | Interleaved A/B of `pipeline.py` between two source trees. |
| `native_footprint.py` | The native `wavr` binary: size, start-up beside Python, the Node role's RSS and CPU. |

Scenarios (`--scenarios`): `base`, `network`, `inventory`, `ble`, `ha` (a fake Home
Assistant served by the harness, four mapped entities), `sim`, `multi`. With `--lan`,
`network-lan` and `network+inventory-lan` sweep the machine's own /24 exactly as a
Core configured that way would.

## Reading the numbers

- **One machine.** Results describe the machine they ran on. Compare two result files
  from the same machine; never a number against a different machine's. The committed
  files deliberately record no hardware description.
- **CPU is process CPU-seconds**, which is robust to a busy machine; wall-clock
  latencies are not, so they are medians over repeats and the system load during each
  idle window is recorded beside it.
- **Child processes** are sampled every 50 ms, so their CPU is a lower bound — a
  `ping` that starts and exits between two samples is counted but not timed.
- **Cold cache is not measured.** Every repeat after the first runs with a warm OS
  file cache and compiled `.pyc` files.
- Nothing here gates CI. Structural regressions (a heavy import creeping back into the
  base path, a disabled source doing work) are guarded by tests in `backend/tests`,
  because those can fail deterministically; a wall-clock threshold cannot.
- **Wall-clock comparisons are interleaved.** Two runs taken minutes apart were not
  comparable here: fusion, which had not changed, came out 60% slower in the second.
  `ab_imports.py` and `ab_pipeline.py` alternate A and B round by round so drift lands
  on both, and the pipeline A/B carries its own controls -- metrics whose code is
  byte-identical in both trees. A difference smaller than the spread of those controls
  is not a result.
