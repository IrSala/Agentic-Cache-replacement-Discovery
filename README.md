# AFAR_HPC

A reproducible OpenEvolve + ChampSim environment for SJSU CoE HPC. It is the foundation for evolving ChampSim cache-replacement policies with OpenEvolve, staged across GPU (generation) and CPU (evaluation) Slurm jobs.

**Status:** Phase 1 is complete; see [docs/status.md](docs/status.md) for what works and the evidence, and [docs/roadmap.md](docs/roadmap.md) for the plan to an autonomous, multi-node research system.
- A fresh clone builds on coe-hpc1 with `make setup` and runs on COE-HPC3 with no modules.
- One GPU job serves a pinned 7B model with vLLM next to OpenEvolve, which evolves a ChampSim LLC replacement policy.
- Candidates are compiled but not yet simulated. That's the next phase, following [docs/experiment-contract.md](docs/experiment-contract.md).

## Two clusters, one workflow

New to the SJSU HPC? Start with [docs/sjsu-hpc-workflow.md](docs/sjsu-hpc-workflow.md): what an HPC is, how SJSU's is configured, and how to develop on it.

SJSU CoE HPC is two clusters that share storage:

| | coe-hpc1 (login node) | COE-HPC3 (g17 + compute/GPU nodes) |
|---|---|---|
| OS | CentOS 7, glibc 2.17 | Rocky Linux 9, glibc 2.34 |
| Internet | yes | no |
| Module tree (`/opt/ohpc/pub`) | its own (local disk) | its own (different) |
| `/home`, `/data`, `/scratch` | shared | shared |
| Used for | editing, building, small tests | `sbatch`, `squeue`, `sacct`; all jobs |

| Step | Where |
|---|---|
| Edit, `make setup`, small tests (`make check`, `make smoke`, `make oe-*`) | coe-hpc1 |
| Submit and monitor jobs | g17 (`ssh coe-hpc3` from coe-hpc1) |
| Jobs run | COE-HPC3 nodes |
| Read logs | anywhere: `logs/` is on the shared `/home` |

### Portability rules

These rules are why a build made on coe-hpc1 runs in HPC3 jobs:
1. **Everything a job uses lives on shared storage** (`/home`, `/data`, `/scratch`), at the same path on both clusters.
2. **It's built on coe-hpc1.** Its glibc 2.17 is the floor, so binaries also run on HPC3's newer glibc:
   - Python is a portable CPython build ([python-build-standalone](https://github.com/astral-sh/python-build-standalone), glibc ≥ 2.17) in `.python/`, and both venvs are made from it.
   - ChampSim is linked with `-static-libstdc++ -static-libgcc`, so it needs only glibc.
3. **Nothing that runs in a job loads coe-hpc1 modules.** Modules (`env/modules.sh`) exist only on coe-hpc1 and are used only for *building*. Jobs source `env/site.sh`, which sets paths and loads no modules. Lmod collections (`module save`/`restore`) are per-cluster for the same reason.
4. **Every environment change is checked on HPC3** with `slurm/check.sbatch` before real work.

## What is pinned

| Component | Version | Pinned in |
|---|---|---|
| Python | CPython 3.11.16, python-build-standalone `20260924` (sha256-verified) | `env/python.pin` |
| OpenEvolve | 0.4.0 (PyPI) | `env/controller-requirements.in` → `.txt` lock |
| ChampSim | `410cee6` (upstream) | git submodule `external/ChampSim` |
| vcpkg (ChampSim's C++ deps) | `6d7bf7e` | ChampSim's own submodule |
| Build compiler | Spack GCC 12.2.0 (coe-hpc1 module; libstdc++ linked statically) | `env/modules.sh` |
| ChampSim config | upstream default (1 core, no prefetch, LLC LRU) | `configs/champsim/baseline.json` |

`env/versions.txt` and `env/modules.lock.txt` record the environment the last time it was verified.

## Quickstart

**On coe-hpc1** (it has internet; g17 doesn't), clone onto shared storage. Anywhere under `/home` or `/data` works:

```bash
git clone --recurse-submodules <repo-url> AFAR_HPC
```

```bash
cd AFAR_HPC && make setup
```

`make setup` runs these steps in order and stops at the first failure:
1. fetch the submodules
2. download the pinned Python into `.python/`
3. create `.venv/controller` and `.venv/evaluator` from the lock files
4. install ChampSim's C++ dependencies with vcpkg
5. build `build/baseline/champsim`
6. run `make check`

It needs no Slurm allocation; builds run on the login node with `-j2`. A fresh clone took 392 s with pip's and vcpkg's caches turned off, as for a new account (2026-10-21), and less with vcpkg's binary cache (`~/.cache/vcpkg`) populated.

**On g17**, confirm that HPC3 sees a working environment:

```bash
ssh coe-hpc3
```

```bash
cd ~/AFAR_HPC && env/check.sh --runtime && sbatch slurm/check.sbatch
```

To work interactively on either cluster (site paths plus the OpenEvolve venv, no modules):

```bash
source env/activate.sh
```

An optional seconds-long ChampSim sanity run (traces are not in the repo):

```bash
make smoke TRACE=/path/to/602.gcc_s-1850B.champsimtrace.xz
```

Anything longer than `make smoke` belongs in a Slurm job, not on a login node.

### LLM assets (once per group)

The model and the vLLM container (about 21 GB) live in `$AFAR_ASSETS`, not in the clone. Fetch them on coe-hpc1:

```bash
make llm-fetch
```

Then pack the container on HPC3, from g17:

```bash
sbatch slurm/build_sif.sbatch
```

- `make llm-fetch` skips whatever is already there.
- If a group member already has these files, point `AFAR_ASSETS` at their copy and skip both steps (see Environment rules).
- `docs/llm-serving.md` explains the two-step container build.

### GPU jobs (from g17)

Check each request with `--test-only` first. Exclude `cs002` until its faulty GPU 0 is repaired.

```bash
sbatch --test-only --exclude=cs002 slurm/openevolve_champsim_compile.sbatch
```

```bash
sbatch --exclude=cs002 slurm/openevolve_champsim_compile.sbatch
```

| Job script | What it runs | Output |
|---|---|---|
| `slurm/llm_ping.sbatch` | the vLLM server, checked through OpenEvolve's client | `logs/llm-ping-<id>.out` |
| `slurm/openevolve_toy.sbatch` | OpenEvolve on its toy example | `runs/toy-<id>/` |
| `slurm/openevolve_champsim_compile.sbatch` | OpenEvolve on the ChampSim policy, compile-only | `runs/champsim-compile-<id>/` |

- **Resume** from a checkpoint with `--export=ALL,CHECKPOINT=runs/<run>/checkpoints/checkpoint_N,ITERATIONS=10`.
- **Summarize** a run with `make oe-summary RUN=runs/<run>`.
- **Test without a GPU:** each script's header shows how to run it on coe-hpc1 against a stand-in server (`AFAR_LLM_FAKE=1`).

## Make targets (coe-hpc1)

| target | does |
|---|---|
| `setup` | `submodules python venvs vcpkg champsim check` |
| `submodules` | `git submodule update --init --recursive` |
| `python` | download the pinned portable CPython into `.python/` and verify its sha256 |
| `venvs` | create both venvs from `env/*-requirements.txt` using `.python/` |
| `vcpkg` | bootstrap vcpkg and install ChampSim's dependencies (`--clean-after-build`) |
| `champsim` | `config.sh` + `make` (static libstdc++) with `configs/champsim/baseline.json` → `build/baseline/champsim` |
| `check` | build checks (with modules), then run-time checks in a clean, job-like environment |
| `check-runtime` | only the run-time checks, in a clean environment |
| `smoke TRACE=…` | 10K warmup + 10K simulated instructions, JSON output parsed |
| `oe-check-config` | load `configs/openevolve/*.yaml` with OpenEvolve's loader; fail on unknown keys |
| `oe-toy-manual` | run OpenEvolve's toy example with no LLM (`llm.manual_mode`). Each prompt appears as `$(OUT)/manual_tasks_queue/<id>.json`; answer it by writing `<id>.answer.json` containing `{"answer": "<SEARCH/REPLACE diff>"}`. Options: `OUT=`, `CHECKPOINT=<dir>` to resume, `ITERATIONS=` (counted from the checkpoint) |
| `oe-summary RUN=…` | one row per iteration of an OpenEvolve run: outcome or evaluator status, score, LLM and evaluation seconds, tokens |
| `oe-compile-selftest` | check the compile-only evaluator on variants of the seed, covering every status and the timeout kill (after `make champsim`) |
| `evaluator-selftest` | self-test the result-record and manifest modules (`evaluator/results.py`, `evaluator/manifest.py`) |
| `baseline-local EXP=…` | run the LRU baseline for a manifest on this host, writing records to `$AFAR_RESULTS`. Seconds-long manifests only (`smoke-tiny-v1`); others belong in `slurm/baseline.sbatch` |
| `llm-fetch` | download the pinned model and unpack the vLLM container into `$AFAR_ASSETS` (`LLM_PARTS=sandbox` or `model`; needs internet) |
| `env-record` | rewrite `env/modules.lock.txt` and `env/versions.txt` |
| `lock` | re-resolve `env/*-requirements.in` into the `.txt` locks (needs internet) |
| `clean` / `distclean` | remove build outputs / also `.python/`, the venvs and `vcpkg_installed/` |

Set `JOBS=N` to change build parallelism (default 2).

## Layout

```
env/
  python.pin                    portable CPython version, release and sha256
  modules.sh                    build-time modules (coe-hpc1 only; sourced by make)
  site.sh                       run-time paths for both clusters (AFAR_ROOT, AFAR_ASSETS); no modules
  activate.sh                   site.sh + controller venv
  controller-requirements.in    OpenEvolve (edit this)
  controller-requirements.txt   pinned lock (generated by `make lock`)
  evaluator-requirements.in     ChampSim-side Python deps (stdlib only for now)
  evaluator-requirements.txt    pinned lock
  check.sh                      --build (coe-hpc1) and --runtime (either cluster) checks
  lock.sh, record-versions.sh
  modules.lock.txt, versions.txt   recorded evidence (generated by `make env-record`)
slurm/                          batch jobs (submit from the repo root on g17)
  baseline.sbatch               LRU baselines for experiment manifests on a CPU node (M1)
  toolchain_test.sbatch         can the node compiler build ChampSim, and what a candidate build costs (M2)
  check.sbatch, discover_gpu.sbatch   environment checks on a CPU node / an A100 node
  build_sif.sbatch              pack the vLLM container into a .sif on a cpuqs node
  llm_serve.sh                  sourced by GPU jobs: start, health-check and stop the vLLM sidecar
  llm_ping.sbatch               serve the model and check it through OpenEvolve's client
  openevolve_toy.sbatch         OpenEvolve on its toy example (R6)
  openevolve_champsim_compile.sbatch   OpenEvolve on the ChampSim policy, compile-only (R7)
configs/champsim/baseline.json  ChampSim configuration for baseline builds
configs/llm/serve.env           pinned vLLM image and model (names, digests, revisions)
configs/openevolve/             OpenEvolve configs: toy.yaml, toy-manual.yaml, champsim-compile.yaml
configs/experiments/           experiment manifests (contract §2): smoke-tiny-v1.json, smoke-v1.json
seeds/evolved_repl.h            the seed candidate: a header-only port of ChampSim's LRU
evaluator/                      candidate evaluation (a Python package; standard library only, except compile_check.py)
  compile_check.py              OpenEvolve evaluator for candidates (compile-only stub)
  manifest.py, results.py       experiment manifests and result records (contract §2, §3)
  run_baseline.py, proc.py      LRU baseline runner; subprocesses killed as a whole group on timeout
  champsim_build.py             build and run ChampSim in a private copy of its source tree
  toolchain_test.py             the measurements behind the candidate build strategy
scripts/                        llm_fetch.sh, llm_ping.py, fake_llm_server.py, oe_check_config.py, oe_run_summary.py
examples/function_minimization/ OpenEvolve's own example (v0.4.0), used to test OpenEvolve itself
docs/status.md                  what works, the evidence, open issues and next steps
docs/roadmap.md                 the plan to an autonomous, multi-node research system (M0–M7)
docs/sjsu-hpc-workflow.md       how to develop on SJSU HPC (for new users and coding agents)
docs/llm-serving.md             LLM serving on HPC3: GPU facts, pinned assets, R5–R6 results
docs/champsim-evaluator.md      how candidates are checked and compiled, and R7 results
docs/experiment-contract.md     candidate format, manifest, result schema, fitness, failure policy (v0)
external/ChampSim/              ChampSim submodule
runs/                           OpenEvolve output, one directory per job (ignored by git)
logs/                           Slurm job logs (contents ignored by git)
```

## Environment rules

- **Venvs are disposable and tied to their location.** If you move the clone, run `make distclean setup`.
- **pip installs wheels only** (`--only-binary=:all:`). Building on glibc 2.17 means pip picks wheels that run on both clusters; numpy resolves to 2.2.6.
- **Large assets** (LLM containers, model weights) go to `$AFAR_ASSETS`, which defaults to `/data/cmpe295-bond/$USER/afar`. If you aren't in that group, set `AFAR_ASSETS` before sourcing `env/site.sh`.
- **Sharing assets:** jobs only *read* `$AFAR_ASSETS`. Files there are readable by the whole group by default, so teammates can use one copy: export `AFAR_ASSETS=<that path>`, for example in `~/.bashrc`, before `make` and `sbatch`, and skip `make llm-fetch`.
  - The pinned file names include their versions, so a mismatched copy fails at job start rather than silently.
  - **Known gap:** `make check` currently requires `$AFAR_ASSETS` to be writable (`docs/status.md`, issue 7).
- **Results** (baselines, later campaigns) go to `$AFAR_RESULTS`, which defaults to `/data/cmpe295-bond/$USER/afar/results`. It defaults from `$USER`, so it stays writable when `AFAR_ASSETS` is a teammate's read-only copy.
- **Keep `/home` small:** the Makefile runs pip with `--no-cache-dir`, so setup adds nothing to `~/.cache/pip`.

## Changing a pin

- **Python packages:** edit `env/<role>-requirements.in`, run `make lock venvs check`, and commit both the `.in` and `.txt` files.
- **Python itself:** update all three values in `env/python.pin`, run `make venvs check`, and commit.
- **ChampSim:**
  1. `git -C external/ChampSim checkout <commit>`
  2. `git submodule update --init --recursive`
  3. `make vcpkg champsim check`
  4. commit the submodule change
- **Build modules:** edit `env/modules.sh`, run `make distclean setup env-record`, and commit.

Each of these changes should be followed by `sbatch slurm/check.sbatch` from g17.

## Porting to another cluster

Edit `env/modules.sh` (build tools) and `env/site.sh` (paths). The build host must provide:
- a C++17 compiler that has a static `libstdc++.a` (GCC 12.2 is known good)
- git ≥ 2.7 (required by vcpkg)
- internet access
- glibc no newer than the glibc of the nodes that run the jobs

## Disk usage

Measured after `make setup` (see `env/versions.txt`):

| Item | Size |
|---|---|
| `external/ChampSim/vcpkg` (port metadata + vcpkg's own cmake/ninja) | 316 MB |
| `vcpkg_installed/` | 22 MB |
| `.python/` | 111 MB |
| `.venv/controller` | 151 MB |
| `.venv/evaluator` | 33 MB |
| total for the environment | about 0.65 GB |
| whole fresh clone, including git history and `build/` (2026-10-21) | 795 MB |
| `$AFAR_ASSETS` on `/data`: model 14 GB + container 7.1 GB | about 21 GB, plus 21 GB of unpacked container while the `.sif` is built |

`--clean-after-build` already removes vcpkg's source downloads and build trees.

`/home` is a shared NFS volume that is nearly full (99% at last check). Keep trace sets and LLM assets on `/data` or `/scratch` and pass them by path. Traces are about 0.7 GB each and never belong in this repo.
