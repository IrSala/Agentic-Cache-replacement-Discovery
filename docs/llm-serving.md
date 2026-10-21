# LLM serving on COE-HPC3

How OpenEvolve's LLM is served for AFAR_HPC: a vLLM server in an Apptainer container, run as a sidecar inside the same GPU job as OpenEvolve and reached at `http://127.0.0.1:$PORT/v1`. Compute nodes have no internet, so the container and model are downloaded on coe-hpc1 into `$AFAR_ASSETS` (`/data/cmpe295-bond/$USER/afar`) beforehand.

## HPC3 GPU facts (E2, 2026-10-15)

Measured by `slurm/discover_gpu.sbatch` (job 90582, node `cs002`) and `slurm/check.sbatch` (job 90581, node `c8`).

| | |
|---|---|
| GPU partitions | `gpuqs` (2 days), `gpuqm` (7 days), `gpuql` (14 days), all sharing the same nodes |
| A100 nodes | `cs001`–`cs004`: 4× NVIDIA A100-PCIE-40GB each, 64 CPUs, 250 GB RAM |
| Other GPUs | `g16`, `g18`, `g19`: 1× H100 each. `g1`–`g15`: 1× P100 each (compute capability 6.0, **not supported by vLLM**) |
| Request used | `--partition=gpuqs --gres=gpu:a100:1` |
| A100 compute capability | 8.0 |
| GPU memory | 40960 MiB |
| Driver / CUDA | 590.44.01 / CUDA 13.1 (runs CUDA 12.x containers) |
| OS | Rocky Linux 9.8, glibc 2.34, kernel 5.14 |
| Node-local SSD | `/tmp` (= `$TMPDIR`), 371 GB |
| Shared storage | `/home`, `/data`, `/scratch` mounted; `$AFAR_ASSETS` writable |
| Network | none (`huggingface.co` times out) |
| Apptainer | `/usr/bin/apptainer` 1.5.3 on `cs002`, 1.5.4 on `c8` and g17 (coe-hpc1 pulls with the `apptainer/1.3.2` module) |
| Compiler on GPU nodes | system gcc/g++ 11.5.0 |
| AFAR_HPC run-time checks | all pass with no modules: venvs from `.python/`, OpenEvolve 0.4.0, statically linked ChampSim |

## Serving stack

- **Server:** vLLM's official image (`vllm/vllm-openai`), pulled into a `.sif` with Apptainer. The A100's compute capability 8.0 meets vLLM's requirement.
- **Sizing:** a 7B-class instruct coder model in bf16 (about 15 GB of weights) fits in 40 GB with room for the KV cache.

## Pinned assets (R4)

Pins live in `configs/llm/serve.env`. Paths are under `$AFAR_ASSETS` (`/data/cmpe295-bond/$USER/afar`).

| Asset | Pin | Location | Status |
|---|---|---|---|
| Model | `Qwen/Qwen2.5-Coder-7B-Instruct` @ `c03e6d358207e414f1eca0bb1891e29f1db0e242` (Apache-2.0) | `models/Qwen2.5-Coder-7B-Instruct@c03e6d35` | **Downloaded and verified** (2026-10-15). Took 2 min 11 s with `make llm-fetch LLM_PARTS=model`. All 14 files match the Hub's sizes, and the 4 safetensors match its sha256. |
| vLLM image | `vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40` (v0.30.0-x86_64, CUDA 13.0.2) | `containers/vllm-openai_v0.30.0-x86_64.sif` | **Built and verified** (2026-10-16): 8,006,660,096 bytes, sha256 `40e8def6149a1d8924361f079ddf5a76b6ca8d53d518d550acc39a8e7f355584`. `import vllm` reports 0.30.0. |

### How the container is built (two steps)

A plain `apptainer pull` doesn't work on coe-hpc1. It downloads and unpacks the layers, then hangs in "Creating SIF file":
- coe-hpc1's `mksquashfs` is CentOS 7's squashfs-tools 4.3, and Apptainer runs it on all 28 cores (`mksquashfs procs = 0`).
- On this image it wrote 4.96 GB, then stayed alive with all 59 threads blocked: no CPU and no I/O for about 4 hours.

What works is Apptainer's documented sandbox-then-SIF workflow:
1. **coe-hpc1:** `make llm-fetch LLM_PARTS=sandbox` runs `apptainer build --sandbox --fix-perms` from the digest-pinned image. It took 32 min (21 GB directory on `/data`), with no `mksquashfs` involved.
2. **HPC3:** `sbatch slurm/build_sif.sbatch` runs `apptainer build <x>.sif <sandbox>` on a `cpuqs` node, using Apptainer 1.5.4 on Rocky 9.8.
   - It took 32.6 min, using the `/data` build space because the node's `/tmp` had less than 40 GB free.
   - The job then records the sha256, checks `import vllm`, and deletes the sandbox.
   - It runs on `cpuqs` because `defq` allows at most 2 CPUs per node.

The `vllm` command-line tool needs a GPU to detect: on a CPU node it fails with "Failed to infer device type". So `vllm serve` is first exercised by the GPU jobs.

## Running the server (R5)

`slurm/llm_serve.sh` runs vLLM as a sidecar inside the GPU job:
- It binds to `127.0.0.1` on a port derived from the job ID.
- It requires a random per-job API key, passed through the environment because command lines are visible to every user on the node.
- Caches go to node-local `/tmp/$USER/afar-llm` (mode 0700), and the server is stopped when the job exits.
- `llm_gpu_preflight` checks the allocated GPU first.

`AFAR_LLM_FAKE=1` swaps in `scripts/fake_llm_server.py`, so job scripts can be tested on coe-hpc1.

**Remaining exposure:** on a shared node, other users can reach `127.0.0.1`. vLLM's key protects only the `/v1`-style endpoints, and some others can run inference without it (vLLM security docs). The mitigation is that the port and key are random per job and the server lives only as long as the job.

**GPU numbering:** cs nodes don't hide unallocated GPUs from `nvidia-smi`, so all four are listed. `CUDA_VISIBLE_DEVICES`, which `--nv` passes through, is what confines vLLM to the allocated GPU. `CUDA_DEVICE_ORDER=PCI_BUS_ID` is set inside the container so that CUDA's numbering matches `nvidia-smi`'s.

### R5 attempts

| Job | Node / GPU | Result |
|---|---|---|
| 93353 (2026-10-21) | `cs002`, GPU 0 (bus `00000000:17:00.0`) | **GPU fault, not our setup.** Everything up to the first GPU call worked: `vllm --version` reported 0.30.0, the SIF ran with `--nv`, `/model` bound read-only, the flags were accepted, and `Qwen2ForCausalLM` resolved. The engine then failed on its first GPU memory query with `CUDA error: CUDA-capable device(s) is/are busy or unavailable`. E2's report shows this exact GPU with **73 volatile uncorrectable ECC errors** (GPUs 1–3: 0) and compute mode "Default". NVIDIA's A100 memory-error guide says recovery needs a GPU reset or reboot, which is an admin action. Resubmit with `--exclude=cs002` and report the GPU to HPC support. |
| 93355 (2026-10-21) | `cs003`, GPU 1 (bus `00000000:65:00.0`) | **Passed** (`--exclude=cs002`). The preflight found 0 uncorrectable ECC errors, no pending row remaps, recovery action "None", and 39.1 of 39.5 GiB free. All 10 requests returned `200 OK`: `/v1/models` plus 9 chat completions. Another user's job held 7 GB on this node's GPU 0, which confirms GPU nodes are shared. |

The server log ends each run with an `EngineDeadError` at shutdown. That is expected: our trap stops the server, vLLM's default shutdown (`mode=abort timeout=0s`) force-kills its engine process, and the API server logs this as it exits, ending in "Application shutdown complete".

## Measurements

**R5** (job 93355, one A100-PCIE-40GB, Qwen2.5-Coder-7B-Instruct bf16, `--max-model-len 16384 --gpu-memory-utilization 0.90`):

| | |
|---|---|
| Start to healthy | 115 s (engine init 33 s, of which compilation 14.8 s; the rest is container start-up and loading weights from `/data`) |
| GPU memory in use | 35,829 MiB of 40,960 |
| KV cache | 346,928 tokens: 21.2 requests at 16,384 tokens each, or about 42 at 8K |
| One request via OpenEvolve's `LLMEnsemble` | 0.27 s, 14 completion tokens |
| 8 concurrent requests | 112 completion tokens in 0.20 s. Replies were short, so this shows concurrency works; it doesn't measure throughput |

**R6:** to be filled in: seconds per OpenEvolve iteration with real prompts.
