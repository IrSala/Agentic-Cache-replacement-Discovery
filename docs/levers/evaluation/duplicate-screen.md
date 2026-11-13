# Duplicate screen

| | |
|---|---|
| Stage | evaluation |
| Status | replayed on the pilot, then built and tested locally (2026-11-13); not yet run on HPC3 |
| Switch | `evaluator.duplicate_screen` in a run spec, off by default |
| Class | result-identical if the screening trace tells behaviors apart |

## Question

Many new candidates behave exactly like one the campaign has already measured: different code, the same cycles on every trace. Can one trace spot them early enough to skip their other traces, without ever giving a candidate the wrong scores?

**This is a separate step from the reuse that already happens:**
- OpenEvolve rejects a reply identical to its parent: 28 of the pilot's 241 replies.
- The evaluator reuses the result of identical code (`evaluator/evaluate_candidate.py`): 51 replies.

The screen catches different code with the same behavior.

## How it works

- **The spec field.** Leaving it out keeps today's behavior. `controller/runspec.py` checks that the trace is in the manifest, that the policy supports a screen (`champsim_ipc` does), and that the spec is a campaign.
  ```yaml
  evaluator:
    duplicate_screen:
      trace: bzip2_183B       # one of the manifest's traces
      audit_every: 10         # the default: every 10th candidate runs all traces anyway; 0 turns audits off
  ```
- **Known behaviors** (`generations/<g>/screen-index.json`, written at aggregation by `update_screen_index` in `controller/campaign.py`):
  - candidates from this campaign's earlier generations that completed every trace
  - Failures are never matched, since timeouts depend on the node.
  - A generation's candidates run at the same time, so they can't match each other.
  - Each entry holds the behavior's cycles on every trace.
- **The key** (`evaluator/screen.py`): the screening trace's cycles and LLC misses, plus the ChampSim commit and compiler.
- **Order:** the screening trace starts first, together with the longest traces, so a new behavior isn't delayed (`evaluator/policies/champsim_ipc.py`).
- **On a match:**
  - Running traces are stopped (`run_killable`'s cancel event) and the rest are skipped.
  - The candidate gets the matched behavior's metrics, with its own build time, so OpenEvolve sees what a full evaluation would have given.
  - Its summary records `screen: {trace, matched, matched_generation, audited, stopped_seconds}`.
  - On disk are the screening trace's record and any trace that finished before the match. Stopped runs leave nothing.
- **Conflicts:** if a trace that finished before the match failed or disagrees with the matched behavior's cycles, the match was wrong. The stopped and skipped traces then run, the candidate is scored in full, and its summary records `conflict`.
- **Ambiguous keys:** a key two different behaviors share is never screened. A false match makes its key ambiguous.
- **Audits:** a candidate whose id is a multiple of `audit_every` runs every trace even when it matches. At aggregation, an audit or conflict that differs from its matched behavior is a false match: it's listed in the index and logged once as a `screen_false_match` event.
- **Reuse:** a screened summary is reused only by its own campaign. Another campaign evaluating the same code runs it again.
- **Reports:** `campaign-diversity` counts a screened candidate as the behavior it matched. `campaign-report` counts screened candidates and charges them only what they ran. The generation's `summary.json` counts screened candidates, audits, conflicts, ambiguous keys and false matches.

## Evidence

### 2026-11-13: replay on the pilot's records

`make campaign-screen C=crc2-evolve-c1 CPUS=8` (`controller/diversity.py`, `screen_rows`) replays the screen candidate by candidate, in the order they were generated, from the pilot's recorded results. CPU time is modeled at 8 CPUs, as `crc2-evolve-c2` runs.

| Screening on bzip2_183B | Pilot |
|---|---|
| New candidates | 162 |
| Matched a behavior from an earlier generation | 83 (51%) |
| Wrong matches | 0 |
| Simulation skipped | 371 of 608 CPU-hours (61%) |
| Spent on runs stopped early | 84 CPU-hours |
| Held CPU-hours freed at 8 CPUs | 412 of 812 (51%) |
| Held CPU-hours added by starting it first | 0 |

**The trace matters more than expected.** Only bzip2 screens the pilot without a wrong match.

| Screening trace | Matches | Wrong | Largest gap in a wrong match |
|---|---|---|---|
| bzip2_183B | 83 | 0 | — |
| omnetpp_340B, soplex_66B | 84 | 1 | 0.002% |
| astar, xalancbmk, mcf, GemsFDTD | 80–81 | 2–3 | 0.021% |
| sphinx3_2520B | 46 | 2 | 0.053% |
| gcc, cactusADM, leslie3d, zeusmp | 39–44 | 3–5 | 0.383% |
| milc_360B | 40 | 5 | 6.6% |
| libquantum_1210B | 83 | 6 | 11.2% |

The largest gap is the biggest per-trace cycle difference between a wrongly matched candidate and the behavior it matched.

**What that shows:**
- **Every wrong match is two different behaviors that tie exactly on the screening trace.** None of the wrong matches was a candidate that failed on another trace.
- **Some behaviors differ on only one trace.** One pair differs only on bzip2, by 0.002% of its cycles; another differs only on bzip2 and soplex. So every screen of one or two traces without bzip2 makes at least one wrong match.
- **How far off a wrong match is depends on the trace.** Seven traces go wrong only between near-identical behaviors, at most 0.053% apart. Four go wrong by up to 0.38%, and milc and libquantum by up to 11%. So the trace should be chosen from a replay, not by guess.

### 2026-11-13: built and tested locally

- **`make evaluate-selftest`,** on three tiny traces, where every policy behaves as LRU does:
  - a match reuses the matched scores
  - no match, or an ambiguous key, runs every trace
  - an audit runs every trace
  - a screened summary isn't reused by another campaign
  - a cancel stops a running ChampSim within a second
- **`make campaign-selftest`:** the self-test campaign with the screen on, against the same campaign with it off. Every candidate in both got identical fitness and speedups, and 2 were screened. A result altered into a disagreeing audit was caught as one false match, and its key became ambiguous.
- **`make oe-check`:** `crc2-evolve-c1` and `crc2-evolve-c2` resolve with unchanged evaluator records.

### 2026-11-13: the false-merge check (HPC3 job 113482)

Two members of each of the pilot's three largest behaviors kept identical cycles and LLC misses at 3.6× the evolution length (`docs/campaigns.md` §9). Identical results on the campaign's traces are a stable basis for matching.

## Costs and risks

- **Compute:**
  - No extra simulation: the screening trace is one of the 14.
  - No delay for new behaviors at 8 CPUs.
  - Duplicates waste the partial runs that are stopped early.
- **What it saves:** CPU-hours, so less of the budget and the shared cluster.
- **What it doesn't save:** wall time under `crc2-evolve-c2`. All 42 candidates run at once, and a generation waits for its slowest. Turning the saved CPUs into more candidates per generation would be a scheduling lever.
- **The risk:** a wrong match gives a candidate another behavior's scores, and the search can't tell.
  - On the pilot, with bzip2, there were none.
  - On a new campaign, the LLM may produce behaviors that differ only on other traces. bzip2 was the only clean trace here, so the margin is thin.
  - The audits, ambiguous keys and a replay after every campaign are how we'd notice.
- **What OpenEvolve sees:** a screened candidate gets the same metrics a full evaluation would have given, so prompts and selection aren't affected.

## Decision and next step

- **Class:** result-identical if the screening trace separates behaviors. So it gets a run-spec switch, off by default (`../README.md`).
- **Next:** the first run on HPC3 is `crc2-evolve-c2`'s 2-generation acceptance run, through its own spec, `crc2-evolve-c2-screen-test` (decided 2026-11-13). It audits every second match, so half the matches still run every trace as evidence.
  - Generation 1 can match only the seed's behavior (LRU), so most matches will come in generation 2.
  - Afterwards: `campaign-report`, `campaign-diversity` and `campaign-screen` on it, and any `screen_false_match` events.
- **Open questions:**
  - Should it screen on two strong separators (bzip2 plus omnetpp or soplex) for margin? The screen would then take as long as the slower of the two.
  - Should the trace be re-chosen from each campaign's replay?

## Sources

- `docs/campaigns.md` §9: behavioral diversity, and the false-merge check
- `controller/diversity.py`: `screen_rows`, `screen_replay`, and `make campaign-screen`
