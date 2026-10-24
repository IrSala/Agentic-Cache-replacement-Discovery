# Project status

As of 2026-10-23. What works, how it was verified, what was measured, and what's next. Update this page at the end of each milestone. The plan going forward is in `docs/roadmap.md`.

## Summary

- **Phase 1 is complete.** A fresh clone of the GitHub repo builds on coe-hpc1 with one `make setup`, and runs on COE-HPC3 with no modules. A GPU job serves a pinned 7B model with vLLM next to OpenEvolve, and OpenEvolve evolves the real target: a ChampSim LLC replacement policy in one C++ header.
- **Candidates are only compiled so far, not simulated.** `evaluator/compile_check.py` is a compile-only stub: every candidate that compiles scores 1.0, so evolution has no fitness signal yet. Building and running ChampSim per candidate is the next phase.
- In BUILD_PLAN's terms, phases 0–2 are done, and phases 3–4 are started (table below). Roadmap milestones M0 and M1 are done.

## Where each BUILD_PLAN phase stands

| BUILD_PLAN phase | Status | Evidence |
|---|---|---|
| 0. Experiment contract | **Agreed; partly implemented** | `docs/experiment-contract.md`. Implemented: the candidate format, the static checks and the failure statuses for compile-only checks |
| 1. Reproducible environment | **Done** | Portable Python and venvs (`.python/`, `env/*-requirements.txt`), records in `env/versions.txt`; `make setup` from a fresh GitHub clone passed (step G) |
| 2. ChampSim baseline | **Done (M1)** | Pinned `410cee6`, built with static libstdc++. `slurm/baseline.sbatch` ran LRU on HPC3 (job 94177) and wrote validated §3 records to `$AFAR_RESULTS/baselines/`, reproducing coe-hpc1's 43,703 cycles exactly |
| 3. Standalone evaluator | **Started** | The compile-only stub, with statuses `completed`/`invalid_candidate`/`compile_failed`/`timeout` and a self-test. **Missing:** a per-candidate build, the ChampSim run, metric parsing and result JSON |
| 4. Persistent-controller prototype | **Started** | One GPU job runs vLLM plus OpenEvolve with checkpoints and resume (R6), on the C++ target (R7). **Missing:** real fitness from short ChampSim runs |
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
| OpenEvolve on a toy, with resume | `slurm/openevolve_toy.sbatch` | `docs/llm-serving.md` (R6) |
| OpenEvolve on the ChampSim policy, compile-only | `slurm/openevolve_champsim_compile.sbatch` | `docs/champsim-evaluator.md` |
| Test job scripts without a GPU | `AFAR_LLM_FAKE=1` (plus `AFAR_LLM_FAKE_REPLY=<file>` to replay a real reply) | `docs/llm-serving.md` |
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
| ChampSim speed, baseline binary | **about 56,000 instructions/s** on `c11` (6M instructions in 106.5 s, trace read from `/data`). At this speed, 50M warmup + 200M simulated instructions would take about 75 min per trace. Simulation length and trace count are the main cost levers for M3–M5; whether NFS reads slow it down is to be checked in M2 | M1 |

## Decisions and why

| Decision | Reason | Where |
|---|---|---|
| Build on coe-hpc1; a portable CPython; ChampSim linked statically | HPC3 is offline and has a different module tree; anything built for glibc 2.17 also runs on 2.34 | `README.md` |
| No modules at run time; paths come from `env/site.sh` | Module names on coe-hpc1 don't exist on HPC3 | `env/modules.sh`, `env/site.sh` |
| vLLM as a sidecar in the same GPU job, from a digest-pinned SIF | Self-contained jobs, with no long-running service to manage | `docs/llm-serving.md` |
| Qwen2.5-Coder-7B-Instruct, bf16, 16K context | Fits a 40 GB A100 with room for the KV cache; permissive license | `configs/llm/serve.env` |
| Full rewrites, not diffs | The 7B model ignored the SEARCH/REPLACE format (0/10 applied) | R6 |
| No top-level LLM seed | vLLM honours the seed, so identical prompts got identical replies | R6 |
| The evaluator enforces the EVOLVE-BLOCK itself | OpenEvolve 0.4.0's `enforce_evolve_blocks` never reaches its workers | `docs/experiment-contract.md` |
| A compile check that `static_assert`s ChampSim's hook detection | A hook with the wrong signature otherwise compiles and is silently never called | `docs/champsim-evaluator.md` |
| `--mem=24G` for the GPU jobs | Measured peak is 4.2 GB; 64G left jobs waiting for memory while GPUs were free | `docs/llm-serving.md` |

## Open issues

| # | Issue | Effect | Proposed fix | State |
|---|---|---|---|---|
| 1 | The seed's header comment ("port of replacement/lru") invites edits outside the block | In job 93495, all 5 `invalid_candidate` results were only this comment | Made policy-neutral: the LRU note now sits inside the block | **Fixed in M1** |
| 2 | The compile-only stub scores every compiling candidate 1.0 | No fitness signal; OpenEvolve keeps the seed as "best" on ties | Phase 3: real build plus ChampSim run | Next phase |
| 3 | `-fsyntax-only` doesn't link | A member declared but never defined passes | Phase 3's real build | Next phase |
| 4 | Compiler feedback rarely reaches the model | OpenEvolve shows artifacts only to a program's children, and failures score 0 | Accept, or a custom prompt template later | Known |
| 5 | Which compiler builds candidates on HPC3? | coe-hpc1 built ChampSim and its vcpkg libraries with gcc 12.2; HPC3 nodes have only gcc 11.5 | **Decided 2026-10-23:** the nodes' system gcc 11.5, with no sandbox. M2 starts with a test build against the gcc-12 libraries; the fallback is rebuilding vcpkg offline on HPC3 | Decided; tested in M2 |
| 6 | The only trace is a smoke trace with flat fitness | LRU, SRRIP, DRRIP and SHiP land within 0.2% IPC on it | Calibrate LLC-sensitive traces (contract §2) | Open |
| 7 | `env/check.sh` requires `AFAR_ASSETS` to be writable | Teammates sharing a read-only copy fail `make check` | `check.sh` accepts a readable copy; `make check` passes `AFAR_ASSETS`/`AFAR_RESULTS` through its clean environment; `llm-fetch` refuses an unwritable one with a clear message | **Fixed in M1** |
| 8 | pip's cache isn't redirected | `~/.cache/pip` is 3 GB on the 99%-full `/home` (mostly older than this repo) | The Makefile runs pip with `--no-cache-dir`. The existing 3 GB is the user's to delete | **Fixed in M1** |
| 9 | `cs002` GPU 0 is faulty (73 uncorrectable ECC errors) | Jobs exclude `cs002` and lose its 3 healthy GPUs | Report to HPC support (a note is drafted) | The user's call |
| 10 | Two near-identical OpenEvolve job scripts | Changes have to be made twice | A shared helper once a third script appears | Deferred |

## Next steps

The plan to an autonomous, multi-node research system is in **`docs/roadmap.md`**: its architecture, research levers, failure handling and milestones M0–M7.

| Milestone | State |
|---|---|
| **M0** HPC3 capability check | **Done (2026-10-23).** Jobs can submit jobs; `scrontab` is disabled, so the watchdog uses `afternotok` handlers plus a `--begin` heartbeat; `MaxArraySize` is 1001; `JobRequeue=1`; `/bin/mail` is available |
| **M1** Baseline in Slurm, result schema, manifest v1, and issues 1, 7 and 8 | **Done (2026-10-23).** Job 94177 reproduced 43,703 cycles on HPC3 and wrote validated `smoke-v1` baselines. Issues 1, 7 and 8 are fixed |
| **M2** Real evaluator (resolves issues 2, 3 and 5) | **Next.** Issue 5 is decided: build with the nodes' gcc 11.5 and no sandbox, starting with a toolchain test job. Also measure whether copying traces to node-local `/tmp` speeds up runs |
| M3 Trace calibration (issue 6) · M4 Single-job prototype with real fitness | Planned |
| M5 Staged generations (issue 10) · M6 Long-run hardening · M7 Comparing levers | Planned |
