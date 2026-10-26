# Project status

As of 2026-10-25. What works, how it was verified, what was measured, and what's next. Update this page at the end of each milestone. The plan going forward is in `docs/roadmap.md`.

## Summary

- **Phase 1 is complete.** A fresh clone of the GitHub repo builds on coe-hpc1 with one `make setup`, and runs on COE-HPC3 with no modules. A GPU job serves a pinned 7B model with vLLM next to OpenEvolve, and OpenEvolve evolves the real target: a ChampSim LLC replacement policy in one C++ header.
- **Candidates can now be scored for real (M2).** `evaluator/evaluate_candidate.py` (the `champsim_ipc` policy) builds a candidate into ChampSim on an HPC3 node, runs the manifest's traces, and scores IPC against LRU. The seed scores exactly 1.0. OpenEvolve runs still use the compile-only policy until M4 switches them over.
- **A research trace set is calibrated (M3).** 14 of the 19 CRC-2 traces ArchAgent used separate the built-in policies. On them, the built-ins rank as in the literature (SHiP +5.95%, DRRIP +5.73%, SRRIP +5.15% over LRU at 50M + 200M). Evolution will score at 20M + 50M (`crc2-evolve-v1`) and confirm at 50M + 200M (`crc2-validate-v1`).
- In BUILD_PLAN's terms, phases 0–3 are done, and phase 4 is started (table below). Roadmap milestones M0–M3 are done. M4 is in progress: part A, the modular run specs, is done (job 95745); part B (real fitness) is next.
- **Runs are assembled from swappable parts (M4 part A).** A run spec, `configs/runs/<name>.yaml`, names the seed, model profile, prompt profile, search profile and evaluator policy, and one generic job, `slurm/openevolve.sbatch`, runs any spec (`docs/run-specs.md`).

## Where each BUILD_PLAN phase stands

| BUILD_PLAN phase | Status | Evidence |
|---|---|---|
| 0. Experiment contract | **Agreed; partly implemented** | `docs/experiment-contract.md`. Implemented: the candidate format, the static checks and the failure statuses for compile-only checks |
| 1. Reproducible environment | **Done** | Portable Python and venvs (`.python/`, `env/*-requirements.txt`), records in `env/versions.txt`; `make setup` from a fresh GitHub clone passed (step G) |
| 2. ChampSim baseline | **Done (M1)** | Pinned `410cee6`, built with static libstdc++. `slurm/baseline.sbatch` ran LRU on HPC3 (job 94177) and wrote validated §3 records to `$AFAR_RESULTS/baselines/`, reproducing coe-hpc1's 43,703 cycles exactly |
| 3. Standalone evaluator | **Done (M2)** | `evaluator/evaluate_candidate.py`: static checks, a build from the prebuilt base tree, runs under `prlimit`, ROI metrics, the contract's statuses and `geomean_ipc_speedup@1`. Self-test 10/10 on HPC3 (job 94248) |
| 4. Persistent-controller prototype | **Started** | One GPU job runs vLLM plus OpenEvolve with checkpoints and resume (R6), on the C++ target (R7), with compile-only fitness. **Missing:** OpenEvolve with `champsim_ipc` (M4) |
| 5–10. CPU arrays, staged generations, campaign state, hardening, observability, islands | Not started | |

The repo's own "Phase 1" was a ladder of steps (E1, E2, R2–R7, G) that got OpenEvolve working with a GPU-served LLM on this HPC. It covers BUILD_PLAN phases 1–2 and the early parts of 3–4.

## What works now

| Capability | Command | Docs |
|---|---|---|
| Build everything on coe-hpc1 | `make setup` (392 s from a fresh clone with no caches) | `README.md` |
| Check an environment on HPC3 | `sbatch slurm/check.sbatch` | `README.md` |
| LRU baselines for a manifest, as validated result records | `sbatch slurm/baseline.sbatch` (or `make baseline-local EXP=smoke-tiny-v1`) | `docs/experiment-contract.md` §2–§3 |
| Fetch and pin LLM assets | `make llm-fetch`, then `sbatch slurm/build_sif.sbatch` | `docs/llm-serving.md` |
| Serve the model inside a GPU job | `slurm/llm_serve.sh` (GPU preflight, random port and key, health wait, clean stop); tested by `slurm/llm_ping.sbatch` | `docs/llm-serving.md` |
| Any OpenEvolve run, from a run spec, with resume | `make oe-check SPEC=<name>`, then `sbatch --export=ALL,SPEC=<name> slurm/openevolve.sbatch` (`RESUME=runs/<run>` to continue) | `docs/run-specs.md` |
| OpenEvolve on a toy / on the ChampSim policy, compile-only | `SPEC=toy-v1` (R6) / `SPEC=champsim-compile-v1` (R7); the old `slurm/openevolve_*.sbatch` are wrappers until M5 | `docs/llm-serving.md`, `docs/champsim-evaluator.md` |
| Evaluate a candidate for real: baseline, candidate file or built-in policy | `sbatch slurm/evaluate.sbatch` (or `python -m evaluator.evaluate_candidate`) | `docs/champsim-evaluator.md` (M2 part B) |
| Test job scripts without a GPU | `AFAR_LLM_FAKE=1` or the `fake-v1` model profile (plus `AFAR_LLM_FAKE_REPLY=<file>` to replay a real reply) | `docs/llm-serving.md` |
| Summarize a run | `make oe-summary RUN=runs/<name>-<jobid>` | |

## Verified on HPC3

| Step | Job(s) | Result |
|---|---|---|
| E2: environment on a CPU node and a GPU node | 90581 (`c8`), 90582 (`cs002`) | All run-time checks pass with no modules |
| R4: container image built on HPC3 | 91230 (`cpuqs`) | SIF built in 32.6 min; sha256 recorded |
| R5: vLLM server on an A100 | 93353 (failed: faulty GPU on `cs002`), 93355 (`cs003`) | Healthy in 115 s; OpenEvolve's client reached it |
| R6: real model on the toy, then resume | 93362 (diff mode: 0/10 applied), 93369, 93386 | Full rewrites applied 9/10; resume continued at iteration 11 |
| R7: real model on the ChampSim policy | 93458 (`cs004`) | 6 of 9 rewrites compiled; markers kept 9/9 |
| G: fresh clone of GitHub `main` | setup on coe-hpc1; 93489 (`c19`), 93495 (`cs001`) | Everything reproduced, including smoke at 43,703 cycles; 5 of 10 rewrites compiled |
| M4 part B: a time-limit stop, then a resume | 95821 (`--time=00:15:00`, 500 iterations asked), 95822 (`cs004`) | 95821 got SIGTERM at 14 min with 3 evaluations in flight. OpenEvolve cancelled them, saved `best/` and stopped within 1 s, and the server stopped. 95822 started 5 s later, resumed from `checkpoint_144` (the last completed iteration), ran iterations 145–154 and finished cleanly; `run.json` lists all 5 jobs. The stop skipped the key-leak check and the summary, and could leave scratch on the node, so the job script now runs both on every exit and uses a job-scoped `TMPDIR` that is always deleted (tested locally with a mid-run SIGTERM) |
| M4 part B, Gate B: `m4-dev-v1` (`champsim_ipc` on `crc2-dev-v1`), one run across 3 jobs | 95767, then resumes 95768 and 95769 (`cs001`; about 18–27 min each) | 120 iterations; 102 evaluations: 51 completed with **33 distinct fitness values** (best 1.1881; seed exactly 1.0); 23 `compile_failed`, 21 `invalid_candidate`, 7 `runtime_failed`. Each resume continued from the newest checkpoint (40, 80), and 4 candidates were reused from the cache across jobs. Every job finished its 40 iterations, so none was stopped at its time limit |
| M4 part A: R7 as a run spec through the generic job | 95745 (`cs001`) | 10/10 rewrites applied, 6 compiled, 0 edits outside the block (R7, job 93458: 6 of 9); 14.2 s per iteration (13.4 LLM + 0.8 compile). All 10 stored prompts carry the profile's system message exactly; `run.json` and `evaluations.jsonl` complete; key-leak check clean |
| M3: LRU baselines of `crc2-evolve-v1` and `crc2-validate-v1` | 95426 (53 min), 95440 (4 h 12 min) | 28/28 reproduce the calibration's LRU cycles exactly; 27 of the 28 pairs ran on different nodes (17 nodes) |
| M3: trace calibration, 228 runs | 95102 (L1, 15 min), 95199 (L2, 62 min), 95240 (ref, 3 h 53 min); 15 nodes incl. `homacs` | All `completed`, one toolchain, commit `205386c`. 14 of 19 traces admitted; L2 chosen (flagged); issue 11 closed (`docs/trace-calibration.md`) |
| M2 part B: real evaluator | 94248 (`c11`) | Self-test 10/10 with gcc 11.5. On `smoke-v1`: seed 1.0 exactly, SRRIP 0.99936, SHiP 1.00129, DRRIP 1.0 (see issue 11) |
| M2 part A: toolchain test | 94191 (`c18`) | gcc 11.5 builds and links; LRU, seed and SRRIP give 43,703 cycles; the gcc 11.5 build gives `smoke-v1`'s 18,624,235 cycles, the same as gcc 12 |
| M1: LRU baselines in Slurm | 94177 (`c11`) | `smoke-tiny-v1`: 43,703 cycles, the same as coe-hpc1. `smoke-v1`: 18,624,235 cycles, IPC 0.2685, LLC demand MPKI 17.80, hit rate 0.11%. Both records validate |

## Measurements

| | Value | Source |
|---|---|---|
| `make setup`, fresh clone, no caches | 392 s; 795 MB on `/home` | step G |
| LLM assets on `/data` | 14 GB model + 7.1 GB SIF (21 GB unpacked while the SIF is built) | R4 |
| Server start to healthy | 75–120 s (longer on a node's first use) | R5–G |
| GPU memory / KV cache | 35.8 of 40 GB; 346,928 tokens, about 42 concurrent 8K requests | R5 |
| Generation | about 80 tokens/s for one request; about 12 s per iteration for 800–1,500 tokens | R6, R7 |
| Compile check | 0.7 s on a GPU node (gcc 11.5), 1.2 s on coe-hpc1 (gcc 12.2) | R7 |
| Host memory (`sacct` MaxRSS) | 3.1–4.2 GB; jobs request 24G | R5–R7 |
| Largest prompt | 7,204 tokens (toy), 4,003 (C++) | R6, G |
| ChampSim speed, baseline binary | **about 55,000 instructions/s** on `c11` and `c18` (Xeon E5-2660 v4, 2.0 GHz): 6M instructions in 106–109 s. A local trace copy is no faster, so the CPU is the limit, not NFS. At this speed, 50M warmup + 200M simulated instructions would take about 75 min per trace, so simulation length and trace count are the main cost levers for M3–M5 | M1, M2 part A |
| ChampSim cost per run, by IPC | about 3.1 µs per instruction plus 4.5 µs per simulated cycle (E5-2660 v4): `mcf_46B` (IPC 0.13) 26K instructions/s, `calculix_2670B` (IPC 2.9) 217K/s. Peak RSS about 117 MB | M3 (`docs/trace-calibration.md`) |
| Real-fitness evolution, one GPU job (`m4-dev-v1`, 3 candidates × 4 CPUs) | Per applied iteration: LLM 13.2 s, evaluation 95.8 s (mean, including fast failures); a completed candidate takes a median 218 s on `cs001`. Wall time 34 s per iteration. Half of the applied rewrites fail; the success rate rose from 9 of 37 in the first job to 21 of 32 in the third, as the population filled with working programs | M4 part B |
| Node-to-node speed | The same mcf 50M + 200M run took 2.6–4.2 h depending on the node (`homacs3` fastest, `c1` slowest), with identical cycles | M3 |
| One candidate on `crc2-evolve-v1` (14 traces) | 5.1 CPU-hours; 62 min on 14 CPUs, bound by `mcf_46B`. On `crc2-validate-v1`: 19.2 CPU-hours, 3.7 h | M3 |
| CRC-2 traces on `/data` | 19 traces, 5.39 GB, 1B instructions each; the folder zip (28 GB) took 37 min and was deleted | M3 |
| Candidate build on an HPC3 node (gcc 11.5, `-j4`) | full build 53.3 s; after copying a built tree, 10.5 s (2 objects recompiled) | M2 part A |

## Decisions and why

| Decision | Reason | Where |
|---|---|---|
| Build on coe-hpc1; a portable CPython; ChampSim linked statically | HPC3 is offline and has a different module tree; anything built for glibc 2.17 also runs on 2.34 | `README.md` |
| No modules at run time; paths come from `env/site.sh` | Module names on coe-hpc1 don't exist on HPC3 | `env/modules.sh`, `env/site.sh` |
| vLLM as a sidecar in the same GPU job, from a digest-pinned SIF | Self-contained jobs, with no long-running service to manage | `docs/llm-serving.md` |
| Qwen2.5-Coder-7B-Instruct, bf16, 16K context | Fits a 40 GB A100 with room for the KV cache; permissive license | `configs/models/qwen2.5-coder-7b-v1.env` |
| Runs assembled from named, versioned parts (run specs) | The model, prompt, search settings and evaluator policy must be swappable without editing job scripts, and every run must record exactly which parts it used. Settings that depend on other parts (evaluator timeout, code-length cap, CPU budget) are derived, never hand-kept | `docs/run-specs.md` |
| Full rewrites, not diffs | The 7B model ignored the SEARCH/REPLACE format (0/10 applied) | R6 |
| No top-level LLM seed | vLLM honours the seed, so identical prompts got identical replies | R6 |
| The evaluator enforces the EVOLVE-BLOCK itself | OpenEvolve 0.4.0's `enforce_evolve_blocks` never reaches its workers | `docs/experiment-contract.md` |
| A compile check that `static_assert`s ChampSim's hook detection | A hook with the wrong signature otherwise compiles and is silently never called | `docs/champsim-evaluator.md` |
| ArchAgent's 19 CRC-2 traces, calibrated under pre-set rules | Comparable with published work; the rules were fixed before any result, so they couldn't be tuned to it | `docs/trace-calibration.md` |
| Evolve at 20M + 50M, confirm at 50M + 200M | No short length met the ±0.5 pp rule. L2 ranks the policies correctly at a quarter of the reference's cost, so it's the rule's flagged fallback (chosen 2026-10-25 over testing 50M + 100M) | `docs/trace-calibration.md` §4 |
| `--mem=24G` for the GPU jobs | Measured peak is 4.2 GB; 64G left jobs waiting for memory while GPUs were free | `docs/llm-serving.md` |

## Open issues

| # | Issue | Effect | Proposed fix | State |
|---|---|---|---|---|
| 1 | The seed's header comment ("port of replacement/lru") invites edits outside the block | In job 93495, all 5 `invalid_candidate` results were only this comment | Made policy-neutral: the LRU note now sits inside the block | **Fixed in M1** |
| 2 | The compile-only stub scores every compiling candidate 1.0 | No fitness signal; OpenEvolve keeps the seed as "best" on ties | `champsim_ipc` (M2) gives real fitness; OpenEvolve runs switch to it in M4 (`evaluator/openevolve_adapter.py`) | **Fixed in M2**; used from M4 |
| 3 | `-fsyntax-only` doesn't link | A member declared but never defined passes | `champsim_ipc` builds and links every candidate; the self-test's link-error case gives `compile_failed` | **Fixed in M2** |
| 4 | Compiler feedback rarely reaches the model | OpenEvolve shows artifacts only to a program's children, and failures score 0 | Accept, or a custom prompt template later | Known |
| 5 | Which compiler builds candidates on HPC3? | coe-hpc1 built ChampSim and its vcpkg libraries with gcc 12.2; HPC3 nodes have only gcc 11.5 | **Decided 2026-10-23:** the nodes' system gcc 11.5, with no sandbox. **Tested (job 94191):** it builds and links against the gcc-12 libraries, with dynamic libstdc++ | **Resolved** |
| 6 | The only trace is a smoke trace with flat fitness | LRU, SRRIP, DRRIP and SHiP land within 0.2% IPC on it | Calibrated 14 LLC-sensitive CRC-2 traces, with spreads of 1.1–63% between the built-ins (`crc2-evolve-v1`, `crc2-validate-v1`) | **Fixed in M3** |
| 7 | `env/check.sh` requires `AFAR_ASSETS` to be writable | Teammates sharing a read-only copy fail `make check` | `check.sh` accepts a readable copy; `make check` passes `AFAR_ASSETS`/`AFAR_RESULTS` through its clean environment; `llm-fetch` refuses an unwritable one with a clear message | **Fixed in M1** |
| 8 | pip's cache isn't redirected | `~/.cache/pip` is 3 GB on the 99%-full `/home` (mostly older than this repo) | The Makefile runs pip with `--no-cache-dir`. The existing 3 GB is the user's to delete | **Fixed in M1** |
| 9 | `cs002` GPU 0 is faulty (73 uncorrectable ECC errors) | Jobs exclude `cs002` and lose its 3 healthy GPUs | Report to HPC support (a note is drafted) | The user's call |
| 10 | Two near-identical OpenEvolve job scripts | Changes have to be made twice | One generic job, `slurm/openevolve.sbatch SPEC=`; the two old scripts are wrappers until M5 | **Fixed in M4 part A** |
| 11 | Cycle counts on `smoke-v1` move in 12,000-cycle steps; DRRIP's LLC behaviour differs from LRU's, yet its cycle count is identical (job 94248) | Fitness resolution of about 0.064% on this trace, so small policy differences can read as exactly 1.0 | Check in M3 on LLC-sensitive traces and longer runs: does the step persist, and does DRRIP then differ? If it does, find its cause before trusting small fitness differences. Ruled out so far: warmup (361,901 cycles for every policy) and DRAM refresh (tREF = 32 ms / 8192 = 15,625 core cycles at 4 GHz, not 12,000) | **Closed in M3:** on all 14 admitted traces, policies' cycle counts differ at 1-cycle resolution and DRRIP differs from LRU. The quantum reappears only on near-flat `tonto_2834B` (−36,000 / +12,000 / −48,000), which is excluded. Its cause is still unknown |

## Next steps

The plan to an autonomous, multi-node research system is in **`docs/roadmap.md`**: its architecture, research levers, failure handling and milestones M0–M7.

| Milestone | State |
|---|---|
| **M0** HPC3 capability check | **Done (2026-10-23).** Jobs can submit jobs; `scrontab` is disabled, so the watchdog uses `afternotok` handlers plus a `--begin` heartbeat; `MaxArraySize` is 1001; `JobRequeue=1`; `/bin/mail` is available |
| **M1** Baseline in Slurm, result schema, manifest v1, and issues 1, 7 and 8 | **Done (2026-10-23).** Job 94177 reproduced 43,703 cycles on HPC3 and wrote validated `smoke-v1` baselines. Issues 1, 7 and 8 are fixed |
| **M2** Real evaluator (resolves issues 2, 3 and 5) | **Done (2026-10-23).** Part A: job 94191. Part B (job 94248): the `champsim_ipc` evaluator with a prebuilt base tree. The self-test passes 10/10 on HPC3; on `smoke-v1` the seed's fitness is exactly 1.0, SRRIP 0.99936, SHiP 1.00129, and DRRIP 1.0 (its LLC behaviour differs, but its cycle count is identical). Cycle counts move in 12,000-cycle steps on this trace; that's to check in M3 |
| **M3** Trace calibration (issue 6) | **Done (2026-10-25).** The 19 CRC-2 traces ArchAgent used were run at 10M + 10M, 20M + 50M and 50M + 200M with the four built-in policies: 228 runs, as the project's first Slurm arrays (jobs 95102, 95199, 95240).
- **Admitted:** 14 traces.
- **Evolution length:** L2 (20M + 50M), the flagged fallback: right ranking, but up to 1.5 pp low.
- **Issues:** 6 fixed, 11 closed.
- **Baselines:** jobs 95426 and 95440 ran the LRU baselines of `crc2-evolve-v1` and `crc2-validate-v1`. All 28 reproduce the calibration's cycles exactly, across 17 nodes, so results don't depend on the node |
| M4 Single-job prototype with real fitness | **In progress.**
- **Part A, modular run specs: done (2026-10-25).** On coe-hpc1: the resolver's 21 self-test cases; equivalence with R6/R7's configs, field by field; fake-LLM runs of both specs, a resume and a prompt swap; the policy contract suite. On HPC3, job 95745 ran R7 through the generic job with the same behaviour.
- **Part B, real fitness: in progress.**
  - Done: the dev manifest `crc2-dev-v1` and the spec `m4-dev-v1` (`champsim_ipc`, 3 candidates × 4 CPUs). `champsim_ipc` ran through the generic job locally (fake LLM, `crc2-tiny-v1`).
  - Gate B passed (jobs 95767–95769): fitness varies (33 distinct values), a run resumes across jobs, the cache works across them, and the LLM/evaluation split is measured. `crc2-dev-v1`'s baselines (job 95760) reproduce the calibration's cycles.
  - A job stopped at its time limit with evaluations in flight resumes cleanly (jobs 95821, 95822).
  - Left: optionally, a first real run on `crc2-evolve-v1` (about 1 h per candidate on 14 CPUs), with its best candidate scored on `crc2-validate-v1` |
| M5 Staged generations (issue 10) · M6 Long-run hardening · M7 Comparing levers | Planned |
