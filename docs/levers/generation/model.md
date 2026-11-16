# Model

| | |
|---|---|
| Stage | generation |
| Status | replayed 2026-11-15: Qwen3.6-27B FP8 against the pilot's Qwen2.5-Coder-7B on 324 recorded prompts. Not yet run in a campaign |
| Switch | `model:` in a run spec, naming a profile in `configs/models/` (`docs/run-specs.md`) |
| Class | changes results |

## Question

Does a bigger model give more candidates per generation that are valid, new and better?

**The measure is per generation, not per GPU-hour.** Evaluation on CPUs costs far more than generation on a GPU: in the pilot, evaluation took 46–120 minutes per generation, and the generation step about 2. So a slower model is worth it if its candidates are better, for the same evaluation budget.

The funnel, cumulative (`docs/levers/README.md`):

1. answered
2. code found
3. not identical to its parent
4. not cosmetic: the program still differs from its parent after comments and whitespace are removed
5. within `max_code_length`
6. compiles: the static checks and a syntax-only compile, the first stage of `champsim_ipc`'s build
7. unique in its batch: a campaign evaluates identical code once

Steps after these need simulation: whether a candidate behaves differently from its parent, and whether it scores better.

## How it works

- **Profiles.** A model profile (`configs/models/<name>.env`) names the checkpoint, its revision, vLLM's arguments, and the request settings. A run spec's `model:` picks one. A profile doesn't change once a run has used it; a change is a new `-v2`.
- **`qwen3.6-27b-fp8-v1`:** `Qwen/Qwen3.6-27B-FP8`, thinking off, 16,384-token context, `--max-num-seqs 32`. Temperature 0.7, `max_tokens` 4,096 and a 300 s timeout, as in the 7B's profile (`qwen2.5-coder-7b-v1`), so the two differ only in the model.
- **The replay** (`controller/llm_replay.py`; serving details in `docs/llm-serving.md`, R8):
  - It sends a campaign's recorded prompts to another profile, a generation at a time, and compares each reply with the one recorded for the same prompt.
  - One reply per prompt, as in a campaign. Campaigns don't seed requests (issue 13), so a second replay would give different replies.
  - **Paired test:** on prompts where one model's reply compiles and the other's doesn't, an exact McNemar test.
- **The check checks itself.** On the 7B's recorded replies, it reproduces every recorded rejection (324 of 324) and every compile verdict (294 of 294) of the two source campaigns, with gcc 12.2 on coe-hpc1 against the campaigns' gcc 11.5.

## Evidence

### 2026-11-15: Qwen3.6-27B against the 7B on 324 recorded prompts

- **Prompts:** `crc2-evolve-c1` (the pilot), generations 1–10, 24 each; `crc2-evolve-c2-screen-test`, generations 1–2, 42 each.
- **Jobs:** 115959 (A100, 2 batches) and 115990 (H100, 10 batches), into `$AFAR_RESULTS/llm-replay/qwen3.6-27b-fp8-v1-115959/`.
- **Check:** `make llm-replay-check R=qwen3.6-27b-fp8-v1-115959`.

| Funnel step (cumulative) | 27B | 7B |
|---|---|---|
| Replies | 324 | 324 |
| Answered, with code | 324 (100%) | 324 (100%) |
| Not the parent | 311 (96%) | 295 (91%) |
| Not cosmetic | 269 (83%) | 295 (91%) |
| Within length | 269 (83%) | 295 (91%) |
| Compiles | 221 (68%) | 252 (78%) |
| Unique in their batch | 215 (66%) | 214 (66%) |

**Paired, on compiling:** 172 both, 49 only the 27B, 80 only the 7B, 23 neither; p = 0.008. All of that difference is in generation 1:

| | Generation 1 (66 prompts; every parent is the seed) | Generations 2–10 (258 prompts) |
|---|---|---|
| Compiles: 27B / 7B | 20 / 46 (p = 7 × 10⁻⁵) | 201 / 206 (p = 0.67) |
| Unique compiling programs in their batch: 27B / 7B | 20 / 46 | **195 / 168** |
| Returned the parent unchanged: 27B / 7B | 0 / 0 | 13 / 29 |
| Returned the seed with nothing but its final newline changed: 27B / 7B | 39 / 0 | — |

| | 27B | 7B |
|---|---|---|
| Compile failures | 46 | 32 |
| … a name or API that doesn't exist (`cache`, `cycle`, `champsim::access_type`, no `CACHE*` constructor) | 20 | 16 |
| … a vector iterator compared with a pointer | 13 | 7 |
| … mixed types in `std::min` or `std::max`, such as `min(uint8_t, int)` | 12 | 0 |
| … other | 1 | 9 |
| Static-check failures (`invalid_candidate`: text outside the EVOLVE-BLOCK, or its markers) | 2 | 11 |
| Reply tokens, median | 1,072 | 1,044 |
| Replies cut off at `max_tokens`, or containing `<think>` | 0 | 0 |
| Share of the EVOLVE-BLOCK's lines changed, median, compiling replies | 86% | 43% |

**What it shows:**
- **In generation 1, the 27B mostly copies the seed:** 39 of its 66 replies, against none from the 7B.
  - The prompt shows the seed three times: as the current program, as "Attempt 1", and as the top program, all with fitness 1.0000.
  - Its task text, OpenEvolve 0.4.0's full-rewrite template, asks to keep "the same inputs and outputs as the original program, but with improved internal implementation".
  - Only 3 of the 27B's other replies were comment-only changes.
- **After generation 1, they compile equally often, and the 27B gives 16% more distinct programs** (195 against 168). The 7B repeats itself: 38 of its 206 compiling replies duplicate another in the same batch, against 6 of the 27B's 201. It also returns its parent unchanged more often, 29 times against 13.
- **The 27B rewrites more:** it changes a median 86% of the block's lines, against 43%. Whether that gives new behaviors or better scores needs simulation.
- **Its compile errors are ordinary C++ slips.** The 12 mixed-type `std::min`/`std::max` calls are the 27B's own; the 7B made none. A prompt line, or one repair attempt with gcc's error, might remove most of them. Both would be levers of their own.
- **66 prompts weren't enough.** The A100 job's 2 batches alone had the 27B ahead (54 against 52 compiling); the full 324 changed that.

**What the seed copies would cost a campaign.** `generate` rejects a reply identical to its parent (`code == parent.code`, `controller/campaign.py:349`). But the parser strips the reply, and the stored seed ends with a newline, so a copy of the seed passes (issue 18 in `docs/status.md`). The copies are identical to each other, so each batch evaluates them once (`controller/campaign.py:359`).
- **CPU:** that one evaluation is the seed's, again, about 5.1 CPU-hours on `crc2-evolve-v1`. The evaluator's cache can't reuse the seed's result, since the newline makes the code's hash differ. The duplicate screen, where it's on, would match it to the seed's behavior after its screening traces.
- **Replies:** about half of generation 1's.

## Costs and risks

- **GPU time,** measured in R8 (`docs/llm-serving.md`):

  | | 27B on an H100 | 27B on an A100 | 7B on an A100 |
  |---|---|---|---|
  | Start-up | 315 s | 255 s | 115 s |
  | A pilot batch (24 prompts) | 48–80 s | 527 s | 18–36 s |
  | Generation step, about | 6.5 min | 13–14 min | 2 min |

  This is small next to evaluation, at 46–120 minutes per generation, so GPU time isn't the constraint.
- **The timeout must never fire.** On an A100, half of a pilot batch's requests ran past 300 s; OpenEvolve would cancel and resend them, losing their replies. A campaign on A100s needs a `-v2` profile with a timeout of about 1,800 s and a longer generation job. On an H100, the slowest request took 80 s.
- **GPUs:** `gpuqs` has 3 H100s and 12 working A100s; job 115990 waited 18 minutes for an H100. Generation jobs take either.
- **Results:**
  - Generation 1 loses about half its replies to seed copies.
  - In later generations, the compile rate is the same, with more distinct programs.
  - Not yet known: their behaviors and scores.
- **Sampling:** one reply per prompt and no seed, so each pair is a single draw from each model.

## Decision and next step

- **Not decided yet.** GPU time doesn't count against the 27B (decided 2026-11-15); the open question is whether its candidates are better.
- **Before c3:**
  - **Behavioral novelty:** run the 27B's compiling, distinct candidates on bzip2, about 7 minutes each (about 25 CPU-hours for the 324 prompts' 215). Compare them with their parents' recorded results, to see whether its extra distinct programs are also distinct behaviors.
  - **Thinking mode:** the 27B ran with thinking off. A replay of the same 324 prompts with thinking on would show whether reasoning removes the compile slips and the seed copies. It needs a new profile with a larger `max_tokens`, on an H100. GPU time isn't the constraint.
- **c3:** the pilot's spec with only `model:` changed, compared with `crc2-evolve-c1` as an A/B campaign. It needs a profile whose timeout never fires on an A100, or generation jobs on H100s only. The seed copies would come with the model; a prompt that avoids them would be a separate lever, compared on its own.

## Sources

- `docs/llm-serving.md`, R8: the serving measurements and how to run a replay
- `controller/llm_replay.py`, `slurm/llm_replay.sbatch`, `make llm-replay-check`, `make llm-replay-selftest`
- `$AFAR_RESULTS/llm-replay/qwen3.6-27b-fp8-v1-115959/check.json`: every reply's funnel flags, and gcc's first error for each compile failure
- `configs/models/qwen3.6-27b-fp8-v1.env`, `configs/models/qwen2.5-coder-7b-v1.env`
