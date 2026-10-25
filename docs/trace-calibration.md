# Trace calibration (M3)

M3 chooses the traces and simulation lengths evolution will use. `smoke-v1` exercises the plumbing, but its one trace hardly uses the LLC, so every policy scores within 0.2% of LRU (`docs/status.md` issue 6). This page records:
- where the traces come from
- the rules for choosing traces and a length, fixed before any results
- the results and the decisions they led to

## 1. The trace suite: ArchAgent's 19 CRC-2 traces

We use the same traces as **ArchAgent** (arXiv 2602.22425), which evolved LLC replacement policies with AlphaEvolve and ChampSim:
- **The traces:** 19 memory-intensive SPEC CPU2006 SimPoints from the 2nd Cache Replacement Championship (CRC-2, 2017).
- **The admission rule:** following Mockingjay, it kept only workloads with an LLC MPKI above 1 under LRU.
- **Fitness:** the geometric mean of per-workload IPC speedups over LRU, like ours (contract §4).

```
GemsFDTD_109B  astar_163B      bwaves_1861B    bzip2_183B      cactusADM_734B
calculix_2670B gcc_13B         lbm_94B         leslie3d_1116B  libquantum_1210B
mcf_46B        milc_360B       omnetpp_340B    soplex_66B      sphinx3_2520B
tonto_2834B    wrf_1212B       xalancbmk_748B  zeusmp_600B
```

**Where they come from:**
- None of these 19 is on the DPC-3 trace server. Its 94 SPEC CPU2006 SimPoints are all different ones.
- They are in the CRC-2 Dropbox folder that ChampSim's README links as `http://bit.ly/2t2nkUj`.
- Dropbox serves that folder only as one generated zip (`CRC2_trace.zip`, 28.07 GB). Links to single files return an HTML page.

**Fetching them:** `make traces-fetch` (`scripts/traces_fetch_crc2.py`, on coe-hpc1) does this:
1. Downloads the zip once into `$AFAR_ASSETS/traces/crc2/`, on `/data`.
2. Lists every member in `configs/traces/crc2-folder-index.txt`.
3. Extracts only the 19, letting zipfile check each one's CRC-32.
4. Pins each trace in `configs/traces/crc2-archagent19.json` by sha256, size and instruction count. The instruction count is read from the xz index: uncompressed bytes / 64, the size of ChampSim's `input_instr` record.
5. Deletes the zip.

**Fetched 2026-10-25 on coe-hpc1:**
- The zip downloaded in 37 min, at 13–27 MB/s. It holds 207 members, 203 of them traces; the full list is in `configs/traces/crc2-folder-index.txt`.
- The 19 traces take 5.39 GB, from 23 MB (`calculix_2670B`) to 795 MB (`mcf_46B`).
- Each holds exactly 1B instructions, so every length below fits in every trace.
- `python -m evaluator.manifest --check configs/experiments/crc2-tiny-v1.json --verify-traces` confirms every sha256.

**One difference from ArchAgent:** we keep ChampSim's default machine model (`configs/champsim/baseline.json`) rather than the CRC-2 configuration. So we use the same traces, but absolute speedups are not comparable to ArchAgent's. A CRC-2-like configuration is a possible later lever.

## 2. Method

**Three lengths, each its own manifest** (`python -m evaluator.calibrate --plan` writes them):

| Manifest | Warmup + simulation | Role |
|---|---|---|
| `crc2-cal-L1-v1` | 10M + 10M | candidate evolution length |
| `crc2-cal-L2-v1` | 20M + 50M | candidate evolution length |
| `crc2-cal-ref-v1` | 50M + 200M | reference, and the validation length |

`--plan` also writes `crc2-tiny-v1`, with all 19 traces at 10K + 10K instructions. It's a format check: every trace must load in ChampSim and give a valid `completed` record, in seconds, before any array runs.

**Format check (2026-10-25, coe-hpc1, gcc 12.2):** `evaluate_candidate --baseline` on `crc2-tiny-v1` gave a valid `completed` record for all 19 traces, in 19 s with 2 jobs. So the 2017 CRC-2 traces load in ChampSim `410cee6` as they are.

ArchAgent gave evolution feedback from short runs (20M/50M, or up to 100M within an hour) and validated at 1B. At about 55K instructions/s on HPC3's nodes, our 50M + 200M reference already takes over an hour per run.

**Run cost depends on IPC, not just instruction count.** Two measurements of the baseline binary at 1M + 2M instructions, on coe-hpc1's Xeon E5-2660 v4 (the same CPU as HPC3's nodes):

| Trace | IPC | Speed | Peak RSS |
|---|---|---|---|
| `mcf_46B` | 0.129 | 26K instructions/s (114 s) | 117 MB |
| `calculix_2670B` | 2.94 | 217K instructions/s (14 s) | 116 MB |

Together these fit about 3.1 µs per instruction plus 4.5 µs per simulated cycle. That predicts M1's 55K instructions/s on `602.gcc_s` at IPC 0.27.

**Consequences for the calibration runs:**
- The slowest trace, mcf, needs about 13 min at L1, 46 min at L2 and 2.7 h at the reference length.
- `run_timeout_s` (2,400 / 7,200 / 25,200 s) and the arrays' `--time` (1:00 / 2:30 / 7:30) allow about 2.5× that.
- At the 10K-instruction IPCs (mean CPI about 2.4), the three arrays cost about 6 + 21 + 74 ≈ 100 CPU-hours.
- Each task asks for 1 CPU and 2 GB.

**Runs:**
- Each manifest runs as one Slurm array, `slurm/calibrate_array.sbatch`, with one task per (policy, trace) pair. That makes 4 × 19 = 76 tasks per length, each on 1 CPU.
- The policies are ChampSim's built-in LRU, SRRIP, DRRIP and SHiP, evaluated unscored (`evaluate_candidate --unscored --trace`).
- Records go to `$AFAR_RESULTS/calibration/<manifest>/builtin-<policy>/<trace>.json`.
- Every record carries the full manifest's sha256, the compiler and the ChampSim commit. The analysis refuses to mix toolchains.

## 3. Rules, fixed before any results

`python -m evaluator.calibrate --analyze` applies these rules. Its self-test checks them on synthetic data.

| Question | Rule |
|---|---|
| Is a trace admitted? | At the reference length, LRU's demand MPKI (LLC LOAD + RFO misses per 1,000 instructions) is **above 1**, **and** the four policies' IPCs spread by **at least 1% of LRU's IPC**. A trace that ends before the requested instruction count is excluded |
| Which evolution length? | The shortest of L1 and L2 that meets both conditions below, computed over the admitted traces. If neither does, L2, flagged |
| | 1. Its suite geomean speedups rank SRRIP, DRRIP and SHiP in the same order as the reference |
| | 2. Each of those speedups is within **0.5 percentage points** of its reference value |
| Issue 11 (12,000-cycle steps) | Per trace and length, the greatest common divisor of the cycle differences between the four policies, and whether DRRIP's cycle count equals LRU's. If most traces show a common step over 1,000 cycles at the reference length, one time-boxed follow-up job looks for the cause. Otherwise the step is a property of the smoke trace, and the issue closes |

**Outputs:** `calibrate --analyze --write` writes two manifests, which are immutable once written. Their LRU baselines then run as one more array (`TARGETS=baseline`):
- `crc2-evolve-v1`: the admitted traces at the chosen length
- `crc2-validate-v1`: the same traces at 50M + 200M

## 4. Results

*Pending: the calibration arrays have not run yet.*
