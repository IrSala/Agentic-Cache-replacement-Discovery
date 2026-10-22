# ChampSim candidate evaluator

How OpenEvolve's candidates, LLC replacement policies in one C++ header, are checked. The rules come from `docs/experiment-contract.md`. This page covers how the evaluator implements them and what was learned on the way.

## R7: compile-only stub

`evaluator/compile_check.py` is a stub. It checks a candidate and compiles it, but builds no binary and runs no simulation (that's Phase 3), so every candidate that compiles scores 1.0.

| Piece | File |
|---|---|
| Seed (header-only port of `replacement/lru` at `410cee6`) | `seeds/evolved_repl.h` |
| Evaluator (`evaluate(program_path)`) | `evaluator/compile_check.py` |
| OpenEvolve config, including the system message describing the API | `configs/openevolve/champsim-compile.yaml` |
| GPU job | `slurm/openevolve_champsim_compile.sbatch` |
| Self-test | `make oe-compile-selftest` (coe-hpc1), or `evaluator/compile_check.py --selftest` (run by the job on the node before the LLM starts) |

### How OpenEvolve calls it

No subclassing is involved. OpenEvolve imports the evaluator file and calls its module-level `evaluate(program_path)` (`openevolve/evaluator.py:75-100`):
- **When and where:** in a thread of a worker process, under `asyncio.wait_for(evaluator.timeout)`.
- **Input:** `program_path` is a temporary file holding the candidate, with the seed's extension (`.h`).
- **Output:** an `EvaluationResult(metrics, artifacts)`.
  - `metrics["combined_score"]` is the fitness.
  - `artifacts` are stored with the program and shown in the prompts of its children.

### What `evaluate` does, in order

1. **Environment** (raises, as `infra_error`): the compiler (`$AFAR_CXX`, default `g++`), the seed, and ChampSim's generated `.csconfig/module_decl.inc` and option files must exist. `make champsim` produces the last two.
2. **Static checks** (`invalid_candidate`, no compile):
   - the file is within 32 KB
   - there is exactly one `// EVOLVE-BLOCK-START` line before one `// EVOLVE-BLOCK-END` line
   - the text before and after the block equals the seed's, ignoring trailing whitespace
   - the class head is present
3. **Compile** in a fresh `TemporaryDirectory`:
   - It writes `evolved_repl.h` and a `check.cc` that includes it and `static_assert`s ChampSim's hook detection.
   - It runs `g++ @global.options @absolute.options @module.options -I.csconfig -fsyntax-only -fdiagnostics-color=never -fmax-errors=10`, with ChampSim's own generated flags.
   - It writes nothing into `external/ChampSim`.
4. **Timeout:** 60 s. The compiler runs in its own process group (`Popen(process_group=0)`), and the whole group is killed (`os.killpg`) on expiry.
   - `subprocess.run(timeout=)` would kill only the `g++` driver and leave `cc1plus` running.
   - `preexec_fn` is unsafe in OpenEvolve's evaluation thread (Python `subprocess` docs).
   - OpenEvolve's `evaluator.timeout` (120 s) only stops waiting; it kills nothing.
5. **Result:**
   - `combined_score` 1.0 (`completed`) or 0.0 (any failure), plus `compiles` and `compile_seconds`
   - artifacts: `status`, `reason`, the last 4 KB of compiler output, and the compiler's version line

This follows OpenEvolve's own example for compiled code (`examples/rust_adaptive_sort/evaluator.py`): a temp directory, a subprocess compile with a timeout, and `EvaluationResult` with compiler output as artifacts.

### Things that would otherwise go wrong

- **A wrong hook signature fails silently.** ChampSim picks each hook with `if constexpr (replacement::has_find_victim<…>)` and otherwise falls back to `return return_type{}` (`inc/cache.h:447-459, 475-494`). A candidate whose `find_victim` has a different signature compiles, builds and runs, but its policy is never called.
  - `check.cc` therefore `static_assert`s ChampSim's own traits (`inc/modules.h`), with every signature variant `cache.h` accepts.
  - A mismatch fails with "find_victim is missing or has a signature ChampSim never calls".
- **The online ChampSim docs describe a different API.** The `develop` Modules page shows pure-virtual methods and `register_module`. The pinned commit detects member functions by signature, and the class name must equal the directory name.
  - The system message spells out the pinned `lru.h` signatures and forbids the older and newer styles.
- **Candidates can't add includes.** Includes are outside the block, and an `#include` inside a class body isn't valid C++. The seed therefore includes a fixed set of standard headers, which the system message lists.
- **Known gap:** `-fsyntax-only` doesn't link, so a member that is declared and called but never defined passes. Phase 3's real build catches it.

### Toolchain

| Where | Compiler | How |
|---|---|---|
| coe-hpc1 (`make oe-compile-selftest`) | Spack gcc 12.2.0 | `$(BUILD_ENV)`; this is the compiler that built ChampSim |
| HPC3 GPU node (job) | `/usr/bin/g++` 11.5.0 (Rocky 9) | The job sets `AFAR_CXX=/usr/bin/g++` and logs its version |

The self-test passed under gcc 12.2.0 and gcc 11.3.0 on coe-hpc1; 11.3 is the closest available to the nodes' 11.5. A compile takes about 1.2 s. coe-hpc1's default `g++` (7.3) fails on the seed, because its libstdc++ has no `<charconv>`, so local job tests pass `AFAR_CXX` set to the Spack g++ 12. Which compiler builds real candidates on HPC3 is a Phase 3 decision.

### R7 attempts

| Job | Node / GPU | Result |
|---|---|---|
| 93458 (2026-10-21) | `cs004`, GPU 0 | **Passed.**<br>**Preflight:** the self-test passed on the node with `/usr/bin/g++` 11.5.0 (Red Hat 11.5.0-14) before the server started.<br>**Statuses:** 6 of 9 rewrites `completed`. 1 was `compile_failed`: a lambda used `begin` without capturing it. 2 were `invalid_candidate`: both added `#include <random>` outside the block despite the system message. OpenEvolve rejected 1 more as identical to its parent.<br>**Markers:** kept in 9 of 9 rewrites (8 of 18 in R6's toy).<br>**Plumbing:** all 10 requests returned `200`, there was no key in any log, and the job exited 0. |
| 93495 (2026-10-22) | `cs001`, GPU 2 | **Passed, from a fresh clone of GitHub `main` (`e0176b7`; Phase 1 step G).**<br>**Setup:** `make setup` passed on coe-hpc1 in 392 s with this account's pip and vcpkg caches turned off. CPU check job 93489 passed on `c19`. `make smoke` reproduced 43,703 cycles.<br>**Job:** submitted with `--mem=24G`, because the original 64G request waited for hours (see `docs/llm-serving.md`). The self-test passed on gcc 11.5.<br>**Statuses:** 5 of 10 rewrites `completed`. The other 5 were `invalid_candidate`, every one because the model edited the seed's header comment ("port of replacement/lru") to name its own policy (`scr`, `srrip`). No candidate added an include, and 2 used `<random>`.<br>**Plumbing:** all 10 requests returned `200`, there was no key in any log, and the job exited 0. |

**What the model wrote:** the compiling candidates were real alternatives to LRU, not copies:
- second-chance bits
- LRU with per-line frequency counters
- recency plus frequency
- LRU with reference information

All score 1.0 here, because the stub only checks that a candidate compiles.

**Compiler feedback:** no failing candidate was picked as a parent, so no prompt contained compiler output. That is expected: OpenEvolve shows artifacts only to a program's children, and failures score 0.

**Timings** (job 93458, from `scripts/oe_run_summary.py`):

| | |
|---|---|
| Server start to healthy | 120 s (first use of `cs004`; 75 s on `cs003`) |
| Wall seconds per iteration | 12.4 |
| LLM per iteration | about 12.4 s (about 1,000 completion tokens) |
| Compile check | 0.7 s on the node |
| Largest prompt | 3,806 tokens (+ 4,096 `max_tokens` ≤ 16,384) |

**Open before any campaign:** the seed's header comment ("…a header-only port of replacement/lru") invites edits outside the block. All 5 rejections in 93495 were the model renaming it. Make it policy-neutral (`docs/status.md`, issue 1).

**Change after 93458:** `<random>` is now in the seed's fixed includes and in the system message's header list, since 2 of 9 rewrites added it themselves.
- **Why now:** the seed's fixed text is what every candidate is compared against, so it has to settle before any campaign.
- **Checked:** the self-test passes under gcc 12.2 and 11.3. A fake job replaying a rewrite that uses `std::mt19937_64` gave `completed` on all 3 iterations.
- **Tie behaviour:** with every candidate tied at 1.0, OpenEvolve keeps the seed as the saved best, because a tie never replaces the current best.
