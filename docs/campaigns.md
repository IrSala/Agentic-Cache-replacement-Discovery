# Staged campaigns (M5, M6)

A campaign runs evolution as a chain of short Slurm jobs instead of one long GPU job:
- **Generation:** a GPU job writes a whole generation of candidates.
- **Evaluation:** a CPU array scores them on many nodes at once.
- **Aggregation:** a small job adds the scores to OpenEvolve's database and starts the next generation.

Why stage it at all? In M4's single-job runs, evaluation took about 7× the LLM's time (95.8 s against 13.2 s per iteration), so the GPU sat idle. Splitting the stages lets evaluation spread over CPU nodes, while the GPU is held only for the minutes it's needed. That trade depends on how much LLM work a generation needs: with today's 7B model answering once per candidate, it's about 1% of a generation. A larger model or LLM coding practices would raise it, and past about a quarter, keeping one GPU on would be the better design (section 8).

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

On g17, where jobs are submitted, with your address in the environment so a halt can mail you (section 5):

```bash
export AFAR_MAIL_USER=<you>@sjsu.edu
```
```bash
make campaign-start SPEC=m5-dev-v1
```

Generation jobs take whichever A100 or H100 frees first, and never a P100 node (`g1`–`g15`), which vLLM can't use. `EXCLUDE=cs002` is the default, which keeps them off that node's faulty GPU. `NAME=<id>` sets the campaign ID; otherwise it's `<spec>-<date>-<time>`.

To check on it, from either cluster:

```bash
make campaign-status C=<id>
```

It shows the state, each generation's results, the heartbeat, the time and CPU- and GPU-hours used against the limits, and, when the stage's job is still queued, Slurm's reason and how long it had waited at the last heartbeat.

To halt it after the current stage (this writes a `STOP` file):

```bash
make campaign-stop C=<id>
```

To halt it now, cancelling the current stage's jobs (on g17):

```bash
make campaign-stop C=<id> NOW=1
```

To resubmit the current stage after a halt or a cancelled job, on g17:

```bash
make campaign-resume C=<id>
```

**Testing on coe-hpc1.** `make campaign-selftest` runs the whole pipeline with the fake LLM, a local executor and injected faults, including the watchdog and every stop condition (section 7).

## 2. The stages

```
start ──► evaluate(0) = the seed ──► aggregate(0) ──► generate(1) ──► evaluate(1) ──► aggregate(1) ──► generate(2) ...
                                                       GPU job         CPU array        CPU job
                                                                       afterany ▲
```

| Stage | Job | What it does |
|---|---|---|
| `generate(g)` | `slurm/campaign_generate.sbatch`: 1 A100 or H100 (`--gres=gpu:1`, P100 nodes excluded), 4 CPUs, 24 GB, 30 min | Starts the model server from the spec's model profile and loads the newest checkpoint. It samples one parent per candidate, round-robin over the islands, and builds each prompt as OpenEvolve's worker does. It sends all the prompts at once, since vLLM batches them. Each reply is parsed as a full rewrite. A reply with no code, an exact copy of its parent, or code over `max_code_length` is recorded as rejected and never evaluated. Then it submits the evaluation and the aggregation |
| `evaluate(g)` | `slurm/campaign_evaluate.sbatch`: an array, one task per unique candidate; `cpus_per_candidate` CPUs, 1 GB each (at least 2 GB); the policy's worst-case time + 10 min | `evaluate_candidate` with the spec's policy: M2's records, statuses and cache. `champsim_ipc` runs a candidate's traces longest first, so fewer CPUs than traces can finish as soon (section 8). Exits 0 for any candidate outcome and 1 for an infrastructure failure |
| `aggregate(g)` | `slurm/campaign_aggregate.sbatch`: 1 CPU, 4 GB, 20 min, `afterany` on the array | Retries missing evaluations, or adds every program in iteration order, as OpenEvolve's controller does. It saves the checkpoint and the generation's `summary.json`, then submits `generate(g+1)`, finishes or halts |

Jobs submit the next stage themselves; compute nodes can run `sbatch`, as M0 showed. The generation job checks that with `sbatch --test-only` before starting the server. It tries up to 4 times, 10, 30 and 60 s apart, and logs each error; if every try fails, it exits 1, an ordinary failure, since a busy controller is the likelier cause than the node (`docs/status.md` issue 16). The GPU it ran on is recorded in `generation.json` and the `generated` event, so LLM times can be compared by GPU.

**Two differences from `openevolve-run`:**
- **Shared parent pool:** all candidates of a generation are sampled from the same database state. Fewer, larger generations mean fewer GPU start-ups, but the database is updated less often.
- **Sampling seed:** each generation reseeds sampling with the configured seed plus the generation number, so it's reproducible without repeating itself.

As in M4, edits outside the EVOLVE-BLOCK are rejected by the evaluator, not reverted. Duplicate candidates within a generation share one evaluation, but each still becomes a program, as it would in `openevolve-run`.

## 3. What a campaign leaves on disk

Each campaign lives in `$AFAR_RESULTS/campaigns/<id>/`, on `/data`, because `/home` is nearly full.

| Path | Holds |
|---|---|
| `campaign.json` | the run record: the spec, its parts, every input's sha256, commits, executor, excluded nodes |
| `status.json` | `state` (`running`, `finished` or `halted`), the generation, the stage, live job IDs, the attempt, and the reason for a halt or an early finish. Since M6 also `stage_job`, `handler_job` and `stage_attempt` (section 5) |
| `events.jsonl` | one line per submission, generation, retry, aggregation, stage failure, stall, halt, mail or resume |
| `heartbeat.json` | the heartbeat's last run, what it did, and its successor's job (M6) |
| `budget.json` | CPU- and GPU-hours used so far, from `sacct`, updated each generation (M6) |
| `resolved/` | the OpenEvolve config and the prompt snapshot |
| `checkpoints/checkpoint_<iteration>/` | standard OpenEvolve checkpoints, one per generation. `openevolve-run --checkpoint` continues from them, so a campaign can be finished in single-job mode |
| `generations/<g>/` | `candidates/<id>.h`; `generation.json` (parent, islands, prompt, reply, tokens or the rejection, per iteration); `tasks.json`; `results/<id>.json` and `<id>.infra-<attempt>.json`; `attempts.json`; `summary.json` |
| `logs/` | every stage job's Slurm output |

## 4. Failures

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
| A generation or aggregation job fails, times out or is cancelled | Its handler restarts it, up to `max_stage_retries` times. A generation job is kept off its node only if the node looks bad (section 5). Then the campaign halts with `halted: stage_failed` |
| A stage job and its handler both vanish, or a handler fails | The heartbeat restarts the stage within about an hour, or halts with `halted: stalled` |
| A limit is reached | The campaign finishes or halts with the limit's reason (section 6) |
| `make campaign-stop` | The next stage boundary halts with `halted: stopped`; `resume` clears the `STOP` file and continues. With `NOW=1`, it cancels the stage's jobs and halts at once |
| Any other halt | `make campaign-resume` resubmits the current stage once the cause is fixed. It refuses while that stage's job is still queued or running; an evaluation array that is still running is kept |

**Fault injection, for tests only.** A `faults.json` in the campaign directory, for example:

```json
{"infra": [{"generation": 1, "index": 3, "attempts": [1]}], "aggregate_crash": [2],
 "generate_fail": [{"generation": 2, "attempts": [1]}]}
```

That makes:
- task 3 of generation 1 fail its first attempt
- every aggregation of generation 2 crash before saving; `{"generation": 2, "attempts": [1]}` makes only the first crash
- the first generation job of generation 2 fail before it starts the model server; `"exit": 3` makes it fail as a node check does, so its restart avoids the node
- `"generate_hold": [2]` (not in the example) submits generation 2's job held, standing in for a long wait for a GPU, until you run `scontrol release <job>`

`"attempts": "all"` makes a failure persistent. For `infra`, attempts count a task's evaluations; for the others, a stage job's restarts (`stage_attempt`).

## 5. The watchdog and mail (M6)

**Failure handlers.** Every generation and aggregation job is submitted with a handler, `slurm/campaign_handle.sbatch`. The handler waits with `--dependency=afternotok:<job> --kill-on-invalid-dep=yes`:
- if the job succeeds, Slurm removes the handler, unrun
- if the job fails, times out, runs out of memory or loses its node, the handler runs. It reads how the job ended from `sacct` and logs a `stage_failed` event. Then it restarts the job (attempt 2, 3, …) or, past `max_stage_retries`, halts the campaign with `stage_failed`, naming the job, its Slurm state, exit code, node and log

A generation job's restart is kept off its node only on evidence the node is bad: the job exited 3, which its script does when the GPU preflight fails (an unusable GPU, or one below compute capability 8.0) or the model server won't start; or Slurm reports `NODE_FAIL` or `BOOT_FAIL`. After any other failure, including a submit check that failed 4 times, the restart may run anywhere. Excluding a good node can leave no free GPU: in the M6 tests, a restart that excluded `cs001` after an injected failure was given an estimated start 1.5 days away, while `cs001` was the only node with free A100s.

Evaluation arrays need no handler: their aggregation already waits with `afterany` and retries missing tasks.

**The heartbeat** is a backstop for when recovery itself fails: a handler that fails or never runs, or anything else that leaves the campaign `running` with no live job (`docs/status.md`, issue 12). One heartbeat chain runs per campaign, `slurm/campaign_heartbeat.sbatch`, started by `campaign-start` and by `campaign-resume`. Each run:
1. ends the chain if the campaign has finished or halted
2. submits its successor first, `heartbeat_minutes` later, so a check that fails can't end the chain
3. if neither the stage's job nor its handler is queued or running, and `status.json` hasn't changed for 10 min, restarts the stage, or halts with `stalled` past `max_stage_retries`
4. if the stage's job is still queued, records Slurm's reason and how long it has waited in `heartbeat.json`, which `campaign-status` shows. `campaign-status` itself reads only files, so it works from either cluster (on coe-hpc1, `squeue` takes 18 s to fail)

It checks no limits; aggregation does that once per generation.

**Shared-compute safeguards:**
- **One at a time:** each campaign's heartbeats share one job name, `afar-heartbeat-<id>`, with `--dependency=singleton`, so Slurm never runs two at once. A heartbeat that finds another one already due ends without a successor.
- **An interval floor:** `heartbeat_minutes` is at least 60 (10 only in a spec with `test: true`).
- **Small footprint:** 1 CPU for 4–6 s per run (measured in M6 part B), about 24 jobs and 2 CPU-minutes a day, and two `squeue` calls an hour. Between runs, one pending job holds no resources.
- **Easy removal:** `scancel --name=afar-heartbeat-<id>`.

These follow the guidance found when M6 was planned: SJSU has no rule on periodic jobs; Cirrus sets a 60 s floor on scheduler polling; NERSC restarts its workflow processes hourly through `scrontab`, which HPC3 has disabled.

**Only the named job acts.** `status.json` names the current stage's job (`stage_job`) and its handler (`handler_job`), and `heartbeat.json` the next heartbeat. Any other job exits without acting, after waiting up to 60 s in case its submitter hasn't recorded it yet. So a duplicate job does nothing: one left by an `sbatch` that timed out after Slurm took the job, or by a stage that crashed after submitting its successor. Every Slurm command is retried after 10, 30 and 60 s on errors a busy controller gives, such as a socket timeout. A rejected request is not retried.

**Mail on halt.** HPC3's compute nodes can't send mail: in the delivery test (job 96270), `homacs1` had no mail program at all, but Slurm's own message arrived. So a halt has Slurm send it. The campaign submits a 1-second job named `AFAR_campaign_<id>_halted_<reason>` with `--mail-type=END`, and its end mails `$AFAR_MAIL_USER`. The mail arrives once that job has run, normally within a minute, later if `cpuqs` is full. Its subject reads `Slurm Job_id=<n> Name=AFAR_campaign_<id>_halted_<reason> Ended, …`. It has no body, so the details are in `make campaign-status C=<id>`.
- **No mail for asked-for ends:** a halt you asked for (`stopped`) doesn't mail, and neither does a planned finish.
- **The address** comes from the environment of the shell that ran `campaign-start` or `campaign-resume`, and jobs pass it on to the jobs they submit. Only `mail: true/false` is recorded.
- **Mail can't block a halt:** a mail that fails becomes a `mail_failed` event and never stops the halt.
- **Watchdog failures mail too:** handler and heartbeat jobs get Slurm's `--mail-type=FAIL`.

To test delivery again (on g17, with `AFAR_MAIL_USER` set):

```bash
make mail-test
```

It tries both ways: `/bin/mail` from a CPU node, and Slurm's message when the job ends.

## 6. Limits and stopping (M6)

All are optional keys in the spec's `campaign:` section.

| Key | Default | Outcome |
|---|---|---|
| `generations` | required | `finished` |
| `target_fitness` | none | `finished` (`target`), once the best `combined_score` reaches it |
| `max_wall_hours` | none | `finished` (`wall_time`), counted from the start |
| `max_cpu_hours`, `max_gpu_hours` | none | `halted: budget` |
| `max_failed_generations` | 3 | `halted: no_progress`, after that many generations in a row with no `completed` candidate |
| `min_free_gb` | 200 | `halted: storage`, when the campaign's filesystem has less free |
| `max_stage_retries` | 2 | `halted: stage_failed` or `stalled` (section 5) |
| `heartbeat_minutes` | 60 | at least 60, or 10 with `test: true` |
| `test` | false | allows test-only settings |

Aggregation checks the limits after each generation, so one can be overshot by up to one generation. The wall time is checked again when a generation job starts: a job that waited in the queue past `max_wall_hours` ends the campaign as `finished (wall_time)` without starting the model (`docs/status.md` issue 15). So the wall time can be overshot by a queue wait, but not by another generation. CPU- and GPU-hours come from `sacct` over every job in `events.jsonl`, array tasks included; heartbeats aren't counted. A budget halt can't be resumed, because the limits are part of the spec: to go further, start a new campaign from its last checkpoint.

## 7. Verified

**On coe-hpc1:** `make campaign-selftest` runs `controller.oe_adapter --selftest` and `controller.campaign --selftest`. The campaign uses `crc2-tiny-v1`, the fake LLM cycling through four replies, and the local executor. It checks that:
- 3 generations finish
- a task that fails once is retried
- a persistent failure halts, with a message naming the task
- a crashed aggregation resumes without adding any program twice
- `STOP` halts the campaign and `resume` continues it
- duplicates are evaluated once
- `openevolve-run` continues from the last checkpoint

M6 added these checks:
- a generation job that fails is restarted by its handler; it's kept off its node only after a node check fails (exit 3)
- an aggregation that crashes on every attempt is restarted twice, then halts with `stage_failed` and mails; the address appears in no campaign file
- a halt's mail is a Slurm job with the right name, `--mail-type=END` and address, recorded as a `mailed` event
- a persistent infrastructure failure mails; a `STOP` doesn't
- a chain whose stage job and handler vanish is restarted by the heartbeat
- heartbeats carry one name, `singleton`, an hourly `--begin` and FAIL mail; a heartbeat of an ended campaign, or one that finds another already due, submits no successor
- through a stand-in `sbatch`: an `sbatch` that times out twice is retried and gives the right jobs; a rejected one isn't retried; the handler's and heartbeat's Slurm flags are right
- each stop condition ends the campaign with its reason: target, wall time, budget (which can't be resumed), storage, no progress
- `stop --now` halts at once, with no retry and no mail

The fixes after the pilot (`docs/status.md` issues 14–16) added these:
- generation jobs ask for `--gres=gpu:1`, exclude the P100 nodes as well as the campaign's own and any a handler excluded, and carry `--hold` when a test asks
- a pending job's Slurm reason and wait are read from `squeue`, recorded by the heartbeat, and shown by `campaign-status`
- a generation job held until the campaign is past `max_wall_hours` doesn't ask for the model when it starts; it ends the campaign as `finished (wall_time)`, and generates and submits nothing
- the real `slurm/campaign_generate.sbatch`, with a stand-in `sbatch`: a submit check that fails twice is retried and the job goes on; one that fails 4 times exits 1, with every error in the log

The efficiency work (section 8) added these:
- `run_order` puts traces longest first by baseline run time, and keeps the manifest's order without baselines
- a candidate evaluated on 2 CPUs and on 4 gets identical cycles and fitness
- `campaign-report` covers every generation, evaluation and trace of a campaign; `campaign-estimate` runs on a spec and warns when `max_cpu_hours` would end it first

`controller.runspec --selftest` checks the new keys, including that a heartbeat under the floor is refused.

**The M6 job scripts** ran on coe-hpc1 against a throwaway campaign, with stand-ins for `sbatch`, `squeue` and `sacct`. The heartbeat submitted its successor. The handler read a failed aggregation from `sacct`, then resubmitted it, with a new handler, as attempt 2.

**The real stage scripts** ran one generation on coe-hpc1 with a stand-in `sbatch` that recorded each command.

**On HPC3, after the pilot (2026-10-29),** `fix-dev-a` (spec `m6-dev-v2`) tested the fixes for issues 14–16:
- **An H100:** with every A100 node excluded, generation 1 ran on `g18`'s H100, and `generation.json` recorded the GPU.
- **The wall time:** generation 2's job was submitted held. The heartbeat recorded the hold, and `campaign-status` showed it. Released past the limit, the job ended the campaign as `finished (wall_time)` without starting the model.

**On HPC3, M6 part C (2026-10-26 to 2026-10-28),** the 48-hour pilot `crc2-evolve-c1` ran unattended on `crc2-evolve-v1`. It ended on its wall-time limit, at 48.1 h, after 11 generations.
- **Restarts:** handlers restarted three failed generation jobs, and nothing was resubmitted by hand. One was the injected failure at generation 2. The other two were failed submit checks at generation 10 (`docs/status.md` issue 16).
- **A long wait:** one generation job waited 35.4 h for an A100 (issue 14). The heartbeat counted it as alive, as designed.

Job IDs and full results are in `docs/status.md`.

**On HPC3, M6 part B (2026-10-26),** `m6-dev-v1` passed all four of M6's short tests. Job IDs and full results are in `docs/status.md`.

| Test | Campaign | What happened |
|---|---|---|
| A failed generation job is restarted | `m6-dev-e` | The injected failure (exit 2) on `cs001` restarted on the same node, and both generations finished. The first try, `m6-dev-a`, excluded `cs001` and stranded the restart (section 5) |
| A cancelled stage is restarted | `m6-dev-b`, `m6-dev-f` | A cancelled aggregation was restarted by its handler within seconds, so Slurm treats a cancel as a failure. With its handler cancelled too, the heartbeat restarted it 18 min after the stall |
| A persistent crash halts and mails | `m6-dev-c` | Three attempts, then `halted: stage_failed`; the mail arrived |
| `campaign-stop NOW=1` | `m6-dev-d` | Halted 5 s later, with no retry and no mail |

**Reaction times:** a handler restarts a failed job about 10 s after it ends. The heartbeat restarts a stall at its first run after the status has been unchanged for 10 min: 10–20 min after the stall with a test spec's 10-min heartbeat, and 10–70 min with the hourly one. Every heartbeat chain ended at its next run after its campaign ended.

**On HPC3, M5 (2026-10-26),** `m5-dev-v1` passed M5's four acceptance tests. Job IDs and full results are in `docs/status.md`.

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

## 8. Cost and efficiency

**Measuring a campaign.** `make campaign-report C=<id>` reads only the campaign's files, so it works from either cluster.
- **Per generation:** the generation job's queue wait and run, the LLM's seconds and GPU, and when half, three quarters and all of the evaluation tasks were done, with the tail after half.
- **For the campaign:**
  - the CPU-hours the evaluation tasks held (CPUs × each task's evaluation time) and spent simulating (the trace records' `run_seconds`)
  - their ratio: the efficiency that `seff` and Princeton's jobstats report, which flags jobs below 65–80%
  - CPU-hours per evaluation, and each trace's run times
- **Cached evaluations** (a candidate scored before) cost nothing and are counted apart.
- **`SACCT=1`, on g17,** adds Slurm's own `TotalCPU` and peak memory for the evaluation tasks. Cores may be shared, since every partition has `OverSubscribe=FORCE:4`, and Slurm's CPU time would show it.

**Estimating a spec before it runs.** `make campaign-estimate SPEC=<name>` replays a candidate's traces on `cpus_per_candidate` CPUs, in the order the evaluator runs them, using the LRU baseline's run times. It scales the result to a generation and to `max_wall_hours`, and warns when `max_cpu_hours` would halt the campaign first.
- **On the pilot's spec, it errs high where it should.** It estimates 56 min per generation, against the pilot's median of about 2 min for the generation job plus 54 min of evaluation. It estimates 239 CPU-hours per generation, against 144–213 measured, since duplicates and the cache made real generations cheaper.
- **Use it to set `max_cpu_hours`.** The pilot's cap of 5,000 came from an estimate of 3,000; 48 hours without the GPU wait would have needed about twice that.

**Where the pilot's compute went** (`make campaign-report C=crc2-evolve-c1`):

| Part | Cost | Waste |
|---|---|---|
| Generation jobs | 0.32 GPU-hours of running over 11 generations: vLLM starts in 95–115 s, the LLM takes 18–36 s | The 35.4-hour wait for an A100 (issue 14, fixed) |
| Evaluation tasks: 24 × 14 CPUs | 1,399.5 CPU-hours held; 608.3 spent simulating | **Efficiency 44%.** A task holds its 14 CPUs until its slowest trace ends. mcf takes a median 2,639 s; the fastest six traces take 553–848 s |
| The generation barrier | the next generation waits for the last task | A median of 12 min per generation after half the tasks were done (up to 67). This costs wall time, not CPU-hours |
| Builds, aggregation, the heartbeat | about 12 s per task; seconds; about 2 CPU-min a day | negligible |

**Traces run longest first (since 2026-10-29).**
- **How:** the `champsim_ipc` policy starts a candidate's traces in order of their LRU baseline's run time, longest first (`run_order` in `evaluator/policies/champsim_ipc.py`). It returns their records in the manifest's order, so a candidate's outcome doesn't depend on the order.
- **Why it works:** this is longest-processing-time-first (LPT) scheduling. On k CPUs, the long traces start at once and the short ones fill the gaps, so a candidate on fewer CPUs than traces can still finish when its slowest trace does.
- **The replay:** the pilot's 125 completed candidates, each with its own measured trace times:

| CPUs per candidate | CPU-hours held | Wall time vs 14 CPUs |
|---|---|---|
| 14 | 1,363 | — |
| 8 | 779 (−43%) | the same for every candidate |
| 7 | 696 | the same at the median, 1.13× at worst |
| 1 per trace (one task each) | 580 (−57%) | the same |

**`crc2-evolve-c2`** applies this:
- 8 CPUs per candidate and 42 candidates per generation, at the pilot's footprint of about 336 CPUs
- the same estimated cost per generation as the pilot's spec, for 1.75× the candidates
- its search profile keeps `population_size: 50`, for comparability with the pilot. With 42 new programs per generation the population turns over faster, and a larger population is a lever to try

**What comes next, and when** (decided 2026-10-29, from the measurements above):
1. **One 1-CPU task per (candidate, trace).**
   - **How:** an array over the pairs, longest first. Each task runs `evaluate(..., trace=)`, as `slurm/calibrate_array.sbatch` does, and aggregation scores the 14 records.
   - **Do it** if `campaign-report` shows efficiency under 80% with 8 CPUs, or if tasks start waiting in the queue: 1-CPU tasks with 2.5-hour limits fit backfill gaps more easily.
   - **Why an array, not packing:** these tasks run 9–75 min, vary in length and are retried one by one. That's what arrays are for; packing many tasks into one job (GNU parallel, pilot jobs) suits short tasks.
2. **Overlapping generations.**
   - **How:** generation g+1's job starts when about 75% of generation g is scored, and the rest join the database when they finish. This is the queue-like behavior of OpenEvolve's own parallel controller, AlphaEvolve and FunSearch, while the GPU is still held for about 2 min per generation.
   - **Do it** if the tail is at least 20% of a generation's wall time; it was a median of about 25% in the pilot.
   - **Watch:** asynchronous search favors candidates that evaluate faster (evaluation-time bias). Here those are mostly candidates with fewer cycles, so the bias runs with the fitness.
3. **A GPU that stays on**, as a pilot-job service with a work queue: only if LLM work would keep it at least 25% busy, for example with a larger model, several samples per candidate or an LLM critic. Today it's about 1%, but that reflects the current model used the simplest way: Qwen2.5-Coder-7B (7.6B parameters), one reply of about 1,100 tokens per candidate, with no reasoning and no second try. A larger model (a 32B one does about 4× the work per token and needs an 80 GB H100) or LLM coding practices would raise it: a compile-and-repair loop for rewrites that don't build, best-of-n replies, reasoning before code, or a fast and a strong model together, as AlphaEvolve does. In the pilot, 40% of the 240 rewrites added nothing new (28 copies of their parent, 30 duplicates, 37 that failed to build or run). Each practice is a search lever, to be compared by results per GPU-hour; self-repair often gains less once its cost is counted. Several centers cancel jobs that leave a GPU idle for 1–2 hours (UF, Yale, Princeton).
4. **Not planned:** cascade or multi-fidelity evaluation, which changes the fitness, so it's a research lever; and the `nsfq*` H100s, which are a policy question (`docs/sjsu-hpc-workflow.md` §2.3).

**Sources:**
- [Princeton's jobstats](https://github.com/PrincetonUniversity/jobstats/)
- [UF's idle-GPU policy](https://docs.rc.ufl.edu/scheduler/idle_gpu_policy/)
- [Yale's Job Defense](https://docs.ycrc.yale.edu/clusters-at-yale/job-scheduling/job_defense/)
- [Slurm's scheduling guide (backfill)](https://slurm.schedmd.com/sched_config.html)
- [PSI on arrays vs packed jobs](https://gitea.psi.ch/HPCE/gitea-pages/commit/ce72b9e9c2b4ebf2910c4addc02589adfb982e5a)
- [Graham's LPT bound](https://community.wvu.edu/~krsubramani/courses/sp14/approx/auxslinotes/lpt4:3.pdf)
- [Karns and Desell on evaluation-time bias (GECCO 2025)](https://www.se.rit.edu/~travis/papers/2025_gecco_time_bias.pdf)
- [Parsl's overview (pilot jobs)](https://parsl.readthedocs.io/en/latest/userguide/overview.html)
- [The Qwen2.5-Coder technical report (model sizes)](https://arxiv.org/abs/2409.12186)
- [Chen et al., Teaching Large Language Models to Self-Debug](https://arxiv.org/abs/2304.05128)
- [Olausson et al., Is Self-Repair a Silver Bullet for Code Generation?](https://arxiv.org/abs/2306.09896)
