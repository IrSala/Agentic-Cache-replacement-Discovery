# Research levers

A lever is one change to the campaign pipeline that we can turn and measure. Each lever has its own document, filed under the stage of the pipeline it changes, so what we've learned about it is in one place. These documents record what we learned; `docs/campaigns.md` stays the manual for running campaigns.

## Stages

| Stage | Folder | What it covers | Where a run spec sets it |
|---|---|---|---|
| Generation | `generation/` | the model, the prompt, sampling | `model:` and `prompt:` |
| Selection | `selection/` | OpenEvolve's database: islands, population, migration, exploration vs exploitation | `search:` |
| Evaluation | `evaluation/` | turning a candidate into scores: traces, their order, CPUs, reusing results | `evaluator:` and the experiment manifest |
| Scheduling | `scheduling/` | how stages run on the cluster: tasks per trace, overlapping generations, GPU use | `campaign:` |

A folder is created when its first lever document is added.

## When a lever gets a switch

Run specs (`configs/runs/*.yaml`, `docs/run-specs.md`) make a campaign's parts swappable, and `run.json` records every input's sha256. A lever's effect on results decides whether it gets a switch there:

| Class | Example | Switch |
|---|---|---|
| **Result-identical by construction:** the same records and scores | longest-first trace order; reusing the result of identical code | None. Verify it once with an identity check, then it's always on. A switch that never changes results is extra code and tests for nothing. |
| **Result-identical if an assumption holds** | the duplicate screen, which assumes its trace tells behaviors apart | A spec field, off by default. Validate it on recorded data first, then audit it while it runs. It can become the default once campaigns show no audit failures, but keep the switch so comparisons can turn it off. |
| **Changes results by design** | shorter or sampled traces, a different fitness, prompts, models | Always a spec field or part, never a silent default. Compare it with an A/B campaign. |

**Two rules for every lever:**
- If a lever changes what a record means, the record says so. For example, a screened candidate's summary names the behavior whose scores it reused; nothing is copied in as if it had been measured.
- Campaigns meant to be compared differ only in the lever under test.

## Levers

| Lever | Stage | Status | Switch | Class |
|---|---|---|---|---|
| [Model](generation/model.md) | generation | replayed 2026-11-15: Qwen3.6-27B against the 7B on 324 recorded prompts. After generation 1, the same compile rate and 16% more distinct programs; in generation 1, mostly copies of the seed | `model:` | changes results |
| [Longest-first trace order](evaluation/trace-order.md) | evaluation | default since 2026-10-29 | none | result-identical by construction |
| [Duplicate screen](evaluation/duplicate-screen.md) | evaluation | first HPC3 run 2026-11-13: audits caught 2 false matches (an indexing bug, fixed). Now screens on bzip2 + soplex, tested locally | `evaluator.duplicate_screen`, off by default | result-identical if its trace separates behaviors |

**Not yet moved here:**
- the OpenEvolve 0.4.0 findings in the team deck (generation and selection)
- the planned scheduling levers in `docs/campaigns.md` §8: one task per trace, overlapping generations, a GPU kept on

## Writing a lever document

Copy this outline into `<stage>/<lever>.md` and add a row to the table above.

```markdown
# <Lever name>

| | |
|---|---|
| Stage | generation, selection, evaluation or scheduling |
| Status | idea, replayed, built, validated or default (with dates) |
| Switch | the run-spec field and its default, or none |
| Class | result-identical by construction; result-identical if <assumption>; or changes results |

## Question
What the lever should improve, and how we'd know.

## How it works
Code paths and spec fields.

## Evidence
Dated entries, each with its command, campaign and Slurm job IDs.

## Costs and risks
Compute, wall time, and what could go wrong with results.

## Decision and next step

## Sources
```
