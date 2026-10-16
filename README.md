# AFAR_HPC

A reproducible OpenEvolve + ChampSim environment for SJSU CoE HPC. It is the foundation for evolving ChampSim cache-replacement policies with OpenEvolve, staged across GPU (generation) and CPU (evaluation) Slurm jobs.

**Status:** Phase 1 is in progress. The environment is portable across the two clusters below, and OpenEvolve is being brought up one step at a time. The experiment contract is recorded in [docs/experiment-contract.md](docs/experiment-contract.md) and is not implemented yet.

## Two clusters, one workflow

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

It needs no Slurm allocation; builds run on the login node with `-j2`. In testing it took a few minutes with vcpkg's binary cache (`~/.cache/vcpkg`) already populated, and it takes longer the first time an account compiles the vcpkg libraries.

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
configs/champsim/baseline.json  ChampSim configuration for baseline builds
configs/openevolve/             OpenEvolve configs
examples/function_minimization/ OpenEvolve's own example (v0.4.0), used to test OpenEvolve itself
docs/experiment-contract.md     candidate format, manifest, result schema, fitness, failure policy (v0)
external/ChampSim/              ChampSim submodule
logs/                           Slurm job logs (contents ignored by git)
```

## Environment rules

- **Venvs are disposable and tied to their location.** If you move the clone, run `make distclean setup`.
- **pip installs wheels only** (`--only-binary=:all:`). Building on glibc 2.17 means pip picks wheels that run on both clusters; numpy resolves to 2.2.6.
- **Large assets** (LLM containers, model weights) go to `$AFAR_ASSETS`, which defaults to `/data/cmpe295-bond/$USER/afar`. If you aren't in that group, set `AFAR_ASSETS` before sourcing `env/site.sh`.

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
| total | about 0.65 GB |

`--clean-after-build` already removes vcpkg's source downloads and build trees.

`/home` is a shared NFS volume that is nearly full (99% at last check). Keep trace sets and LLM assets on `/data` or `/scratch` and pass them by path. Traces are about 0.7 GB each and never belong in this repo.
