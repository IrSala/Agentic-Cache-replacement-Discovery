# Duplicate screen

| | |
|---|---|
| Stage | evaluation |
| Status | replayed on the pilot, then built and tested locally (2026-11-13). First HPC3 run, 2026-11-13: audits caught 2 false matches, caused by an indexing bug (fixed the same day, locally tested), and bzip2 didn't separate every behavior. Screening on bzip2 and soplex together, decided and built the same day, is tested locally and not yet run on HPC3 |
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
      trace: [bzip2_183B, soplex_66B]   # the manifest's traces to screen on: one id, or a list
      audit_every: 10                   # the default: every 10th candidate runs all traces anyway; 0 turns audits off
  ```
- **Known behaviors** (`generations/<g>/screen-index.json`, written at aggregation by `update_screen_index` in `controller/campaign.py`):
  - candidates from this campaign's earlier generations that completed every trace, including results from the cache, such as a seed another campaign scored
  - Failures are never matched, since timeouts depend on the node.
  - A generation's candidates run at the same time, so they can't match each other.
  - Each entry holds the behavior's cycles on every trace.
- **The key** (`evaluator/screen.py`): each screening trace's cycles and LLC misses, plus the ChampSim commit and compiler. With one trace, the key is the same as before lists were allowed.
- **Order:** the screening traces start first, longest first, together with the longest other traces, so a new behavior isn't delayed (`evaluator/policies/champsim_ipc.py`). The match is checked when the last screening trace completes. If one fails, there's no match.
- **On a match:**
  - Running traces are stopped (`run_killable`'s cancel event) and the rest are skipped.
  - The candidate gets the matched behavior's metrics, with its own build time, so OpenEvolve sees what a full evaluation would have given.
  - Its summary records `screen: {trace, matched, matched_generation, audited, stopped_seconds}`.
  - On disk are the screening trace's record and any trace that finished before the match. Stopped runs leave nothing.
- **Conflicts:** if a trace that finished before the match failed or disagrees with the matched behavior's cycles, the match was wrong. The stopped and skipped traces then run, the candidate is scored in full, and its summary records `conflict`.
- **Ambiguous keys:** a key two different behaviors share is never screened. A false match makes its key ambiguous.
- **Audits:** a candidate whose id is a multiple of `audit_every` runs every trace even when it matches. At aggregation, an audit or conflict that differs from its matched behavior is a false match: it's listed in the index and logged once as a `screen_false_match` event.
- **Reuse:** a screened summary is reused only by its own campaign. Another campaign evaluating the same code runs it again.
- **Reports:** `campaign-diversity` counts a screened candidate as the behavior it matched. `campaign-report` counts screened candidates and charges them only what they ran. The generation's `summary.json` counts screened candidates, audits of matches, conflicts, ambiguous keys and false matches. A candidate's own `screen.audited` says only that it was chosen for audit.

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

### 2026-11-13: the first run on HPC3 (`crc2-evolve-c2-screen-test`, Slurm jobs 113743–113810)

c2's spec for 2 generations: 42 candidates × 8 CPUs, screening on bzip2, with every candidate whose id is even chosen for audit. It finished in 2.0 hours and used 190.9 CPU-hours, against an estimate of 478 without the screen. The estimate assumes every candidate completes; 41 of the 84 replies failed to build or run, as is usual early on (the pilot's generations 1 and 2 had 54% and 38% failures; this run had 64% and 33%).

| | Generation 1 | Generation 2 |
|---|---|---|
| Behaviors the screen knew | 0 | 15 |
| Candidates matched on bzip2 | 0 | 6 |
| Screened (other traces skipped) | 0 | 1 |
| Matches that were audits | 0 | 5 |
| False matches | 0 | 2 |

**What happened:**
- **The 4 matches with `b3a0bbfc58f62476` (fitness 1.0259) were right.**
  - The 3 audits agreed with it on every trace.
  - The 4th, `f820f293f03359dd`, was screened. It ran bzip2 (389 s), and its 7 other running traces were stopped after 2,723 CPU-seconds in all. A full evaluation takes 16,285 CPU-seconds, so the screen saved about 3.7 CPU-hours of simulation.
- **The 2 matches with `09bfa84f27b7bed1` (fitness 1.0000122) were wrong.**
  - Both were audits, so they ran every trace, and both turned out to be LRU (fitness 1.0).
  - LRU and `09bfa84f` have identical bzip2 cycles and LLC misses. They differ on 4 other traces by 0.005–0.010% (GemsFDTD, leslie3d, milc, soplex).
  - Aggregation logged both as `screen_false_match` events, and the key became ambiguous.
- **Why LRU wasn't a known behavior: a bug.**
  - Generation 0's seed was scored from the cache, since `crc2-evolve-c1` had evaluated it.
  - `update_screen_index` (`controller/campaign.py`) skips cached results. It assumes this campaign indexed them when it evaluated them, which holds only for its own results.
  - So generation 0's index was empty. LRU candidates matched the near-LRU behavior instead.
- **With the bug fixed, bzip2 would still have made one wrong match.**
  - In generation 1, `09bfa84f` (not an audit) would have matched LRU and been given fitness 1.0 instead of 1.0000122. Its other traces would never have run.
  - Only a later audit of another candidate with its behavior would show it.
  - `make campaign-screen C=crc2-evolve-c2-screen-test CPUS=8` replays this: bzip2 makes 5 matches, 1 wrong (a gap of 0.010%), with 2 candidates not screened because their key is ambiguous.
- **No trace is clean in both campaigns:**
  - In this run's replay, only soplex and GemsFDTD screen without a wrong match (6 matches each).
  - On the pilot, soplex made 1 wrong match and GemsFDTD 2–3.
  - But bzip2 alone separates all 42 of the pilot's behaviors, and soplex alone all 26 of this run's (`make campaign-diversity`). So a screen on both would have made no wrong match in either campaign.
- **A count that reads wrong:** the generation's `audited` counted every candidate chosen for audit (17 and 11 in this run's files), not only the matches among them (0 and 5).

### 2026-11-13: both fixed and tested locally

- **The index:** `update_screen_index` now indexes cached results too. A result another campaign scored is new to the index, and one this campaign scored earlier merges as the same behavior. Cached results stay out of the counts and the false-match check, which belong to the generation that evaluated them.
- **The count:** `audited` counts only candidates that matched a behavior and ran every trace anyway.
- **`make campaign-selftest`: 64 checks pass.** Two are new:
  - a seed marked as scored by another campaign is a known behavior after generation 0
  - a candidate chosen for audit that matched nothing isn't counted
  
  Both fail on the old code.
- **This run's records, re-indexed in memory with the fix** (its files unchanged):
  - generation 0 knows LRU
  - generation 1 marks LRU's bzip2 key ambiguous, since `09bfa84f` shares it
  - the audits of matches are 0 and 5
  
  This agrees with `make campaign-screen`.

### 2026-11-13: screening on bzip2 and soplex (decided, built and tested locally)

**The replay on both campaigns' records**, at 8 CPUs (`make campaign-screen C=<id> TRACE=bzip2_183B,soplex_66B CPUS=8`). The replay now times the screen by the schedule: the match is known when the last screening trace ends, and other traces run until then. bzip2 alone reproduces the pilot's numbers above exactly.

| Screen | Campaign | Matches | Wrong | Simulation skipped | Simulated before the match | Held CPU-hours freed |
|---|---|---|---|---|---|---|
| bzip2 | pilot | 83 of 162 | 0 | 370.9 of 608.3 | 83.8 | 411.6 of 812.4 (51%) |
| bzip2 + soplex | pilot | 83 of 162 | 0 | 336.8 | 226.7 | 234.7 (29%) |
| soplex | pilot | 84 | 1 (0.002%) | 352.2 | 241.0 | 237.7 |
| bzip2 | c2 test run | 5 of 73 | 1 (0.010%) | 21.1 of 139.7 | 4.6 | 25.1 of 200.7 |
| bzip2 + soplex | c2 test run | 6 of 73 | 0 | 23.0 | 15.3 | 18.2 |
| soplex | c2 test run | 6 | 0 | 23.7 | 16.1 | 18.2 |

**What it shows:**
- **The pair makes no wrong match on either campaign,** and finds every match bzip2 alone found on the pilot.
- **The margin costs 43% of the freed CPU-hours.** soplex runs about 22 minutes, against bzip2's 7, and a duplicate keeps running its other traces until then. On the pilot, the held CPU-hours freed fall from 412 to 235. The simulation saved (skipped minus run before the match) falls from 287 to 110 CPU-hours.
- **New behaviors still cost nothing extra.** soplex is among the 8 longest traces, so on 8 CPUs it would have started at once anyway.

**Built:**
- `trace` accepts a list in the run spec (`controller/runspec.py`).
- The evaluator keys on every listed trace.
- The index, the replay and the reports all take a list.
- Existing specs and records keep a single string. `crc2-evolve-c1`, `crc2-evolve-c2` and `crc2-evolve-c2-screen-test` resolve as before.

**Tests:**
- **`make evaluate-selftest`:** a two-trace key matches when both traces agree, and a one-trace key doesn't match a two-trace screen.
- **`make campaign-selftest` (65 checks):**
  - the screen-on campaign now screens on bzip2 and soplex, with every score unchanged and 2 candidates screened
  - the replay on two traces drops a wrong match and times the match by the longer trace
- **The run-spec self-test** rejects a repeated trace, an empty list, and a listed trace that isn't in the manifest.

## Costs and risks

- **Compute:**
  - No extra simulation: the screening traces are among the 14.
  - No delay for new behaviors at 8 CPUs.
  - Duplicates waste whatever their other traces ran before the match was known. That's about 7 minutes with bzip2 alone and 22 with bzip2 and soplex.
- **What it saves:** CPU-hours, so less of the budget and the shared cluster.
- **What it doesn't save:** wall time under `crc2-evolve-c2`. All 42 candidates run at once, and a generation waits for its slowest. Turning the saved CPUs into more candidates per generation would be a scheduling lever.
- **The risk:** a wrong match gives a candidate another behavior's scores, and the search can't tell.
  - On the pilot, with bzip2, there were none. On the first HPC3 run there was one: a near-LRU behavior that ties LRU on bzip2 and differs by at most 0.010% elsewhere.
  - bzip2's one wrong match was 0.010% off, so the wrong scores would have been close. Other traces' wrong matches have been up to 11% off.
  - The audits, ambiguous keys and a replay after every campaign are how we'd notice. A screened candidate's other traces never run, so a replay can't check it. Only an audit of another candidate with the same behavior can.
- **What OpenEvolve sees:** a screened candidate gets the same metrics a full evaluation would have given, so prompts and selection aren't affected.

## Decision and next step

- **Class:** result-identical if the screening trace separates behaviors. So it gets a run-spec switch, off by default (`../README.md`).
- **First HPC3 run (2026-11-13):** the audits did their job, catching both false matches. The run found a bug, now fixed, and one trace wasn't enough.
- **Decided (2026-11-13): screen on bzip2 and soplex together,** trading 43% of the freed CPU-hours for a margin. Each trace separated every behavior in one campaign. A campaign that turns the screen on uses `trace: [bzip2_183B, soplex_66B]`.
- **Still open:**
  - which campaign runs it first on HPC3
  - whether to re-choose the traces from each campaign's replay. `make campaign-screen` after every campaign shows whether the pair is still clean.

## Sources

- `docs/campaigns.md` §9: behavioral diversity, and the false-merge check
- `controller/diversity.py`: `screen_rows`, `screen_replay`, and `make campaign-screen`
