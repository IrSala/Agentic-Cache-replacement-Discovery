# Longest-first trace order

| | |
|---|---|
| Stage | evaluation |
| Status | default since 2026-10-29 |
| Switch | none |
| Class | result-identical by construction |

## Question

Can a candidate run on fewer CPUs than it has traces and still finish when its slowest trace does? If so, the same CPUs evaluate more candidates.

## How it works

- `run_order` in `evaluator/policies/champsim_ipc.py` starts a candidate's traces in order of the LRU baseline's run times, longest first.
- This is longest-processing-time-first (LPT) scheduling: the long traces start at once and the short ones fill the gaps.
- Records come back in the manifest's order, so a candidate's outcome doesn't depend on the order its traces ran.

## Evidence

**2026-10-29: replay of the pilot's 125 completed candidates**, each with its own measured trace times (`docs/campaigns.md` §8):

| CPUs per candidate | CPU-hours held | Wall time vs 14 CPUs |
|---|---|---|
| 14 | 1,363 | — |
| 8 | 779 (−43%) | the same for every candidate |
| 7 | 696 | the same at the median, 1.13× at worst |
| 1 per trace (one task each) | 580 (−57%) | the same |

**On HPC3 (jobs 98637–98639):** results were identical on 8 CPUs.

**2026-11-13: `crc2-evolve-c2`'s acceptance run** (`crc2-evolve-c2-screen-test`, 2 generations × 42 candidates × 8 CPUs, jobs 113743–113810; `make campaign-report`):
- **Efficiency 69%:** 131.2 of 189.9 CPU-hours held were spent simulating, against 44% for the pilot on 14 CPUs.
- **Per evaluation:** 2.64 CPU-hours held.
- **Each generation's evaluation:** 54 and 60 minutes until the last task finished. That's 23 minutes after half the tasks in generation 2. In generation 1, half had failed within the first minute.
- The duplicate screen was on but screened only one candidate, so it barely changes these numbers.

## Costs and risks

- **Results:** none. Each trace is an independent, deterministic simulation.
- **Wall time:** unchanged at 8 CPUs.

## Decision and next step

- **Decision:** always on, with no switch.
- **Next:** `crc2-evolve-c2` runs 42 candidates on 8 CPUs each. Its acceptance run measured 69% efficiency, below the 80% at which `docs/campaigns.md` §8 says to build the next lever: one 1-CPU task per trace.

## Sources

- `docs/campaigns.md` §8, with sources on LPT scheduling and job efficiency
