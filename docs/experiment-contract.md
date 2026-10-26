# Experiment contract (v0)

**Status: agreed; partly implemented.** This records the BUILD_PLAN Phase 0 decisions so later phases build against one definition. §1's seed and static checks, plus a compile-only check, exist since R7 (`seeds/evolved_repl.h`, `evaluator/compile_check.py`, `docs/champsim-evaluator.md`). The real build and simulation arrive in Phase 3, and the controller in Phase 4.

Scope: evolve the **LLC replacement policy** of ChampSim at the pinned commit. Everything else stays fixed: `configs/champsim/baseline.json`, which is ChampSim's default single-core config with no prefetchers.

## 1. Candidate format

- A candidate is **one self-contained C++ header**, `evolved_repl.h`, defining `class evolved_repl : public champsim::modules::replacement` fully inline.
- The hooks match the module API at the pinned commit (see `external/ChampSim/replacement/lru/lru.h`):
  - required: `find_victim` and `update_replacement_state`
  - optional: `replacement_cache_fill`, `initialize_replacement` and `replacement_final_stats`
  - ChampSim calls a hook only if its traits detect that exact signature (`inc/cache.h:447-494`). Otherwise it silently uses a default, so the build succeeds but the hook never runs. The evaluator `static_assert`s that the required hooks are detected and that `evolved_repl(CACHE*)` exists; a candidate that fails is `compile_failed`.
- `// EVOLVE-BLOCK-START` and `// EVOLVE-BLOCK-END` wrap the whole class body (state, constructors, hooks). The includes, the class head and the closing `};` stay outside the block.
- Why header-only:
  - OpenEvolve evolves a single file.
  - ChampSim includes every `.h` in a module directory and accepts a directory with no `.cc` files.
  - The module's class name must equal its directory name.
- How it is built: the evaluator places the file at `<workspace>/repl/evolved_repl/evolved_repl.h` and runs `config.sh --replacement-dir <workspace>/repl` with `LLC.replacement = "evolved_repl"`. This is ChampSim's standard out-of-tree module mechanism, with no source edits.
- A candidate's identity is the sha256 of its file. Candidate files are immutable once written.
- The seed is an exact port of stock `lru`, so its fitness must be exactly 1.0.
- Static checks reject a candidate as `invalid_candidate` when:
  - it exceeds the size limit
  - an EVOLVE-BLOCK marker is missing, or there is more than one block
  - any text outside the EVOLVE-BLOCK differs from the seed's. Trailing whitespace on each line and at the end of the file is ignored; OpenEvolve's full-rewrite parser strips the end.
  - the `evolved_repl` class declaration is missing
  - the EVOLVE-BLOCK uses a forbidden construct: process, file or environment calls, inline assembly, `__attribute__`, threads, or preprocessor lines (`evaluator/static_checks.py`). This is a safeguard, not a sandbox.
- The evaluator enforces the EVOLVE-BLOCK itself, because OpenEvolve 0.4.0's `enforce_evolve_blocks` never reaches its workers (`docs/llm-serving.md`, R6). Its rejection note (an artifact) says what changed, mainly for people reading the run: OpenEvolve shows a program's artifacts only in prompts that use it as the parent (`process_parallel.py:156`), and a rejected candidate scores 0, so it rarely is one. The system prompt is what tells the model to keep the markers and fixed code. Revisit when an OpenEvolve release has the fix: OpenEvolve would then restore outside edits instead, and this check stays as a backstop.

## 2. Experiment manifest

There is one hand-written JSON file per experiment, `configs/experiments/<experiment_id>.json`. It is immutable once a campaign uses it.

| key | content |
|---|---|
| `contract_version`, `experiment_id`, `purpose` | identification |
| `champsim` | `repo`, `commit`, `base_config`, `target_cache: "LLC"`, `module_name: "evolved_repl"` |
| `simulation` | `warmup_instructions`, `simulation_instructions` |
| `baseline` | `policy: "lru"` |
| `traces` | list of `{id, path, sha256}`; traces live outside the repo, and `path` is relative to `$AFAR_ASSETS` (for example `traces/602.gcc_s-1850B.champsimtrace.xz`), so one manifest works for every user |
| `limits` | `build_timeout_s`, `run_timeout_s`, `max_source_bytes` |
| `fitness` | `name: "geomean_ipc_speedup"`, `version` |

**Smoke trace:** `602.gcc_s-1850B` at 1M warmup / 5M simulated instructions (`smoke-v1`), for **plumbing only**. On this trace the LLC hit rate is about 0.3%, and LRU, SRRIP, DRRIP and SHIP land within 0.2% IPC of each other, so fitness is flat.

**Research trace set (M3, `docs/trace-calibration.md`):** the 19 CRC-2 SPEC CPU2006 SimPoints that ArchAgent used, pinned in `configs/traces/crc2-archagent19.json`. A trace is admitted only if both hold at the 50M + 200M reference length:
- LRU's demand MPKI is above 1
- the built-in policies' IPCs differ by at least 1%

Calibration then picks the shortest run length that ranks the built-in policies like the reference does. It writes two manifests:
- `crc2-evolve-v1`, for evolution: 14 admitted traces at 20M + 50M (2026-10-25). This is the rule's flagged fallback, so it's a screening score.
- `crc2-validate-v1`, at the reference length: the same 14 at 50M + 200M, where top candidates are confirmed

Records of a single-trace array task (`evaluate_candidate --trace`) carry the full manifest's sha256.

## 3. Result schema

There is one JSON record per (candidate, trace). Every pair in the manifest gets exactly one record; a compile failure writes one `compile_failed` record per trace.

- **Identity:** `schema_version`, `experiment_id`, `experiment_sha256`, `campaign_id`, `generation`, `candidate_id`, `candidate_sha256`, `trace_id`.
- **Outcome:** `status`, `failure_class`, `error_summary`, `exit_code`, `build_seconds`, `run_seconds`.
- **Metrics:** `ipc`, `instructions`, `cycles`, `llc_demand_mpki`, `llc_hit_rate`.
  - They are flat and non-null exactly when `status == "completed"`.
  - They come from the ROI section of ChampSim's `--json` output, never from stdout.
  - **Metric definitions v1** (`evaluator/results.py`):
    - `ipc` = instructions / cycles, for core 0.
    - Demand accesses are LLC `LOAD` + `RFO`. `llc_demand_mpki` = 1000 × demand misses / instructions, and `llc_hit_rate` = demand hits / demand accesses (0.0 if there are none).
  - Records are written to `$AFAR_RESULTS` (`env/site.sh`), atomically (a temporary file, then a rename), with ChampSim's raw log and JSON in `raw/` beside them:
    - `baselines/<experiment>/<trace>.json`: the LRU baseline, built by the same pipeline as candidates (`evaluate_candidate --baseline`)
    - `evaluations/<experiment>/<candidate id>/`: candidates, plus a `summary.json`
    - `reference/<experiment>/`: M1's runs of the coe-hpc1-built binary, a reproducibility check only
- **Provenance:**
  - `logs`: build log, run log and ChampSim JSON path
  - `env`: `champsim_commit`, `base_config_sha256`, `config_sha256`, `project_git_commit`, `hostname`, `cxx_version`, `slurm_job_id`, `slurm_array_task_id`
  - `started_at` and `finished_at`

Baselines use the same schema (stock `lru`, at `baselines/<experiment_id>/<trace_id>.json`).

## 4. Fitness

```
speedup_t      = ipc_candidate_t / ipc_lru_t
combined_score = geometric_mean(speedup_t)   if every trace completed   (1.0 = LRU parity)
               = 0.0                         if any trace has a candidate-class failure
```

- `combined_score` is the only fitness key OpenEvolve sees. Fitness functions are named and versioned in `evaluator/fitness.py`, and the manifest picks one.
- A candidate's score is compared only with a baseline from the same ChampSim commit and compiler; otherwise it's `infra_error`.
- A missing baseline, a missing record or any `infra_error` raises an error instead of producing a score.
- ChampSim is deterministic for a fixed binary, config and trace, so each pair runs once.

## 5. Failure policy

| status | class | combined_score | retried | Slurm exit (Phase 5+) |
|---|---|---|---|---|
| `completed` | none | geomean | n/a | 0 |
| `invalid_candidate` | candidate | 0.0 | no | 0 |
| `compile_failed` | candidate | 0.0 | no | 0 |
| `runtime_failed` (non-zero exit / signal) | candidate | 0.0 | no | 0 |
| `timeout` (evaluator kills the process group) | candidate | 0.0 | no | 0 |
| `infra_error` (missing trace or baseline, unparseable output after exit 0, filesystem) | infra | raise | OpenEvolve `max_retries: 1` | non-zero |

- **Candidate failures** never stop a campaign. The tail of the compiler or ChampSim stderr goes back to OpenEvolve as `artifacts`. OpenEvolve shows a program's artifacts only in prompts that use it as the parent, so the LLM sees a failure only when that program is chosen as one.
- **Infrastructure failures** stop the stage. A preflight check runs before any candidate. It verifies the manifest, the trace hashes and the baselines, and checks that the seed reproduces LRU cycles exactly.
- **Known gap:** OpenEvolve stores a raised error as a 0-scored program. `infra_error` records make these auditable, and the Phase 4 controller must halt on them.
- **Subprocess timeouts:** OpenEvolve's own `evaluator.timeout` does not kill subprocesses, so the evaluator enforces its own.
- **Untrusted code:** candidate code runs only inside Slurm allocations, never on the login node.
