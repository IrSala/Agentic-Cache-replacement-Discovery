# Project status

As of 2026-10-26. What works, how it was verified, what was measured, and what's next. Update this page at the end of each milestone. The plan going forward is in `docs/roadmap.md`.

## Summary

- **Phase 1 is complete.** A fresh clone of the GitHub repo builds on coe-hpc1 with one `make setup`, and runs on COE-HPC3 with no modules. A GPU job serves a pinned 7B model with vLLM next to OpenEvolve, and OpenEvolve evolves the real target: a ChampSim LLC replacement policy in one C++ header.
- **Candidates can now be scored for real (M2).** `evaluator/evaluate_candidate.py` (the `champsim_ipc` policy) builds a candidate into ChampSim on an HPC3 node, runs the manifest's traces, and scores IPC against LRU. The seed scores exactly 1.0. OpenEvolve runs have used it since M4.
- **A research trace set is calibrated (M3).** 14 of the 19 CRC-2 traces ArchAgent used separate the built-in policies. On them, the built-ins rank as in the literature (SHiP +5.95%, DRRIP +5.73%, SRRIP +5.15% over LRU at 50M + 200M). Evolution will score at 20M + 50M (`crc2-evolve-v1`) and confirm at 50M + 200M (`crc2-validate-v1`).
- **OpenEvolve evolves with real fitness in one GPU job (M4).** On the 4-trace dev set, 120 iterations across 3 resumed jobs gave 33 distinct fitness values (best 1.1881, about the built-ins' level). A job stopped at its time limit resumes cleanly.
- **Evolution runs as a staged campaign (M5).** A GPU job writes a whole generation of 24 candidates in under 2 minutes. A CPU array evaluates them, and a small job adds the scores and starts the next generation (`docs/campaigns.md`).
  - On the dev set, 3 generations ran unattended in 27 minutes: 22 s per iteration against M4's 34 s, with the GPU held a fifth of the time.
  - A killed task was retried, a cancelled stage resumed, and a persistent failure halted with a clear message.
- In BUILD_PLAN's terms, phases 0–6 are done (table below). Roadmap milestones M0–M5 are done.
- **M6, hardening for long runs, is in progress.** Its watchdog, mail on halt and stop conditions pass their checks on coe-hpc1 (part A, `docs/campaigns.md` §5–6). Its four HPC3 tests passed on `crc2-dev-v1` (part B): a failed or cancelled stage was restarted by its handler, a stalled one by the heartbeat, a persistent crash halted and mailed, and a stop halted at once. The 48-hour pilot is next (part C).
- **Runs are assembled from swappable parts (M4 part A).** A run spec, `configs/runs/<name>.yaml`, names the seed, model profile, prompt profile, search profile and evaluator policy, and one generic job, `slurm/openevolve.sbatch`, runs any spec (`docs/run-specs.md`).

## Where each BUILD_PLAN phase stands

| BUILD_PLAN phase | Status | Evidence |
|---|---|---|
| 0. Experiment contract | **Agreed; partly implemented** | `docs/experiment-contract.md`. Implemented: the candidate format, the static checks and the failure statuses for compile-only checks |
| 1. Reproducible environment | **Done** | Portable Python and venvs (`.python/`, `env/*-requirements.txt`), records in `env/versions.txt`; `make setup` from a fresh GitHub clone passed (step G) |
| 2. ChampSim baseline | **Done (M1)** | Pinned `410cee6`, built with static libstdc++. `slurm/baseline.sbatch` ran LRU on HPC3 (job 94177) and wrote validated §3 records to `$AFAR_RESULTS/baselines/`, reproducing coe-hpc1's 43,703 cycles exactly |
| 3. Standalone evaluator | **Done (M2)** | `evaluator/evaluate_candidate.py`: static checks, a build from the prebuilt base tree, runs under `prlimit`, ROI metrics, the contract's statuses and `geomean_ipc_speedup@1`. Self-test 10/10 on HPC3 (job 94248) |
| 4. Persistent-controller prototype | **Done (M4)** | One GPU job runs vLLM plus OpenEvolve with real `champsim_ipc` fitness on the node's CPUs, assembled from a run spec, with resume across jobs and clean time-limit stops (jobs 95745, 95767–95769, 95821–95822) |
| 5–6. CPU arrays, staged generations | **Done (M5)** | A campaign's GPU job generates a generation, a CPU array evaluates it, and an aggregation job updates OpenEvolve's database and submits the next stage. Stages are idempotent; failed tasks are retried, persistent failures halt, and stopped stages resume (`m5-dev-a`, `-b`, `-c`: jobs 95825–96182) |
| 7–10. Campaign state, hardening, observability, islands | In progress: M6 (phases 7–9), then M7 (phase 10) | M6 part A on coe-hpc1: failure handlers, a heartbeat, mail on halt, stop conditions and a budget |

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
| OpenEvolve on a toy / on the ChampSim policy, compile-only | `SPEC=toy-v1` (R6) / `SPEC=champsim-compile-v1` (R7) | `docs/llm-serving.md`, `docs/champsim-evaluator.md` |
| A staged campaign: a GPU job generates, CPU arrays evaluate, a small job aggregates | `make campaign-start SPEC=<name>` on g17; `make campaign-status`, `campaign-stop` (`NOW=1`) or `campaign-resume C=<id>`; `make campaign-selftest` on coe-hpc1 | `docs/campaigns.md` |
| A campaign's watchdog, mail on halt and limits (M6; the watchdog and mail verified on HPC3, the limits on coe-hpc1) | automatic; limits in the spec's `campaign:` section; `make mail-test` on g17 tests delivery | `docs/campaigns.md` §5–6 |
| Evaluate a candidate for real: baseline, candidate file or built-in policy | `sbatch slurm/evaluate.sbatch` (or `python -m evaluator.evaluate_candidate`) | `docs/champsim-evaluator.md` (M2 part B) |
| Test job scripts without a GPU | `AFAR_LLM_FAKE=1` or the `fake-v1` model profile (plus `AFAR_LLM_FAKE_REPLY=<file>` to replay a real reply) | `docs/llm-serving.md` |
| Summarize a run | `make oe-summary RUN=runs/<name>-<jobid>` | |

## Verified on HPC3

| Step | Job(s) | Result |
|---|---|---|
| M6 part B: the heartbeat restarts a stalled stage (`m6-dev-f`) | from 96459; handler 96468 and aggregation 96467 cancelled | With both gone, nothing else could notice. Heartbeat 96462 (about 23:22 UTC) found no live job but a status under 10 min old, and left it. Heartbeat 96531 (23:32:42) found the stage stalled for 18 min, logged `stalled`, and resubmitted the aggregation (96534) with a handler. It added generation 1's 24 programs 5 s later, and the campaign went on |
| M6 part B: a cancelled aggregation is restarted by its handler (`m6-dev-b`) | from 96303; aggregation 96329 cancelled | Its handler (96330) ran, so Slurm counts a cancel as a failure for `afternotok`. It resubmitted the aggregation as 96380 (attempt 2), waiting with `afterany` on the array (96328) that was still running. Both generations finished (best 1.1964). Generation 1's 24 candidates were exactly m5-dev-c's (issue 13) |
| M6 part B: a failed generation job is restarted (`m6-dev-a`, then `m6-dev-e`) | 96300 and 96307; 96425 and 96428 | **First try (m6-dev-a):** the injected failure on `cs001` (exit 2) was restarted as 96307, with `cs001` excluded. That left only `cs003` and `cs004`, which were full, so Slurm estimated a start 1.5 days later, while `cs001` had free A100s. So now a restart avoids a node only after exit 3 (a node check failed), `NODE_FAIL` or `BOOT_FAIL`. m6-dev-a was stopped with `NOW=1`, which cancelled the queued job and its handler. **On the fixed code (m6-dev-e):** the same failure restarted as 96428 with only `cs002` excluded, ran on `cs001`, and both generations finished (best 1.1984) |
| M6 part B: a persistent crash halts and mails (`m6-dev-c`) | 96312–96326 | Aggregations 96313, 96316 and 96322 crashed (injected), each restarted by its handler. After the third, the campaign halted with `stage_failed`, naming the last job, exit 2:0, node `c7` and the log, 35 s after the first crash. Mail job 96326 ran, and the mail arrived |
| M6 part B: `make campaign-stop NOW=1` (`m6-dev-d`) | 96354–96356 cancelled | It cancelled the evaluation array, the aggregation and its handler, and halted as `stopped` 5 s later. Nothing ran after it, and no mail was sent |
| M6 part B: heartbeats, one at a time, ending with their campaigns | 10 heartbeat runs across the six campaigns (`sacct`) | Each run took 4–6 s. A campaign's runs were about 10 min apart and never overlapped. Each chain ended at its next run after its campaign finished or halted (`ended (finished)` or `ended (halted)`). The only mail was m6-dev-c's: handlers that Slurm removed unrun, and failed stage jobs, sent none |
| M6: mail delivery (`make mail-test`) | 96270 (`homacs1`) | Slurm's own message (`--mail-type=END`) arrived. The node has no `/bin/mail`, `mailx` or `sendmail`, so it couldn't send its own. Campaigns therefore mail through Slurm: a 1-second job whose name is the subject |
| M5 part B: `m5-dev-v1`, 3 generations × 24 candidates, unattended (`m5-dev-a`) | 95825–95890; generation jobs 95827, 95860 (`cs004`), 95887 (`cs001`) | Finished in 27 min with no intervention; every stage ran first time. Of the 72 iterations: 41 `completed`, 8 `compile_failed`, 3 `invalid_candidate`, 16 `runtime_failed`, and 4 rejected as identical to their parent. 4 duplicates shared an evaluation. Best `combined_score` 1.2011, found in generation 1 (M4's best: 1.1881) |
| M5 part B: a killed task is retried, and a cancelled stage resumes (`m5-dev-b`) | 95915–96182: task 95981_19 and aggregation 96012 cancelled | **Retry:** task 19 of generation 2 was cancelled mid-run. Aggregation resubmitted only that task (job 96006, attempt 2), which completed on `c11`, and generation 3 followed. No node was excluded, since a cancelled task writes no infrastructure marker. A first try (task 95919_3) came too late: that candidate had already finished. **Resume:** generation 3's aggregation (96012) was cancelled while it waited for its array. `make campaign-resume` refused while the array (96011) still ran. Run again once the array was done, it submitted aggregation 96182, which added the generation's 22 programs and finished the campaign. The final checkpoint holds 50 programs (the population cap), iterations 0–72, none repeated. The campaign sat idle for about 3 h 45 min until the resume (issue 12) |
| M5 part B: a persistent infrastructure failure halts (`m5-dev-c`, a `faults.json` failing generation 1's task 0 on every attempt) | 95944–96004 | Attempt 1 failed on `c20`. Aggregation resubmitted the task with `--exclude=c20`; it failed on `c2`. Then with `--exclude=c2,c20`; it failed on `c6`. The campaign then halted with `halted: infra` and the message "1 of 24 evaluation(s) of generation 1 failed 3 time(s): task 0 (376739fb594257a8): injected by faults.json (attempt 3) on c6", followed by the log directory. It halted 9 min after the start |
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
| Staged campaign (`m5-dev-v1`: 24 candidates × 4 CPUs per generation) | 8.6–9.4 min per generation. The GPU job takes 1.7–1.9 min: the server is healthy after 70–75 s, and the 24 batched replies take 17–27 s (one reply took 13.2 s in M4). Evaluation plus aggregation takes 6.9–7.7 min. Over the campaign, 22 s per iteration against 34 s in M4's single job, and the GPU was held about 5.5 of the 27 min | M5 part B |
| Why candidates crash (`m5-dev-a`, 16 `runtime_failed`) | 10 uncaught `std::out_of_range`, as `.at()` throws on a bad index; 5 ChampSim assertions on the returned way (`set_begin <= way`, `way <= set_end`, `victim < end`); 1 segfault | M5 part B |
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
| Evolution as a chain of short jobs (generate on a GPU, evaluate in CPU arrays, aggregate), in plain Slurm with all state in files | Evaluation took 7× the LLM's time in one job, so the GPU sat idle. Short jobs spread evaluation over CPU nodes and hold a GPU only for minutes. Every stage is idempotent, so retries, requeues and resumes are safe | `docs/campaigns.md` |
| Full rewrites, not diffs | The 7B model ignored the SEARCH/REPLACE format (0/10 applied) | R6 |
| No top-level LLM seed | vLLM honours the seed, so identical prompts got identical replies | R6 |
| The evaluator enforces the EVOLVE-BLOCK itself | OpenEvolve 0.4.0's `enforce_evolve_blocks` never reaches its workers | `docs/experiment-contract.md` |
| A compile check that `static_assert`s ChampSim's hook detection | A hook with the wrong signature otherwise compiles and is silently never called | `docs/champsim-evaluator.md` |
| ArchAgent's 19 CRC-2 traces, calibrated under pre-set rules | Comparable with published work; the rules were fixed before any result, so they couldn't be tuned to it | `docs/trace-calibration.md` |
| Evolve at 20M + 50M, confirm at 50M + 200M | No short length met the ±0.5 pp rule. L2 ranks the policies correctly at a quarter of the reference's cost, so it's the rule's flagged fallback (chosen 2026-10-25 over testing 50M + 100M) | `docs/trace-calibration.md` §4 |
| A failure handler per stage job, plus a heartbeat at most hourly per campaign | Handlers cover the failures seen so far; the heartbeat covers a handler that fails or never runs. Hourly is 60× slower than the strictest polling rule found (Cirrus), as often as NERSC's own hourly restarts, and costs about 2 CPU-minutes a day (4–6 s per run, measured). SJSU publishes no rule on periodic jobs | `docs/campaigns.md` §5 |
| Mail only on halts that need someone, sent through Slurm | A campaign retries most failures itself; per-job failure mail would mostly report problems already fixed. Compute nodes can't send mail (job 96270), so a halt submits a 1-second job whose name is the subject, and Slurm's controller mails at its end | `docs/campaigns.md` §5 |
| `--mem=24G` for the GPU jobs | Measured peak is 4.2 GB; 64G left jobs waiting for memory while GPUs were free | `docs/llm-serving.md` |

## Open issues

| # | Issue | Effect | Proposed fix | State |
|---|---|---|---|---|
| 1 | The seed's header comment ("port of replacement/lru") invites edits outside the block | In job 93495, all 5 `invalid_candidate` results were only this comment | Made policy-neutral: the LRU note now sits inside the block | **Fixed in M1** |
| 2 | The compile-only stub scores every compiling candidate 1.0 | No fitness signal; OpenEvolve keeps the seed as "best" on ties | `champsim_ipc` (M2) gives real fitness; OpenEvolve runs switch to it in M4 (`evaluator/openevolve_adapter.py`) | **Fixed in M2**; used since M4 |
| 3 | `-fsyntax-only` doesn't link | A member declared but never defined passes | `champsim_ipc` builds and links every candidate; the self-test's link-error case gives `compile_failed` | **Fixed in M2** |
| 4 | Compiler feedback rarely reaches the model | OpenEvolve shows artifacts only to a program's children, and failures score 0 | Accept, or a custom prompt template later | Known |
| 5 | Which compiler builds candidates on HPC3? | coe-hpc1 built ChampSim and its vcpkg libraries with gcc 12.2; HPC3 nodes have only gcc 11.5 | **Decided 2026-10-23:** the nodes' system gcc 11.5, with no sandbox. **Tested (job 94191):** it builds and links against the gcc-12 libraries, with dynamic libstdc++ | **Resolved** |
| 6 | The only trace is a smoke trace with flat fitness | LRU, SRRIP, DRRIP and SHiP land within 0.2% IPC on it | Calibrated 14 LLC-sensitive CRC-2 traces, with spreads of 1.1–63% between the built-ins (`crc2-evolve-v1`, `crc2-validate-v1`) | **Fixed in M3** |
| 7 | `env/check.sh` requires `AFAR_ASSETS` to be writable | Teammates sharing a read-only copy fail `make check` | `check.sh` accepts a readable copy; `make check` passes `AFAR_ASSETS`/`AFAR_RESULTS` through its clean environment; `llm-fetch` refuses an unwritable one with a clear message | **Fixed in M1** |
| 8 | pip's cache isn't redirected | `~/.cache/pip` is 3 GB on the 99%-full `/home` (mostly older than this repo) | The Makefile runs pip with `--no-cache-dir`. The existing 3 GB is the user's to delete | **Fixed in M1** |
| 9 | `cs002` GPU 0 is faulty (73 uncorrectable ECC errors) | Jobs exclude `cs002` and lose its 3 healthy GPUs | Report to HPC support (a note is drafted) | The user's call |
| 10 | Two near-identical OpenEvolve job scripts | Changes have to be made twice | One generic job, `slurm/openevolve.sbatch SPEC=`; the two old scripts were wrappers until M5 removed them | **Fixed in M4 part A** |
| 11 | Cycle counts on `smoke-v1` move in 12,000-cycle steps; DRRIP's LLC behaviour differs from LRU's, yet its cycle count is identical (job 94248) | Fitness resolution of about 0.064% on this trace, so small policy differences can read as exactly 1.0 | Check in M3 on LLC-sensitive traces and longer runs: does the step persist, and does DRRIP then differ? If it does, find its cause before trusting small fitness differences. Ruled out so far: warmup (361,901 cycles for every policy) and DRAM refresh (tREF = 32 ms / 8192 = 15,625 core cycles at 4 GHz, not 12,000) | **Closed in M3:** on all 14 admitted traces, policies' cycle counts differ at 1-cycle resolution and DRRIP differs from LRU. The quantum reappears only on near-flat `tonto_2834B` (−36,000 / +12,000 / −48,000), which is excluded. Its cause is still unknown |
| 12 | Nothing notices a campaign stage that is cancelled, or dies before it runs | The campaign waits for `make campaign-resume`: about 3 h 45 min in the `m5-dev-b` test | M6's `afternotok` handlers and hourly heartbeat resubmit the stage | **Fix written in M6 part A;** verified on coe-hpc1, HPC3 test next |
| 13 | The model's sampling isn't seeded per request: campaigns send no seed, and every generation job starts a fresh vLLM server, whose generator is seeded the same way at every start (`--seed` defaults to 0) | Campaigns of the same spec can repeat each other's candidates. Generation 1's prompts are identical in every campaign, and m6-dev-b's 24 rewrites were exactly m5-dev-c's, so its evaluations came from the cache (0.62 CPU-hours for the generation). Other campaigns differed only because their requests were batched differently. So same-spec campaigns aren't independent samples, which matters for M7's comparisons and for replicates | Give every request its own seed from the campaign ID, the generation and the candidate's index, and record it in `generation.json`: a campaign stays reproducible, and two campaigns sample independently | **Open; deferred (decided 2026-10-26).** The pilot runs without it |

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
| **M4** Single-job prototype with real fitness (issues 2, 10) | **Done (2026-10-26).**
- **Part A, modular run specs: done (2026-10-25).** On coe-hpc1: the resolver's 21 self-test cases; equivalence with R6/R7's configs, field by field; fake-LLM runs of both specs, a resume and a prompt swap; the policy contract suite. On HPC3, job 95745 ran R7 through the generic job with the same behaviour.
- **Part B, real fitness: done (2026-10-26).**
  - Done: the dev manifest `crc2-dev-v1` and the spec `m4-dev-v1` (`champsim_ipc`, 3 candidates × 4 CPUs). `champsim_ipc` ran through the generic job locally (fake LLM, `crc2-tiny-v1`).
  - Gate B passed (jobs 95767–95769): fitness varies (33 distinct values), a run resumes across jobs, the cache works across them, and the LLM/evaluation split is measured. `crc2-dev-v1`'s baselines (job 95760) reproduce the calibration's cycles.
  - A job stopped at its time limit with evaluations in flight resumes cleanly (jobs 95821, 95822).
  - Not run, by choice: a first real run on `crc2-evolve-v1` (about 1 h per candidate on 14 CPUs). It's better done with M5's CPU arrays than on 14 CPUs of a GPU node.
  - First data on the levers: half of the 7B model's rewrites fail (23 don't compile, 21 break the EVOLVE-BLOCK rules, 7 crash), and evaluation takes about 7× the LLM's time per iteration. |
| **M5** Staged generations | **Done (2026-10-26).**
- **Part A, on coe-hpc1:**
  - `controller/campaign.py`, `executor.py` and `oe_adapter.py`, the three `slurm/campaign_*.sbatch` stages, and `make campaign-*`.
  - `make campaign-selftest` passes all of Gate A: 3 generations; a retried task; a persistent failure halting with a clear message; a crashed aggregation resumed with no duplicates; `STOP` and resume; duplicates evaluated once; `openevolve-run` continuing from a campaign checkpoint.
  - One generation ran through the real stage scripts with a stand-in `sbatch`.
  - The R6/R7 wrapper scripts and `configs/llm/serve.env` are removed.
- **Part B, on HPC3:** all four acceptance tests passed on `m5-dev-v1` (`crc2-dev-v1`, 3 generations × 24 candidates):
  - an unattended campaign (`m5-dev-a`, 27 min)
  - a killed task retried, and a cancelled aggregation resumed (`m5-dev-b`)
  - a persistent infrastructure failure halted with a clear message (`m5-dev-c`)
- **First data on the levers:**
  - Batching works: a generation's 24 replies take about as long as two single replies did in M4.
  - 16 of `m5-dev-a`'s 72 candidates crashed at run time; 15 of them on an out-of-range index or way.
- **Moved to M6:** the first real `crc2-evolve-v1` campaign (decided 2026-10-26) |
| **M6** Long-run hardening | **In progress.**
- **Decided (2026-10-26):** mail only on halts that need you; a slim, hourly heartbeat, after checking SJSU's and other centers' guidance on periodic jobs; the 48-hour pilot is the first `crc2-evolve-v1` campaign, capped at 5,000 CPU-hours; no storage clean-up (a candidate's evaluation takes 177 KB).
- **Part A, done on coe-hpc1:**
  - failure handlers and a heartbeat (issue 12), where only the job `status.json` names may act
  - `sbatch` retries
  - mail on halt
  - stop conditions and a budget (`docs/campaigns.md` §5–6)
  - `make campaign-selftest` passes M5's checks and M6's, and the handler and heartbeat scripts ran against stand-ins for Slurm
- **Mail delivery (job 96270):** only Slurm's message arrived, since compute nodes have no mail program; campaigns now mail through Slurm.
- **Part B, done on HPC3 (`m6-dev-v1`, 2026-10-26):** all four tests passed:
  - a failed generation job restarted (m6-dev-e)
  - a cancelled aggregation restarted by its handler (m6-dev-b), and by the heartbeat when its handler was cancelled too (m6-dev-f)
  - a persistent crash halting with mail (m6-dev-c)
  - `campaign-stop NOW=1` (m6-dev-d)
- **Found in part B:**
  - Excluding a generation job's node after any failure stranded a restart (m6-dev-a). Now a node is excluded only on evidence it's bad.
  - The model's sampling repeats across campaigns (issue 13, deferred).
- **Next, part C:** the 48-hour pilot, `crc2-evolve-c1` |
| M7 Comparing levers | Planned |
