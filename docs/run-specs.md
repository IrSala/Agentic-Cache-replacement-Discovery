# Run specs: assembling an OpenEvolve run from swappable parts (M4)

An OpenEvolve run is assembled from named parts: the model, the prompt, the search settings and the evaluator. Swapping any one of them means naming a different part, never editing a job script or a shared config.

**The parts:**
- **Run spec:** each run configuration is one file, `configs/runs/<name>.yaml`.
- **Resolver:** `controller/runspec.py` checks that the named parts fit together, derives the settings that depend on them, and writes the exact config OpenEvolve reads.
- **Job:** `slurm/openevolve.sbatch` runs any spec.
- **Record:** `run.json` lists every input's sha256, so two runs that differ in one part can be compared directly.

## 1. A run spec

```yaml
spec_version: 1
description: R7, compile-only fitness on the ChampSim LLC policy
seed: seeds/evolved_repl.h          # the initial program, and the text candidates are checked against
model: qwen2.5-coder-7b-v1          # configs/models/<name>.env
prompt: champsim-rewrite-v1         # configs/prompts/<name>/
search: champsim-rewrite-v1         # configs/search/<name>.yaml
evaluator:
  policy: compile_only              # a registered evaluator policy (evaluator/policies/)
  experiment: smoke-tiny-v1         # an experiment manifest (configs/experiments/)
  executor: local                   # where candidates run: this node's CPUs (M5 adds slurm-array)
  parallel_candidates: 1
  cpus_per_candidate: 1
iterations: 10
overrides: {}                       # optional dotted OpenEvolve keys, applied last, e.g. database.num_islands: 2
```

**Other ways to name a part:**
- **Raw OpenEvolve evaluators:** a spec can name one instead of a policy, with `evaluator.openevolve_file`, `timeout_s` and optional `openevolve:` settings. `toy-v1` does this for OpenEvolve's example.
- **Profiles by path:** any profile can be named by a path (a value containing `/`) instead of a name. That's handy for an experiment that isn't ready to become a named profile.

| Spec | What it is |
|---|---|
| `toy-v1` | R6: OpenEvolve's function-minimization example with its own evaluator |
| `champsim-compile-v1` | R7: the ChampSim LLC policy, compile-only fitness |
| `m4-dev-v1` | M4 part B: real fitness (`champsim_ipc`) on `crc2-dev-v1`, 3 candidates × 4 CPUs, about 6 min per candidate |

## 2. The parts

| Part | Where | What it holds | Today |
|---|---|---|---|
| Model | `configs/models/<name>.env` (shell), after the shared `configs/llm/vllm.env` | The fields below this table | `qwen2.5-coder-7b-v1`; `fake-v1` (the stand-in server, no GPU) |
| Prompt | `configs/prompts/<name>/` | `prompt.yaml`: `description`, `requires` (`champsim_commit`, `module_name`) and `openevolve:` (`prompt.*` settings). Also `system_message.txt` and any OpenEvolve template overrides, named after OpenEvolve's templates (`full_rewrite_user.txt`, ...) | `champsim-rewrite-v1`, `toy-v1` |
| Search | `configs/search/<name>.yaml` | `description`, plus `openevolve:` with top-level and `database.*` settings, such as islands, population and diff vs full rewrite | `champsim-rewrite-v1`, `toy-v1` |
| Evaluator policy | `evaluator/policies/<module>.py`, registered in `evaluator/policies/__init__.py` | the `static_check → build → run → score` stages, plus the metadata and methods listed under the table | `compile_only`, `champsim_ipc` |
| Experiment | `configs/experiments/<id>.json` | traces, simulation length, fitness and limits (contract §2) | `smoke-*`, `crc2-*` |

**What a model profile sets:**
- `LLM_SERVER` (`vllm` or `fake`)
- `MODEL_ID`, `MODEL_REVISION` and `MODEL_DIR`
- `CONTEXT_TOKENS` and `VLLM_SERVE_ARGS`
- the request defaults `LLM_TEMPERATURE`, `LLM_MAX_TOKENS` and `LLM_TIMEOUT_S`

**What an evaluator policy declares and provides:**
- `needs_baselines` and `needs_simulation`, two flags
- `time_limit_s(manifest, cpus)`, its worst-case run time
- `preflight(ctx)`, a seconds-long check of the host

**Rules:**
- **Profiles are immutable once a run uses them.** To change one, copy it to a `-v2`. A resumed run refuses to continue if any of its inputs changed.
- **The resolver owns some settings.** A profile that sets one of these is rejected:
  - the LLM section
  - `prompt.template_dir` and `prompt.system_message`
  - `evaluator.*`
  - `max_iterations`, `language` and `max_code_length`
- **Every key is checked against OpenEvolve's own config classes.** OpenEvolve silently ignores unknown keys, so a typo like `populaton_size` would otherwise vanish.

## 3. What the resolver derives and checks

**Derived values, so they never drift:**

| Setting | From |
|---|---|
| `evaluator.timeout` | the policy's `time_limit_s` for this manifest and CPU count, + 60 s. R7's hand-set 120 s comes out exactly; `champsim_ipc` on `crc2-evolve-v1` with 7 CPUs gets 15,120 s |
| `evaluator.max_retries` 1, `cascade_evaluation` off | OpenEvolve retries only when `evaluate()` raises, which here means `infra_error`. The adapter has no cascade stages |
| `evaluator.parallel_evaluations` | `parallel_candidates` |
| `max_code_length` | the manifest's `max_source_bytes` + 8,192, so the evaluator itself reports an oversize candidate |
| `language` | the seed's suffix (`.h` → `cpp`), unless the spec sets it |
| `llm.models`, `api_base`, `api_key` | the model profile and the job. `api_base` is a literal, because OpenEvolve doesn't expand `${VAR}` there. The key stays `${AFAR_LLM_API_KEY}`, which OpenEvolve expands |
| `prompt.system_message`, `template_dir` | the prompt profile's files, copied to `<run>/resolved/prompt/`. The text itself goes into `system_message`, because OpenEvolve also copies it into each model's settings |

**Checks, each covered by `python -m controller.runspec --selftest`:**
- every named part exists
- template file names are real OpenEvolve templates (OpenEvolve silently falls back to its default prompt otherwise)
- the model's weights and container are on `$AFAR_ASSETS`
- the policy is registered
- the manifest is valid, with its traces present when the policy simulates
- baselines exist when the policy needs them
- the prompt's `requires` match the manifest
- `max_tokens` is less than the context window
- `parallel_candidates × cpus_per_candidate + 2` fits the job's CPUs

**Equivalence with R6 and R7.** OpenEvolve loaded each old hand-written YAML and the resolved config of its spec, and every field was compared. They differ only where intended:

| Field | Old | Resolved |
|---|---|---|
| models | `primary_model` | an explicit list |
| `template_dir` | — | set |
| `max_code_length` (R7) | 40,000 | 40,960 |
| `language` (toy) | — | explicit |

The effective system message is identical.

**On HPC3 (job 95745, `cs001`, 2026-10-25):** `champsim-compile-v1` through the generic job behaved like R7 (job 93458):

| | R7, job 93458 | `champsim-compile-v1`, job 95745 |
|---|---|---|
| Rewrites applied | 9 of 10 | 10 of 10 |
| Compiled | 6 | 6 |
| Edits outside the EVOLVE-BLOCK | 0 | 0 |
| Seconds per iteration | ~12 | 14.2 |

- All 10 stored prompts carry the profile's system message exactly.
- `run.json`, `evaluations.jsonl` and checkpoints 1–10 were written.

## 4. Running

```bash
make oe-check SPEC=champsim-compile-v1
```

`make oe-check` runs on coe-hpc1, with no GPU: the resolver's self-test, then every spec (or one, with `SPEC=`).

From `~/AFAR_HPC` on g17:

```bash
sbatch --exclude=cs002 --export=ALL,SPEC=champsim-compile-v1 slurm/openevolve.sbatch
```

To continue a run in its own directory from its newest checkpoint:

```bash
sbatch --exclude=cs002 --export=ALL,SPEC=champsim-compile-v1,RESUME=runs/champsim-compile-v1-<jobid> slurm/openevolve.sbatch
```

**The job's steps, in order:**
1. The spec check.
2. The policy's preflight on the node's compiler. For `champsim_ipc` that includes the prebuilt tree and the traces.
3. The model server from the spec's profile.
4. The resolved config.
5. `openevolve-run`, with `evaluator/openevolve_adapter.py` for policies.
6. The key-leak check.
7. `scripts/oe_run_summary.py`, which also prints the run's parts.

`ITERATIONS=N` overrides the spec's count; after a checkpoint, it counts from there. The job requests 16 CPUs and 32 GB by default; override with sbatch options.

**At the time limit:**
1. Slurm sends SIGTERM 60 s early (`--signal=B:TERM@60`), and the job passes it to OpenEvolve.
2. OpenEvolve cancels the evaluations in flight, saves `best/` and stops. Its newest checkpoint is the last completed iteration, since OpenEvolve 0.4.0 saves none on SIGTERM.
3. The job runs the key-leak check and the summary, prints `stopped; continue with RESUME=<run>` and exits 143.
4. Candidates' scratch directories live in a job-scoped `TMPDIR`, deleted however the job ends.

Jobs 95821 and 95822 tested this: a stop with 3 evaluations in flight, then a clean resume from `checkpoint_144`. To chain jobs, use `--dependency=afterany:<previous job>`, not `afterok`, which never fires after a time-limit stop.

**What a run writes in `runs/<spec>-<jobid>/`:**
- `resolved/config.yaml` and `resolved/prompt/`
- `run.json`: the parts, every input's sha256, the resolved config's sha256, the commits, and each job
- `evaluations.jsonl`: one line per evaluation, with its status, fitness, seconds and whether it was cached
- OpenEvolve's own logs and checkpoints

`python -m controller.runspec --describe runs/<run>` prints the parts of a finished run.

Evaluation records go to `$AFAR_RESULTS/evaluations/<experiment>/<policy>/<candidate id>/`. They're reused whenever the same candidate, manifest, policy and seed come back, including after a resume.

**Testing without a GPU, on coe-hpc1:**

```bash
AFAR_LLM_FAKE=1 AFAR_CXX=<Spack g++ 12> SLURM_JOB_ID=12345 SLURM_SUBMIT_DIR=$PWD SLURM_CPUS_PER_TASK=16 SPEC=<name> bash slurm/openevolve.sbatch
```

- `AFAR_LLM_FAKE_REPLY=<file>` makes the stand-in server answer with a fixed program.
- The old `slurm/openevolve_toy.sbatch` and `openevolve_champsim_compile.sbatch` are wrappers around this job, with their R6 and R7 resources, until M5.

## 5. Adding a part

| To add | Do this |
|---|---|
| A model | Copy `configs/models/qwen2.5-coder-7b-v1.env` to a new name and set its fields, then `make llm-fetch PROFILE=<name>` on coe-hpc1 |
| A prompt | Copy `configs/prompts/champsim-rewrite-v1/` and edit `system_message.txt`, or add a template override such as `full_rewrite_user.txt` |
| Search settings | Copy `configs/search/champsim-rewrite-v1.yaml` and edit it |
| An evaluator policy | Write a `Policy` subclass with its two flags, `time_limit_s` and `preflight`, and add it to `REGISTRY`. `make evaluate-selftest` then runs the contract cases on it: seed → `completed` 1.0; forbidden call and edits outside the block → `invalid_candidate`; syntax error → `compile_failed` |
| A run | Write `configs/runs/<name>.yaml` naming the parts, then run `make oe-check SPEC=<name>` |

## 6. Background

**The design** follows Hydra's config groups: pick one named option per group, then override. It follows Twelve-Factor config: experiment choices live in versioned files, while per-job values (port, key, CPUs) arrive at job start. Policies form a plugin registry behind one interface. Old entry points were migrated strangler-fig style.

**OpenEvolve 0.4.0 behaviour the resolver works around:**
- `--primary-model` discards a YAML model list, so jobs no longer pass it.
- `api_base` doesn't expand `${VAR}`.
- A missing `template_dir` silently falls back to the default prompt.
- Unknown keys are dropped without warning.
