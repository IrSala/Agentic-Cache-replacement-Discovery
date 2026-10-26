# Roadmap: an autonomous, long-running research system

Agreed 2026-10-23. This takes the repo from Phase 1's compile-only loop to a tool that evolves, evaluates and repeats unattended for days, across many HPC3 nodes:
- It survives failures without corrupting results.
- The team can swap research levers (evaluator policy, fitness, traces, model, prompts, search settings) quickly and compare runs fairly.

Each milestone gets its own detailed plan and review stop before implementation. Progress is tracked in `docs/status.md`.

## Target architecture

This is BUILD_PLAN phase 6, staged generations, adjusted for failures.

```
make campaign-start EXP=<manifest>
        │
        ▼
  ┌─ generation g (GPU, minutes) ─────────────────────────────────────────────┐
  │  load checkpoint g-1 → sample N parents → vLLM answers N prompts at once   │
  │  → write N immutable candidates + manifest_g                               │
  └────────────────┬───────────────────────────────────────────────────────────┘
                   │ afterok
  ┌─ evaluation g (CPU array, many nodes, --array=0-N%K) ─────────────────────┐
  │  one task per candidate: static checks → build → run traces → result JSON │
  │  candidate failures exit 0; infrastructure failures exit non-zero          │
  └────────────────┬───────────────────────────────────────────────────────────┘
                   │ afterany   (afterok on an array never fires if one task fails)
  ┌─ aggregation g (CPU, seconds) ────────────────────────────────────────────┐
  │  validate results → retry missing or infra tasks (bounded) → fitness       │
  │  → add programs to OpenEvolve's database → checkpoint g → summary_g        │
  │  → check stop conditions → submit generation g+1, or halt with a reason    │
  └────────────────────────────────────────────────────────────────────────────┘
   + for each stage, a failure handler (--dependency=afternotok:<stage>): repair and resubmit (bounded), or halt and mail
   + an hourly heartbeat job (--begin=now+1hour; it reschedules itself) that restarts a chain with no live jobs
```

| Design choice | Reason |
|---|---|
| Staged generations, not a GPU job that runs for weeks | The GPU would sit idle while CPU evaluation runs. Short GPU jobs get scheduled into gaps sooner, and vLLM can answer a whole generation at once (the KV cache fits about 42 requests of 8K tokens). |
| A thin controller of our own, built on OpenEvolve's public pieces (`ProgramDatabase.sample_from_island/add/save/load/migrate_programs`, `PromptSampler.build_prompt`, `LLMEnsemble.generate_with_context`, `parse_full_rewrite`) | `openevolve-run` evaluates inside the same process, and 0.4.0 has no distributed evaluation. The generation stage repeats `_run_iteration_worker` (`process_parallel.py:140-330`) without evaluating. Checkpoints stay in OpenEvolve's format, and one adapter module confines the coupling to 0.4.0. |
| Plain Slurm (`sbatch`, arrays, dependencies) and files on shared storage; no workflow framework | HPC3 is offline. This is what BUILD_PLAN prescribes, it's easy to inspect, and all state is in files that can be recovered. |
| An `Executor` interface with `SlurmExecutor` and `LocalExecutor` | The whole chain can be tested on coe-hpc1 with the fake LLM and a fast fake evaluator before any GPU time is used. |
| The node's system gcc 11.5 to build candidates, with no sandbox (team decision, 2026-10-23), plus a forbidden-call list and resource limits | Simplest to start. **Remaining risk:** candidate code runs with the submitting user's permissions. An Apptainer evaluator image would sandbox it, if that's needed later. |

## HPC3 capabilities the design relies on (M0, measured 2026-10-23)

| | |
|---|---|
| Slurm | 23.11.10 |
| Can jobs submit jobs? | **Yes**: compute nodes have `sbatch`/`squeue`/`sacct`, and `sbatch --test-only` worked from `c20` |
| `scrontab` | **Disabled** (`ScronParameters = (null)`), so the watchdog is built from Slurm jobs, as above |
| `JobRequeue` | 1: jobs are requeued after a node failure, so every stage must be idempotent |
| Mail | `MailProg = /bin/mail`, so `--mail-type=FAIL` can be used (delivery is tested in M6). The address comes from `AFAR_MAIL_USER`, never from the repo. |
| Limits | `MaxArraySize` 1001 (indices 0–1000), `MaxJobCount` 10000, `normal` QOS with no per-user cap, `gpu3nodes` 2 nodes per user |
| Storage | `/data` 96% full (4.1 TB free; inodes 2% used), `/scratch` 90% (12 TB free) |

## Research levers: one manifest, swappable parts

Each experiment manifest, `configs/experiments/<id>.json` (JSON per contract §2, so the evaluator stays standard-library only), names one choice for each lever. It's immutable once a campaign uses it, and its sha256 goes into every result. So two campaigns that differ in one lever can be compared directly.

| Lever | Where it lives | How to swap it |
|---|---|---|
| Evaluator policy | `evaluator/policies/<name>.py`, each implementing one interface: `static_check → build → run → score`, found by name | Today's stub becomes the `compile_only` policy; the real one is `champsim_ipc` |
| Fitness | `evaluator/fitness.py`: named, versioned functions (`geomean_ipc_speedup@1`) | Add a function and select it in the manifest |
| Traces and simulation length | Manifest (`traces` pinned by sha256, `warmup`/`sim` instruction counts) | Edit the manifest |
| Model | `configs/llm/<profile>.env` (image, model, revision, serving flags) | Add a profile, such as a 14B model, and select it |
| Prompts | `configs/prompts/<name>/` (system message and templates, through OpenEvolve's `template_dir`) | Add a directory |
| Search | Manifest's OpenEvolve section (islands, exploitation ratio, population, candidates per generation) | Edit the values |
| Candidate format | `seeds/<name>.h` plus its EVOLVE-BLOCK rules | Add a seed |

## How failures are handled

| Failure | Detected by | Handling |
|---|---|---|
| A candidate is invalid, fails to compile, crashes or times out | Evaluator status | Score 0 and record it; the array task exits 0 and the campaign continues |
| An infrastructure error in a task (missing trace, disk, bad node) | Non-zero exit plus an `infra_error` record | Aggregation retries only those tasks, up to 2 times, with `--exclude=<node>`. If they still fail, the campaign halts with `halted: infra` and the task logs. |
| A node fails, a task hits its time limit, or a job is pre-empted | Slurm state and a missing result | Treated as infrastructure and retried |
| A bad GPU, or the LLM server fails to start | GPU preflight / server health wait (`slurm/llm_serve.sh`) | The generation job exits non-zero. Its `afternotok` handler resubmits it with `--exclude=<node>`, a bounded number of times, then halts and mails. |
| A stage is cancelled, crashes partway, or is requeued after a node failure (`JobRequeue=1`) | Completion markers | Every stage is idempotent: it skips finished items, writes atomically (temporary file, then rename), and resumes from the last good generation |
| A dependency that can never be satisfied, or a handler that never ran | `--kill-on-invalid-dep=yes`; the hourly heartbeat finds no live jobs for a campaign that isn't halted | The heartbeat restarts the chain from the last good generation |
| A duplicate candidate | sha256 of its content | Its results are reused, keyed by (candidate, trace, manifest) |
| Storage running out, a runaway campaign, a budget limit | Aggregation's preflight; stop conditions in the manifest (generations, wall time, target fitness, consecutive failed generations); a `STOP` file | Halt cleanly with a reason in the campaign status |

## Milestones

Each milestone ends with a short HPC3 job (`--test-only` first, submitted from g17) and a `docs/status.md` update.

| # | Milestone (BUILD_PLAN phase) | Deliverables | Done when |
|---|---|---|---|
| **M0** | HPC3 capability check | **Done, 2026-10-23** (table above) | Jobs can submit jobs; there's no `scrontab`; arrays of up to 1001 tasks |
| **M1** | Baseline in Slurm, plus result schema and manifest v1 (phase 2) | `evaluator/results.py` (writes and validates the contract's §3 schema), `configs/experiments/smoke-v1.json`, `slurm/baseline.sbatch` → `baselines/<exp>/<trace>.json`. Clean-up of `docs/status.md` issues 1 (neutral seed comment), 7 (read-only assets in `check.sh`) and 8 (pip cache) | A CPU job reproduces 43,703 cycles and writes JSON that passes validation |
| **M2** | Real evaluator (phase 3) | **Toolchain test first:** build the baseline with `/usr/bin/g++` 11.5 on a `cpuqs` node against the vcpkg libraries (fallback: rebuild vcpkg offline on HPC3 from sources downloaded on coe-hpc1). Then: the evaluator interface and the `champsim_ipc` policy (workspace on the node's `/tmp`, `config.sh --prefix <ws> --replacement-dir`, `make`, a run per trace under `prlimit` with a process-group timeout, ROI parsed from `--json`), fitness v1, a build cache keyed by sha256, the forbidden-call list, and `evaluator/evaluate_candidate.py` (one manifest entry in, result JSON out), used by both OpenEvolve and the array tasks | The seed's fitness is **exactly 1.0**; SRRIP/DRRIP/SHiP score ≠ 1.0; every status is reproduced by test files; build and run seconds are measured |
| **M3** | Trace calibration (contract §2) | Download traces on coe-hpc1 into `/data`, pinned by sha256: ArchAgent's 19 CRC-2 SPEC CPU2006 traces, none of which is on the DPC-3 server. Calibration arrays run the built-in policies × the traces at three lengths. Then `configs/experiments/crc2-evolve-v1.json` and `crc2-validate-v1.json` (`docs/trace-calibration.md`) | Traces admitted by pre-set rules (LRU MPKI > 1, ≥1% IPC spread between policies), with measured seconds per trace. Also: whether the 12,000-cycle step seen on `smoke-v1` persists, and whether DRRIP differs from LRU there (`docs/status.md`, issue 11) |
| **M4** | Single-job prototype with real fitness (phase 4) | The R7 job with `champsim_ipc` running on the GPU node's CPUs (about 16 CPUs, `parallel_evaluations` 4) and short simulations | Fitness varies across candidates; ≥2 stop-and-resume cycles work; the LLM/evaluation time split is measured, which sizes M5 |
| **M5** | Staged generations (phases 5–6) | `controller/campaign.py` (the campaign directory, atomic writes, an events JSONL, the stages and the CLI), `oe_adapter.py`, and `executor.py` (Slurm and local executors). `slurm/campaign_generate.sbatch`, `campaign_evaluate.sbatch`, `campaign_aggregate.sbatch`. `make campaign-start/status/stop/resume`. Failure tests on coe-hpc1 with `LocalExecutor` and fakes (`docs/campaigns.md`) | A 3-generation campaign finishes unattended on HPC3. A deliberately killed task is retried, a cancelled stage resumes, and a persistent infrastructure error halts with a clear status |
| **M6** | Long-run hardening and observability (phases 7–9) | `slurm/campaign_handle.sbatch` (the `afternotok` handler) and `slurm/campaign_heartbeat.sbatch` (reschedules itself hourly with `--begin`, singleton). Also: stop conditions and a budget from `sacct`, a free-space floor on `/data`, a campaign status summary, and mail on halt to `$AFAR_MAIL_USER` after a delivery test (`docs/campaigns.md` §5–6). Storage clean-up was dropped: a candidate's evaluation takes 177 KB. Then the first `crc2-evolve-v1` campaign (moved from M5) | A 48-hour unattended pilot campaign recovers from at least one injected failure, with no manual resubmission |
| **M7** | Comparing levers and scaling out (phase 10) | A second model profile, islands across 2 GPU nodes (the QOS cap), `make campaign-compare A B` | Two campaigns that differ in one lever produce a comparable summary |

**Why this order:**
- **M1–M3 first:** without real fitness there's nothing to evolve toward, so the evaluator foundation comes before any autonomy.
- **M4 before M5:** it proves real fitness in the single-job setup that already works, before distributing it.
- **M5–M6:** the main build, then hardening it for long runs.

## Verification approach

- **Without GPU or Slurm:** every component can be tested on coe-hpc1, using the fake LLM (`scripts/fake_llm_server.py`), a fake evaluator policy and the `LocalExecutor`. That includes failure tests for M5–M6: kill a task, corrupt a result, delete a trace, fill a quota, stop the LLM.
- **On HPC3:** each milestone's job follows the routine proven in Phase 1: `bash -n`, fake-mode test, `--test-only`, submit, then read the logs from `/home`.
- **Provenance:** every result records the manifest sha256, the commits, the compiler version, the node and the job IDs (contract §3), so a run can be repeated and compared.
