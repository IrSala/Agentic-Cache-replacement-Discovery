# Staged campaigns (M5)

A campaign runs evolution as a chain of short Slurm jobs instead of one long GPU job:
- **Generation:** a GPU job writes a whole generation of candidates.
- **Evaluation:** a CPU array scores them on many nodes at once.
- **Aggregation:** a small job adds the scores to OpenEvolve's database and starts the next generation.

Why stage it at all? In M4's single-job runs, evaluation took about 7× the LLM's time (95.8 s against 13.2 s per iteration), so the GPU sat idle. Splitting the stages lets evaluation spread over CPU nodes, while the GPU is held only for the minutes it's needed.

## 1. Starting one

A campaign is a run spec (`docs/run-specs.md`) with a `campaign:` section. The model, prompt, search and evaluator are chosen exactly as for a single-job run.

```yaml
# configs/runs/m5-dev-v1.yaml (excerpt)
evaluator: {policy: champsim_ipc, experiment: crc2-dev-v1, cpus_per_candidate: 4}
campaign:
  generations: 3
  candidates_per_generation: 24   # spread round-robin over the search profile's islands
  max_concurrent_tasks: 40        # the evaluation array's %K
  max_task_retries: 2             # for infrastructure failures only
```

`iterations` is derived as `generations × candidates_per_generation`, so iteration numbers match what `openevolve-run` would use.

**Commands.** Check the spec on coe-hpc1:

```bash
make oe-check SPEC=m5-dev-v1
```

On g17, where jobs are submitted:

```bash
make campaign-start SPEC=m5-dev-v1
```

`EXCLUDE=cs002` is the default, which keeps generation jobs off that node's faulty GPU. `NAME=<id>` sets the campaign ID; otherwise it's `<spec>-<date>-<time>`.

To check on it, from either cluster:

```bash
make campaign-status C=<id>
```

To halt it after the current stage (this writes a `STOP` file):

```bash
make campaign-stop C=<id>
```

To resubmit the current stage after a halt or a cancelled job, on g17:

```bash
make campaign-resume C=<id>
```

**Testing on coe-hpc1.** `make campaign-selftest` runs the whole pipeline with the fake LLM, a local executor and injected faults, in about 2 minutes.

## 2. The stages

```
start ──► evaluate(0) = the seed ──► aggregate(0) ──► generate(1) ──► evaluate(1) ──► aggregate(1) ──► generate(2) ...
                                                       GPU job         CPU array        CPU job
                                                                       afterany ▲
```

| Stage | Job | What it does |
|---|---|---|
| `generate(g)` | `slurm/campaign_generate.sbatch`: 1 A100, 4 CPUs, 24 GB, 30 min | Starts the model server from the spec's model profile and loads the newest checkpoint. It samples one parent per candidate, round-robin over the islands, and builds each prompt as OpenEvolve's worker does. It sends all the prompts at once, since vLLM batches them. Each reply is parsed as a full rewrite. A reply with no code, an exact copy of its parent, or code over `max_code_length` is recorded as rejected and never evaluated. Then it submits the evaluation and the aggregation |
| `evaluate(g)` | `slurm/campaign_evaluate.sbatch`: an array, one task per unique candidate; `cpus_per_candidate` CPUs, 1 GB each (at least 2 GB); the policy's worst-case time + 10 min | `evaluate_candidate` with the spec's policy: M2's records, statuses and cache. Exits 0 for any candidate outcome and 1 for an infrastructure failure |
| `aggregate(g)` | `slurm/campaign_aggregate.sbatch`: 1 CPU, 4 GB, 20 min, `afterany` on the array | Retries missing evaluations, or adds every program in iteration order, as OpenEvolve's controller does. It saves the checkpoint and the generation's `summary.json`, then submits `generate(g+1)`, finishes or halts |

Jobs submit the next stage themselves; compute nodes can run `sbatch`, as M0 showed. The generation job checks that with `sbatch --test-only` before starting the server.

**Two differences from `openevolve-run`:**
- **Shared parent pool:** all candidates of a generation are sampled from the same database state. Fewer, larger generations mean fewer GPU start-ups, but the database is updated less often.
- **Sampling seed:** each generation reseeds sampling with the configured seed plus the generation number, so it's reproducible without repeating itself.

As in M4, edits outside the EVOLVE-BLOCK are rejected by the evaluator, not reverted. Duplicate candidates within a generation share one evaluation, but each still becomes a program, as it would in `openevolve-run`.

## 3. What a campaign leaves on disk

Each campaign lives in `$AFAR_RESULTS/campaigns/<id>/`, on `/data`, because `/home` is nearly full.

| Path | Holds |
|---|---|
| `campaign.json` | the run record: the spec, its parts, every input's sha256, commits, executor, excluded nodes |
| `status.json` | `state` (`running`, `finished` or `halted`), the generation, the stage, live job IDs, the attempt, and the reason for a halt |
| `events.jsonl` | one line per submission, generation, retry, aggregation, halt or resume |
| `resolved/` | the OpenEvolve config and the prompt snapshot |
| `checkpoints/checkpoint_<iteration>/` | standard OpenEvolve checkpoints, one per generation. `openevolve-run --checkpoint` continues from them, so a campaign can be finished in single-job mode |
| `generations/<g>/` | `candidates/<id>.h`; `generation.json` (parent, islands, prompt, reply, tokens or the rejection, per iteration); `tasks.json`; `results/<id>.json` and `<id>.infra-<attempt>.json`; `attempts.json`; `summary.json` |
| `logs/` | every stage job's Slurm output |

## 4. Failures (M5)

Every stage is idempotent, so running it twice is safe. That matters because Slurm requeues jobs after a node failure (`JobRequeue=1`), and a resume may repeat a stage:
- **Atomic writes:** files are written to a temporary name, then renamed. A checkpoint becomes visible only when complete.
- **Generation:** writes its candidates before the `generation.json` that marks it done.
- **Evaluation:** a task skips a candidate that already has a result.
- **Aggregation:** if the checkpoint already exists, it doesn't add the programs again.

| Failure | What happens |
|---|---|
| A candidate is invalid, doesn't compile, crashes or times out | Scored 0 and recorded; the campaign goes on |
| An evaluation task fails on infrastructure, is cancelled or is killed | `aggregate` resubmits just those tasks, up to `max_task_retries` times, with `--exclude=` the nodes named in their infrastructure markers. A task killed by `scancel` or a node failure writes no marker, so its retry may run on any node |
| It still fails after that | The campaign halts with `halted: infra`. The message names each task, its last error and node, and the log directory |
| A stage job is cancelled, crashes or never runs | `status.json` still points at that stage. `make campaign-resume` resubmits it. It refuses while the stage's jobs are still queued or running. Until M6, nothing notices such a stage, so the campaign waits for that resume |
| `make campaign-stop` | The next stage boundary halts with `halted: stopped`; `resume` clears the `STOP` file and continues |

**Coming in M6:** a failed stage restarting itself, through `afternotok` handlers and an hourly heartbeat job; mail; and stop conditions beyond the generation count.

**Fault injection, for tests only.** A `faults.json` in the campaign directory, for example:

```json
{"infra": [{"generation": 1, "index": 3, "attempts": [1]}], "aggregate_crash": [2]}
```

That makes task 3 of generation 1 fail its first attempt, and makes the aggregation of generation 2 crash before saving. `"attempts": "all"` makes a failure persistent.

## 5. Verified

**On coe-hpc1:** `make campaign-selftest` runs `controller.oe_adapter --selftest` and `controller.campaign --selftest`. The campaign uses `crc2-tiny-v1`, the fake LLM cycling through four replies, and the local executor. It checks that:
- 3 generations finish
- a task that fails once is retried
- a persistent failure halts, with a message naming the task
- a crashed aggregation resumes without adding any program twice
- `STOP` halts the campaign and `resume` continues it
- duplicates are evaluated once
- `openevolve-run` continues from the last checkpoint

**The real stage scripts** ran one generation on coe-hpc1 with a stand-in `sbatch` that recorded each command.

**On HPC3 (2026-10-26),** `m5-dev-v1` passed all four acceptance tests. Job IDs and full results are in `docs/status.md`.

| Test | Campaign | What happened |
|---|---|---|
| 3 generations, unattended | `m5-dev-a` | Finished in 27 min with no intervention: 72 iterations, best `combined_score` 1.2011 |
| A killed task is retried | `m5-dev-b`, generation 2 | Task 19 was cancelled mid-run. Aggregation resubmitted just that task (attempt 2), it completed, and the campaign went on |
| A cancelled stage resumes | `m5-dev-b`, generation 3 | The aggregation job was cancelled while it waited for the array. `resume` refused while the array still ran; afterwards it resubmitted the aggregation, which finished the campaign. No iteration appears twice in the final checkpoint |
| A persistent failure halts | `m5-dev-c`, with `faults.json` | Task 0 of generation 1 failed on `c20`, then on `c2` (with `c20` excluded), then on `c6`. The campaign halted with `halted: infra` and a message naming the task, its error, its node and the logs |

**Stage timings on `m5-dev-v1`:**
- **One generation:** 8.6–9.4 min in all.
- **Generation job:** 1.7–1.9 min of GPU time. The server is healthy after 70–75 s, and the 24 batched replies take 17–27 s.
- **Evaluation plus aggregation:** 6.9–7.7 min.
- **Whole campaign:** 22 s per iteration, against 34 s in M4's single job, and the GPU was held for about 5.5 min of the 27.

A checkpoint can hold fewer programs than were added. When a program takes over a MAP-Elites cell, OpenEvolve 0.4.0 deletes the program it displaced unless that one is the best overall (`_remove_program_if_orphaned`). It also caps the population at `population_size` (50 here). The same happens in `openevolve-run`.
