# Trace calibration (M3)

*Done 2026-10-25.*

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

## 4. Results (2026-10-25)

**The runs:** three arrays on HPC3's CPU nodes, 228 runs in total.
- **Every run `completed`**, and every record validates.
- **One toolchain throughout:** gcc 11.5, ChampSim `410cee6`, project commit `205386c` (clean).
- **Nodes:** tasks landed on `c1`–`c20` and `homacs1`–`4`. ChampSim builds without `-march`, so its binaries are portable.

| Array | Job | Wall time | Slowest run |
|---|---|---|---|
| L1 | 95102 | 15 min | `mcf_46B`, 860 s (predicted about 780 s) |
| L2 | 95199 | 62 min | `mcf_46B`, 62 min (predicted 46 min) |
| ref | 95240 | 3 h 53 min | `mcf_46B`, about 3.7 h |

### Admission: 14 of 19 traces

Speedups are each policy's IPC over LRU's, at 50M + 200M.

| Trace | LRU IPC | LRU demand MPKI | LRU hit rate | SRRIP | DRRIP | SHiP | Spread | Admitted |
|---|---|---|---|---|---|---|---|---|
| GemsFDTD_109B | 0.2873 | 37.60 | 0.019 | -0.90% | -0.77% | -1.07% | 1.07% | yes |
| astar_163B | 0.9088 | 1.36 | 0.316 | -2.08% | +2.19% | +2.12% | 4.27% | yes |
| bwaves_1861B | 0.5954 | 22.85 | 0.000 | -0.11% | +0.21% | +0.45% | 0.56% | no: spread |
| bzip2_183B | 1.3457 | 1.86 | 0.659 | -3.49% | +0.43% | +1.75% | 5.24% | yes |
| cactusADM_734B | 1.0347 | 8.80 | 0.002 | +11.86% | +8.75% | +1.01% | 11.86% | yes |
| calculix_2670B | 3.1739 | 0.01 | 0.000 | +0.00% | +0.00% | +0.00% | 0.00% | no: MPKI |
| gcc_13B | 0.2721 | 20.33 | 0.037 | +7.01% | +6.45% | +8.14% | 8.14% | yes |
| lbm_94B | 0.5920 | 29.59 | 0.000 | +0.07% | -0.36% | -0.39% | 0.46% | no: spread |
| leslie3d_1116B | 0.4311 | 26.86 | 0.084 | -1.25% | -0.23% | -0.22% | 1.25% | yes |
| libquantum_1210B | 0.2621 | 28.70 | 0.000 | +9.91% | -0.23% | -0.38% | 10.29% | yes |
| mcf_46B | 0.1119 | 71.57 | 0.163 | +26.33% | +8.07% | +11.79% | 26.33% | yes |
| milc_360B | 0.4459 | 24.44 | 0.000 | +1.73% | -0.13% | +1.35% | 1.86% | yes |
| omnetpp_340B | 0.3303 | 17.15 | 0.210 | -5.59% | +0.88% | +1.92% | 7.51% | yes |
| soplex_66B | 0.2816 | 26.90 | 0.225 | -0.28% | +11.85% | +11.80% | 12.13% | yes |
| sphinx3_2520B | 0.5521 | 10.49 | 0.186 | +47.74% | +56.28% | +63.12% | 63.12% | yes |
| tonto_2834B | 1.6779 | 0.20 | 0.517 | +0.03% | -0.01% | +0.04% | 0.05% | no: MPKI |
| wrf_1212B | 1.2827 | 1.49 | 0.001 | -0.01% | +0.01% | -0.06% | 0.07% | no: spread |
| xalancbmk_748B | 1.1717 | 2.72 | 0.149 | -2.44% | -0.16% | -1.30% | 2.44% | yes |
| zeusmp_600B | 1.4165 | 2.62 | 0.213 | -4.88% | -2.15% | -3.18% | 4.88% | yes |

**Why the 5 were excluded:**
- **calculix and tonto fit in the cache:** demand MPKI of 0.01 and 0.20.
- **bwaves, lbm and wrf stream:** no policy moves them by 1%.

**The admitted 14 rank the built-ins as the literature does:** SHiP > DRRIP > SRRIP > LRU, with suite geomeans of +5.95%, +5.73% and +5.15%.

### Evolution length: L2, flagged

| Length | SRRIP | DRRIP | SHiP | Same order as ref | Worst error | Best policy agrees | One candidate, 14 traces |
|---|---|---|---|---|---|---|---|
| L1 (10M + 10M) | +7.64% | +5.65% | +5.31% | no | 2.49 pp | 5/14 | 1.2 CPU-h; 14 min on 14 CPUs |
| L2 (20M + 50M) | +3.64% | +4.32% | +5.23% | yes | 1.51 pp | 7/14 | 5.1 CPU-h; 62 min |
| ref (50M + 200M) | +5.15% | +5.73% | +5.95% | reference | | | 19.2 CPU-h; 3.7 h |

Neither short length met the 0.5 pp rule, so the rule's fallback applies: L2, flagged. Accepted on 2026-10-25 over testing a 50M + 100M length first (about 45 CPU-hours more).

**How L2 compares with the reference:**
- **What it gets right:** it ranks the three policies correctly.
- **Where it falls short:** it underestimates every suite speedup, by 0.7–1.5 pp.
- **Per trace, its agreement with the reference is moderate:** across the 42 (policy, trace) speedups, Pearson correlation is 0.97 (dominated by sphinx3's large values) and Spearman rank correlation is 0.75.
- **The largest disagreements:**
  - SRRIP on sphinx3: +32.4% at L2, +47.7% at the reference.
  - SRRIP on zeusmp: +2.4% at L2, −4.9% at the reference.
  - DRRIP on gcc: −0.5% at L2, +6.4% at the reference.

**Part of the gap is the program phase, not just the length.** L2 measures instructions 20M–70M of each SimPoint, while the reference measures 50M–250M.

**What this means for M4/M5:**
- L2 is a screening score, as in ArchAgent (short runs for feedback, validation at 1B).
- Candidates that look best at L2 must be re-scored on `crc2-validate-v1` before any claim.
- Expect some candidates that are good at the reference length to be under-scored during evolution.

### Issue 11: closed

- **Admitted traces:** the GCD of the cycle differences between policies is 1 on all 14 at the reference length, and at most 15 at L1 and L2. Cycle counts resolve to single cycles, and DRRIP differs from LRU on every trace except calculix, where all four policies tie.
- **The 12,000-cycle quantum appears only on near-flat traces.** On `tonto_2834B` at the reference length, the differences from LRU are −36,000 (SRRIP), +12,000 (DRRIP) and −48,000 (SHiP). That's the same quantum as on the smoke trace, and both traces are excluded.
- **Rule outcome:** only 1 of 19 traces shows a step over 1,000 cycles at the reference length, so issue 11 closes without the follow-up job. Its cause is still unknown. It's worth revisiting only if evolved policies make near-flat traces matter.

### Outputs

**Two manifests:**
- `configs/experiments/crc2-evolve-v1.json`: the 14 admitted traces at 20M + 50M, with `run_timeout_s` 7,200.
- `configs/experiments/crc2-validate-v1.json`: the same 14 traces at 50M + 200M, with `run_timeout_s` 25,200.

**The analysis** is in `$AFAR_RESULTS/calibration/crc2-v1-analysis.json`.

**A development manifest (M4):** `crc2-dev-v1` holds 4 admitted traces at 10M + 10M: sphinx3, cactusADM, soplex and omnetpp. `python -m evaluator.calibrate --dev` writes it. These traces were picked because:
- they have the largest L1 spreads between the built-ins: 58%, 16%, 21% and 8%
- SHiP, SRRIP and DRRIP are each best on at least one of them
- every run takes at most 6 min, so a candidate takes about 6 min on 4 CPUs

It's for testing the evolution pipeline, not for claims. `--check-baselines` covers its baselines too.

**Baselines:** the LRU baselines of both manifests ran as arrays (`TARGETS=baseline`) at commit `fa9b49c`:

| Manifest | Job | Wall time | Result |
|---|---|---|---|
| `crc2-evolve-v1` | 95426 | 53 min | 14/14 reproduce the L2 calibration's LRU cycles exactly |
| `crc2-validate-v1` | 95440 | 4 h 12 min | 14/14 reproduce the reference calibration's LRU cycles exactly |

`python -m evaluator.calibrate --check-baselines` compared each baseline with its calibration twin, the same simulation run independently.
- **27 of the 28 pairs ran on different nodes,** 17 nodes in all, including `homacs`. So results don't depend on the node, and candidates can be scored on any CPU node against these baselines.
- **Run time does depend on the node.** mcf's 50M + 200M run took 15,043 s on `c1`, against 13,112 s on `c17`, with identical cycles. Across the calibration's four mcf runs the range was 2.6–4.2 h, so `--time` and `run_timeout_s` keep their margins.
