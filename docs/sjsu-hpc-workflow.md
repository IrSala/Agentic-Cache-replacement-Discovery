# Developing on SJSU CoE HPC: workflow guide

A practical guide for people (and LLM coding agents) new to the SJSU College of Engineering HPC. Facts marked *(measured)* were checked between 2026-10-15 and 2026-10-22. Clusters change, so re-check with `sinfo`, `module avail` and `df -h` before relying on numbers.

## Rules at a glance

1. **There are two clusters.** You log in to **coe-hpc1**, which is for editing, building and small tests. **COE-HPC3** runs all jobs: you reach its head node **g17** with `ssh coe-hpc3` and submit from there.
2. **Only storage is shared** between them: `/home`, `/data`, `/scratch`, at the same paths. The OS, the module tree (`/opt/ohpc/pub`) and Slurm are **not** shared.
3. **Build and download on coe-hpc1, onto shared storage.** HPC3 jobs only read what you built. g17 and every compute node have **no internet**.
4. **Jobs must not depend on coe-hpc1 modules**, or on venvs made from coe-hpc1's module Python. Neither exists on HPC3.
5. **Make built software portable:** build against coe-hpc1's older glibc (2.17), link C++ runtimes statically, and use a portable Python, or use Apptainer containers.
6. **Heavy work only through Slurm.** Login nodes are for editing, compiling and small tests.
7. **Use absolute paths in job scripts**, and create the log directory before you submit.
8. **After any environment change, run a short check job on HPC3** before real work.

## 1. What an HPC cluster is

An HPC cluster is many computers ("nodes") that share storage and are managed by a scheduler.
- **Login (head) node:** "serves as an access point to the cluster". It's for file transfer, setting up software and quick tests. It "should not be used for time-consuming or resource-intensive tasks".
- **Compute nodes:** "dedicated to long or hard tasks". You reach them only through the scheduler.
- **Shared filesystem:** "Files saved on one node are typically accessible from all nodes."
- **Scheduler (Slurm):** your work is a **job**, usually a shell script submitted with `sbatch`. You request cores, memory, time and GPUs in a **partition** (queue). The job waits in the queue until resources are free. Requests are binding: "if you exceed them, your job will be killed."

SJSU's own guide says the same: use the login node to "write scripts and code, compile programs, test execution of your programs on small data". Schedule real work "in batch mode (*preferred*)" or as interactive Slurm sessions. Install your own software (pip, Anaconda, Singularity/Apptainer) in your home directory.

## 2. How SJSU CoE HPC is configured

### 2.1 Access

- **Network:** you must be on the campus network or VPN. Then `ssh <SJSU-ID>@coe-hpc1.sjsu.edu`.
- **Eligibility:** access is for CoE faculty and students, with a faculty sponsor. It's granted for up to 6 months (class) or 1 year (capstone or research).
- **First time on HPC3**, per the coe-hpc1 login banner. Run these on coe-hpc1 (`~/.ssh/config` may already be set up, as in this repo's account):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -q -N "" && cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
```

Then set `IdentityFile ~/.ssh/id_ed25519` in `~/.ssh/config`, and connect with `ssh coe-hpc3`. Its hostname is g17.

### 2.2 The two clusters *(measured)*

| | coe-hpc1 (login node) | COE-HPC3: g17 head node + compute/GPU nodes |
|---|---|---|
| OS / glibc | CentOS 7.9 / 2.17 | Rocky Linux 9.8 / 2.34 |
| Internet | yes | **no**, on g17 and all nodes |
| Slurm | not usable ("Unable to contact slurm controller") | `sbatch`, `squeue`, `sacct` from g17 |
| Module tree `/opt/ohpc/pub` | its own local disk | its own NFS export, with different contents |
| Compilers | default gcc 7.3 (the `gnu7` module in login shells; `/usr/bin/g++` is 7.3.1). Its libstdc++ lacks C++17's `<charconv>`. Spack modules go up to gcc 12.2 | system gcc/g++ 11.5 (no gcc modules) |
| git | system 1.8.3; Spack module 2.39.1 | system 2.52 |
| Python modules | python3/3.5–3.11.5 | python3/3.11.7, 3.12.12 (default) |
| Apptainer | module `apptainer/1.3.2` | system `/usr/bin/apptainer` 1.5.x |
| Shared storage | `/home`, `/data`, `/scratch` | the same paths |

On coe-hpc1 the Spack modules may not be on `MODULEPATH` in non-interactive shells or jobs. Run `module use /opt/ohpc/pub/apps/spack/share/spack/modules/linux-centos7-broadwell` first.

### 2.3 Partitions and hardware *(measured with `sinfo`)*

| Partition | Time limit | Nodes |
|---|---|---|
| `defq` (default) | 6 h, **at most 2 CPUs per node** | `c1`–`c20` (56 cores, ~112–128 GB each) and `homacs1`–`4` (128 cores, ~251 GB each): 1,632 CPU cores. Default memory 1 GB per CPU |
| `cpuqs` / `cpuqm` / `cpuql` | 5 / 14 / 21 days | same CPU nodes |
| `gpuqs` / `gpuqm` / `gpuql` | 2 / 7 / 14 days | `cs001`–`cs004`, `g1`–`g16`, `g18`–`g19` |
| `nsfqs` / `nsfqm` / `nsfql` | 2 / 14 / 21 days | `g20`–`g32` (not characterized here) |
| `condo` | 30 days | `condo1`, `condo3`–`11`. Faculty-owned; your jobs can be preempted by the owner |

The GPU partitions mix three kinds of GPU:

| GPU | Nodes | Request with | Notes |
|---|---|---|---|
| NVIDIA A100-PCIE-40GB | `cs001`–`cs004`, 4 per node (64 CPUs, ~250 GB RAM) | `--gres=gpu:a100:1` | compute capability 8.0; driver 590.44.01, CUDA ≤ 13.1 |
| H100 | `g16`, `g18`, `g19`, 1 per node | `--gres=gpu:h100:1` | |
| P100 | `g1`–`g15`, 1 per node | `--gres=gpu:p100:1` | compute capability 6.0; too old for vLLM and many recent frameworks |

A plain `--gres=gpu:1` matches **any** type: P100, A100 or H100. On 2026-10-22, other users' `gpu:1` jobs held A100s on `cs001` and `cs003`. Name the type you need. GPU nodes have a local SSD at `/tmp` (`$TMPDIR`, about 370 GB on `cs002`). **`cs002` GPU 0 is faulty** (uncorrectable ECC errors, 2026-10-15/21), so submit GPU jobs with `--exclude=cs002` until it's repaired.

**Per-user limits** (`sacctmgr show qos format=Name,MaxTRESPerUser,MaxJobsPerUser`):
- `gpuqs` uses the `gpu3nodes` QOS (`scontrol show partition gpuqs`), which allows **2 nodes per user**: at most two 4×A100 nodes, or 8 A100s, at once. `nsf3nodes` has the same cap.
- The `normal` QOS sets no per-user cap.
- The 56-core CPU nodes have about 2.3 GB of RAM per core, so a job's memory request can fill a node before its cores do. Measure real use with `sacct -j <id> --format=JobID,MaxRSS`. HPC3's accounting holds only jobs run through HPC3; jobs from before the move are not there.
- **Memory can block a GPU job while GPUs are free** *(measured)*. A100 nodes have about 250 GB of RAM, shared by their 4 GPUs. On 2026-10-22, the nodes with a free A100 had only 27–59 GB left unallocated, so a 64G request waited for hours (`Resources`) and a 24G request started at once.
  - A vLLM job serving a 7B model peaked at about 4 GB of host RAM, because the weights and cache live on the GPU.
  - Request what `sacct` measured, plus a margin.

### 2.4 Storage

| Path | What it's for | Notes *(measured 2026-10-15)* |
|---|---|---|
| `/home/<id>` | code, environments, small results | NFS, shared, **99% full** across all users |
| `/data/<group>/` | durable project data: models, containers, datasets | NFS, 7.2 TB free; one writable directory per group (e.g. `cmpe295-bond`) |
| `/scratch/<group>/` | large, temporary, I/O-heavy job files | NFS, 13 TB free |
| `/tmp` on a compute node | per-job temporary files | local SSD; deleted after the job |

SJSU publishes **no quota, backup or purge policy** for `/data` or `/scratch`. Common practice elsewhere: scratch isn't backed up and may be purged, so never keep the only copy of anything there. Group directories are shared with other students, so work inside `<group>/<your-id>/`.

`/data/<group>` itself is open only to group members (`drwxrws---`). Files you create there are readable by the group but not writable by it, under the default umask. So **one read-only copy of a large model or container can serve the whole group**: teammates read it by path instead of downloading their own.

### 2.5 Modules are per cluster

Lmod modules are scripts that point `PATH` and related variables at software under `/opt/ohpc/pub`. That tree differs between the clusters, so a module name that works on coe-hpc1 is "unknown" on HPC3. Saved collections (`module save` / `module restore`) are per cluster too: on g17, Lmod warns *"The system MODULEPATH has changed: please rebuild your saved collection."* Lmod's documentation says sites with shared home directories should set `LMOD_SYSTEM_NAME` per cluster.

## 3. The development workflow

### 3.1 The loop

| Step | Where | Example |
|---|---|---|
| Edit code and configs | coe-hpc1 (or your laptop + git) | `vim`, `git pull` |
| Download and build onto shared storage | coe-hpc1 | `make setup`, `pip install`, `apptainer pull` |
| Small tests (seconds to minutes) | coe-hpc1 | unit tests, a 10K-instruction smoke run |
| Check the environment on HPC3 | g17, then a short job | `sbatch check.sbatch` |
| Submit real work | g17 | `sbatch job.sbatch` |
| Monitor | g17 | `squeue -u $USER`, `sacct -j <id>` |
| Read logs and results | either cluster | files on `/home` or `/data` |

### 3.2 Making software portable (the key technique)

Anything built on coe-hpc1 (glibc 2.17) runs on HPC3 (glibc 2.34) if:
- it lives on shared storage
- it doesn't load coe-hpc1 modules at run time

This is the same principle as Python's manylinux wheels: build on the oldest system, run on newer ones. Three standard ways to get there:
- **Python:** don't base venvs on a module's Python. Use a portable interpreter on shared storage: [python-build-standalone](https://github.com/astral-sh/python-build-standalone), which needs glibc ≥ 2.17, or a conda/Miniforge environment. Then create venvs from it. Venvs record absolute paths and are "inherently non-portable": rebuild them instead of moving them.
- **C/C++:** build with a modern compiler module on coe-hpc1 and link with `-static-libstdc++ -static-libgcc`, so the binary needs only glibc. Verify with:
  - `ldd <binary>`: no `libstdc++` or `libgcc_s`, nothing "not found"
  - `objdump -T <binary> | grep -o 'GLIBC_[0-9.]*' | sort -V | tail -1`: should be ≤ 2.17
- **Containers:** `apptainer pull` on coe-hpc1 into `/data`, then `apptainer exec` on HPC3. Use `--nv` for GPUs: it binds the host's NVIDIA driver libraries in, so the container's CUDA version must be supported by the host driver.

The alternative is to build on the HPC3 side, inside jobs or on g17. Because HPC3 is offline, every download then has to be staged from coe-hpc1 first.

### 3.3 Python packages

- **Prefer pre-built wheels:** `pip install --only-binary=:all: …`. On glibc 2.17 this picks wheels that run on both clusters (for example numpy resolves to 2.2.6). Source builds tend to fail on CentOS 7.
- **Pin exact versions:** keep a top-level `requirements.in` and a frozen `requirements.txt` from `pip freeze`. Install from the frozen file.
- **Install on coe-hpc1**, where the internet is.

### 3.4 Data, models and containers for offline nodes

- **Download everything on coe-hpc1:**
  - Hugging Face models: `hf download <repo> --revision <commit> --local-dir /data/...`
  - Containers: `apptainer pull <file>.sif docker://<image>@sha256:<digest>`
  - Datasets and traces
- **Pin by immutable ID:** commit, digest or sha256, never "latest".
- **Keep tool caches off `/home`:** set `APPTAINER_CACHEDIR`, `APPTAINER_TMPDIR`, `HF_HOME` and `PIP_CACHE_DIR` under `/data/<group>/<id>/`. pip's default `~/.cache/pip` reached 3 GB on this account.
- **Expect large pulls to be slow** when the temp directory is on NFS: unpacking a 9 GB vLLM image took about 30 minutes. Run pulls at low priority (`nice -n 19`). On coe-hpc1, packing that image into a `.sif` then **hung** in its old `mksquashfs`. Unpack to a sandbox there and pack the `.sif` in an HPC3 job instead (see Common problems).
- **In jobs, set offline flags** such as `HF_HUB_OFFLINE=1`, so tools fail fast instead of hanging on the network.

## 4. Slurm essentials

**Batch script template** (submit from the project root on g17):

```bash
#!/bin/bash
#SBATCH --job-name=myjob
#SBATCH --partition=defq                 # GPU: gpuqs + --gres=gpu:a100:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=01:00:00
#SBATCH --output=logs/%x-%j.out          # logs/ must already exist
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
source env/site.sh                       # paths only: no coe-hpc1 modules
.venv/bin/python my_script.py            # tools from shared storage
```

**Common commands:**
- **Submit:** `sbatch job.sbatch`. Use `--parsable` to print only the job ID.
- **Interactive session:**

  ```bash
  srun -p defq -c 2 --mem=4G -t 00:30:00 --pty bash
  ```

- **Job arrays:** `--array=0-99%10` runs tasks 0–99, at most 10 at once. Each task reads `$SLURM_ARRAY_TASK_ID`.
- **Dependencies:** `--dependency=afterok:<id>` starts only after `<id>` succeeded (exit code 0).
- **Monitor and cancel:**
  - `squeue -u $USER`
  - `sacct -j <id> --format=JobID,State,Elapsed,MaxRSS,ExitCode`
  - `scancel <id>`

Chain jobs like this:

```bash
a=$(sbatch --parsable step1.sbatch); sbatch --dependency=afterok:$a step2.sbatch
```

**Environment:** by default `sbatch` copies your submitting shell's environment into the job (`--export=ALL`), including any modules loaded on g17. To prove a job doesn't depend on that, run its payload in a clean environment:

```bash
env -i HOME="$HOME" USER="$USER" PATH=/usr/bin:/bin bash script.sh
```

**HPC3 Slurm settings that matter for automation** *(measured 2026-10-23)*:
- Slurm 23.11.10.
- **Jobs can submit jobs:** compute nodes have `sbatch`, `squeue` and `sacct`, so a job can submit its own successor.
- **`scrontab` (Slurm's cron) is disabled.** For periodic checks, use a short job that resubmits itself with `--begin=now+1hour`.
- **Failed dependencies:** to react to a failed job, submit a handler with `--dependency=afternotok:<id>`. `afterok` on an array fires only if *every* task succeeds; otherwise the dependent job waits forever (`DependencyNeverSatisfied`) unless it was submitted with `--kill-on-invalid-dep=yes`.
- **Requeueing:** `JobRequeue=1`, so jobs are requeued after a node failure. A job script must be safe to run twice.
- **Limits:** `MaxArraySize` is 1001 (array indices 0–1000), and `MaxJobCount` is 10000 jobs cluster-wide.
- **Mail:** `MailProg=/bin/mail`, so `--mail-type=FAIL --mail-user=<you>` can notify you (delivery not yet tested).

**Sizing a request and diagnosing a wait:**
- `sbatch --test-only <script>` reports whether the request fits, and the expected start time and node, without submitting.
- `squeue -j <id> -o "%i %T %r %S"` shows a pending job's reason (`Resources`, `Priority`, `QOSMaxNodePerUserLimit`, …) and Slurm's estimated start.
- `scontrol show node <node> | grep -E "CPUAlloc|AllocMem|AllocTRES"` shows what's left on a node. The free GPUs, CPUs *and* memory must all fit the request.
- Size `--mem` and `--time` from `sacct` measurements, not guesses. Oversized requests wait longer.

**Signals and services inside a job:**
- `--signal=B:TERM@60` sends SIGTERM 60 s before the time limit to the **batch shell only**. Run long programs as `prog & wait $!` so a trap fires promptly, and have the trap pass the signal on to `prog` so it can save its state. Slurm kills whatever is left when the script exits.
- To run a server that a job's own client uses (for example an LLM), start it in the same job:
  - bind it to `127.0.0.1` on a port derived from `$SLURM_JOB_ID`
  - give it a random key, passed through the environment (command lines are visible to every user on the node)
  - poll its health endpoint before starting the client, and stop it from an `EXIT` trap
- Nodes are shared, so other users can reach `127.0.0.1` too. The random key and the server's short life are the mitigation.

## 5. Common problems

| Symptom | Cause | Fix |
|---|---|---|
| `Unable to contact slurm controller` on coe-hpc1 | coe-hpc1's Slurm no longer reaches any compute nodes | submit from g17 (`ssh coe-hpc3`) |
| `module unknown: "gcc-12.2.0-…"` in a job | the module exists only on coe-hpc1 | don't load build modules in jobs; use portable builds (3.2) |
| `Permission denied` running a venv's `python` on HPC3 | the venv points at coe-hpc1's module Python | rebuild the venv from a portable Python on shared storage |
| `module restore` warns that the system MODULEPATH has changed | collections are per cluster | save a separate collection on each cluster, or avoid modules in jobs |
| pip, git or Hugging Face timeouts on g17 or in jobs | HPC3 is offline | download on coe-hpc1 into shared storage |
| pip tries to compile numpy and fails on coe-hpc1 | newest wheels need a newer glibc than 2.17 | `pip install --only-binary=:all:` |
| `GLIBCXX_3.4.xx not found` | binary built with a newer libstdc++ than the node has | link `-static-libstdc++ -static-libgcc`, or rebuild on the target |
| vcpkg or other tools complain about git | system git 1.8.3 on coe-hpc1 is too old | load the Spack git module (2.39.1) for the build |
| job finishes but no log file appears | the `--output` directory didn't exist | `mkdir -p logs` (or keep `logs/.gitkeep` in the repo) |
| `sbatch: … Requested node configuration is not available` | the request can't fit any node in that partition. For example, `defq` caps jobs at 2 CPUs per node | check limits with `scontrol show partition <p>`, test with `sbatch --test-only`, and use `cpuqs` for more cores |
| a GPU job lands on a P100 | `--gres=gpu:1` matches any type | `--gres=gpu:a100:1` (or `h100`) |
| `CUDA error: CUDA-capable device(s) is/are busy or unavailable` on a GPU node | the allocated GPU is faulty. Seen on `cs002` GPU 0, which had 73 volatile uncorrectable ECC errors in `nvidia-smi` | check `nvidia-smi -q -i $CUDA_VISIBLE_DEVICES` for ECC errors; resubmit with `--exclude=<node>`; report the node and GPU to HPC support |
| `/home` fills up during a pull or download | tool caches default to `~/.cache` and `~/.apptainer` | set `APPTAINER_CACHEDIR`, `APPTAINER_TMPDIR` and `HF_HOME` under `/data` |
| a GPU job stays `PENDING (Resources)` for hours while `squeue` shows free GPUs | the nodes with free GPUs don't have enough unallocated memory (or CPUs) left for the request | check with `scontrol show node`; lower `--mem` to what `sacct` MaxRSS measured, plus a margin |
| `fatal error: charconv: No such file or directory` compiling C++17 on coe-hpc1 | the default gcc 7.3 is too old for that header | load the Spack gcc 12.2 module for builds |
| a program in a job is killed at the time limit without saving | `--signal=B:` reaches only the batch shell; Slurm kills the rest when the script exits | trap the signal in the script and pass it on to the program (section 4) |
| `apptainer pull` sits at "Creating SIF file" for hours on coe-hpc1, using no CPU | coe-hpc1's `mksquashfs` (CentOS 7 squashfs-tools 4.3) deadlocked on a large image | stop it. Then run `apptainer build --sandbox --fix-perms <dir> docker://…` on coe-hpc1, and `apptainer build x.sif <dir>` in an HPC3 `cpuqs` job (verified: about 32 min each for a 9 GB image) |

## 6. Reference implementation: AFAR_HPC

This repository applies the guide end to end. See `README.md` for details.

- **One command builds everything on coe-hpc1:** `make setup` produces a portable CPython in `.python/` (pinned in `env/python.pin`), venvs from lock files, and a ChampSim binary with static libstdc++ in `build/`.
- **Build-time vs run-time environments:**
  - `env/modules.sh`: build modules for coe-hpc1 only
  - `env/site.sh`: shared paths for both clusters, no modules
- **Checks:** `env/check.sh --runtime` checks what jobs need, from either cluster. `slurm/check.sbatch` runs it on an HPC3 CPU node, and `slurm/discover_gpu.sbatch` on an A100 node.
- **Large assets:** pinned in `configs/llm/vllm.env` and the model profiles `configs/models/*.env`, and fetched by `make llm-fetch PROFILE=<name>` into `/data/cmpe295-bond/$USER/afar`. The container is unpacked on coe-hpc1 and packed into a `.sif` by `slurm/build_sif.sbatch` on HPC3.
- **GPU jobs:**
  - `slurm/llm_serve.sh` runs vLLM inside the job, following the service pattern in section 4.
  - `slurm/openevolve.sbatch SPEC=<run spec>` runs OpenEvolve against it; the spec names the model, prompt, search settings and evaluator (`docs/run-specs.md`).
  - `AFAR_LLM_FAKE=1` tests these scripts on coe-hpc1 against a stand-in server before any GPU time is used.
- **Verified from scratch:** a fresh clone of the GitHub repo passed `make setup` on coe-hpc1 (392 s) and its CPU and GPU jobs on HPC3 (2026-10-22). `docs/status.md` has the evidence and open issues.

## Sources

- SJSU CoE HPC, user guide and access: https://www.sjsu.edu/cmpe/resources/hpc.php
- HPC Carpentry, Introduction to HPC (concepts, login vs compute nodes, schedulers): https://carpentries-incubator.github.io/hpc-intro/
- Slurm `sbatch` (export default, dependencies, arrays, `--parsable`): https://slurm.schedmd.com/sbatch.html
- Slurm job arrays: https://slurm.schedmd.com/job_array.html
- Lmod user guide (collections, `LMOD_SYSTEM_NAME`): https://lmod.readthedocs.io/en/latest/010_user.html
- Python `venv` (environments are non-portable; recreate, don't move): https://docs.python.org/3/library/venv.html
- python-build-standalone (glibc ≥ 2.17 baseline): https://gregoryszorc.com/docs/python-build-standalone/main/running.html
- Apptainer GPU support (`--nv`): https://apptainer.org/docs/user/latest/gpu.html
