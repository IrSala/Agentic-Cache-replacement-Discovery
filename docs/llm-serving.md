# LLM serving on COE-HPC3

How OpenEvolve's LLM is served for AFAR_HPC: a vLLM server in an Apptainer container, run as a sidecar inside the same GPU job as OpenEvolve and reached at `http://127.0.0.1:$PORT/v1`. Compute nodes have no internet, so the container and model are downloaded on coe-hpc1 into `$AFAR_ASSETS` (`/data/cmpe295-bond/$USER/afar`) beforehand.

## HPC3 GPU facts (E2, 2026-10-15)

Measured by `slurm/discover_gpu.sbatch` (job 90582, node `cs002`) and `slurm/check.sbatch` (job 90581, node `c8`).

| | |
|---|---|
| GPU partitions | `gpuqs` (2 days), `gpuqm` (7 days), `gpuql` (14 days), all sharing the same nodes |
| A100 nodes | `cs001`–`cs004`: 4× NVIDIA A100-PCIE-40GB each, 64 CPUs, 250 GB RAM |
| Other GPUs | `g16`, `g18`, `g19`: 1× H100 PCIe 80 GB each (compute capability 9.0; vLLM works, job 98572). `g1`–`g15`: 1× P100 each (compute capability 6.0, **not supported by vLLM**) |
| Request used | campaigns: `--partition=gpuqs --gres=gpu:1 --exclude=g[1-15],cs002`, an A100 or an H100 (since 2026-10-29); single jobs: `--gres=gpu:a100:1`, or `gpu:h100:1` on the command line |
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

- **Server:** vLLM's official image (`vllm/vllm-openai`), pulled into a `.sif` with Apptainer. The A100's compute capability 8.0 and the H100's 9.0 meet vLLM's requirement and bf16's (8.0). The GPU preflight refuses anything older.
- **Sizing:** a 7B-class instruct coder model in bf16 (about 15 GB of weights) fits in 40 GB with room for the KV cache.

## Pinned assets (R4)

The vLLM image is pinned in `configs/llm/vllm.env`, and each model in a profile, `configs/models/<name>.env`; a run spec names its model by profile (`docs/run-specs.md`). Before M4 both lived in `configs/llm/serve.env`, which was removed in M5. Paths are under `$AFAR_ASSETS` (`/data/cmpe295-bond/$USER/afar`).

| Asset | Pin | Location | Status |
|---|---|---|---|
| Model | `Qwen/Qwen2.5-Coder-7B-Instruct` @ `c03e6d358207e414f1eca0bb1891e29f1db0e242` (Apache-2.0) | `models/Qwen2.5-Coder-7B-Instruct@c03e6d35` | **Downloaded and verified** (2026-10-15). Took 2 min 11 s with `make llm-fetch LLM_PARTS=model`. All 14 files match the Hub's sizes, and the 4 safetensors match its sha256. |
| Model (profile `qwen3.6-27b-fp8-v1`) | `Qwen/Qwen3.6-27B-FP8` @ `a56219b8202864dc6d3e565be5ff1eea01f54cb4` (Apache-2.0) | `models/Qwen3.6-27B-FP8@a56219b8` | **Downloaded and verified** (2026-11-14). Took 4 min 29 s with `make llm-fetch PROFILE=qwen3.6-27b-fp8-v1 LLM_PARTS=model`. All 80 files match the Hub's sizes, and the 67 LFS files, including the 66 safetensors (30.9 GB), match its sha256. Served on an A100 in job 114550 (Measurements), then on campaign prompts on an A100 (115959) and an H100 (115990) (R8). |
| vLLM image | `vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40` (v0.30.0-x86_64, CUDA 13.0.2) | `containers/vllm-openai_v0.30.0-x86_64.sif` | **Built and verified** (2026-10-16): 8,006,660,096 bytes, sha256 `40e8def6149a1d8924361f079ddf5a76b6ca8d53d518d550acc39a8e7f355584`. `import vllm` reports 0.30.0. |

**Sharing one copy:** jobs only read these two paths, and the model is mounted read-only (`--bind …:/model:ro`).
- **Permissions:** `/data/cmpe295-bond` is open only to the group, and the files are group-readable but not group-writable. Other members can set `AFAR_ASSETS=/data/cmpe295-bond/011383814/afar` and skip `make llm-fetch` and `slurm/build_sif.sbatch`.
- **Verifying a copy:** compare `sha256sum` of the `.sif` with `VLLM_SIF_SHA256` in `vllm.env`, and check that the model's `.complete` marker names the pinned revision.
- **Not yet tested from another account.** `make check` still requires write access (`docs/status.md`, issue 7).

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

`AFAR_LLM_FAKE=1`, or the model profile `fake-v1` (`LLM_SERVER=fake`), swaps in `scripts/fake_llm_server.py`, so job scripts can be tested on coe-hpc1.

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

**R5 on an H100** (job 98572, 2026-10-29, `g19`, the same image, model and settings, `--gres=gpu:h100:1`):

| | H100 PCIe 80 GB | A100 (R5, above) |
|---|---|---|
| Preflight | compute capability 9.0, no ECC errors, 78.7 of 79.2 GiB free; driver 590.44.01, the same as the A100 nodes | |
| Start to healthy | 100 s (weights 9.6 s, engine init 29.1 s, of which compilation 11.5 s; FLASH_ATTN backend) | 115 s |
| GPU memory in use | 72,087 MiB of 81,559 | 35,829 MiB of 40,960 |
| KV cache | 995,472 tokens: 60.8 requests at 16,384 tokens each | 346,928 tokens: 21.2 |
| One request via OpenEvolve's `LLMEnsemble` | 0.21 s, 14 completion tokens | 0.27 s |
| 8 concurrent requests | 112 completion tokens in 0.16 s | 0.20 s |

All 10 requests returned `200`. Campaign generation jobs have taken an A100 or an H100 since this test (`docs/status.md`, issue 14).

**Qwen3.6-27B FP8 on an A100** (2026-11-14, `slurm/llm_ping.sbatch` with `PROFILE=qwen3.6-27b-fp8-v1`, the same image and `--max-model-len 16384 --gpu-memory-utilization 0.90`):

| Job | Node / GPU | Result |
|---|---|---|
| 114320 | `cs001`, GPU 0 | **Failed at start-up.** The weights loaded (28.02 GiB in 17.4 s). vLLM then set aside a test cache of one block per request it may run at once, before sizing the real cache. This model's blocks are 784 tokens and about 49 MiB, so the default 256 requests needed 12.25 GiB, with 10.63 GiB free: `torch.OutOfMemoryError`. The profile now sets `--max-num-seqs 32` (1.5 GiB). |
| 114550 | `cs004`, GPU 0 | **Passed.** The preflight found no ECC errors and 39.1 of 39.5 GiB free. All 10 requests returned `200 OK`. |

| | Qwen3.6-27B FP8 (114550) | Qwen2.5-Coder-7B bf16 (R5) |
|---|---|---|
| Start to healthy | 225 s: weights 16.5 s (22.2 s with set-up), engine init 127.5 s, of which compilation 35.0 s | 115 s |
| Weights on the GPU | 28.02 GiB; FP8 runs weight-only through Marlin (`MarlinFP8ScaledMMLinearKernel`), since an A100 has no FP8 units | about 15 GB |
| GPU memory in use | 34,295 MiB of 40,960 | 35,829 MiB |
| KV cache | 4.68 GiB, 58,861 tokens: **3.59 requests at 16,384 tokens each**. Blocks are 784 tokens, so that the 16 attention layers' pages match the 48 DeltaNet layers' state | 346,928 tokens: 21.2 |
| One request via OpenEvolve's `LLMEnsemble` | 0.49 s, 11 completion tokens | 0.27 s, 14 |
| 8 concurrent requests | 128 completion tokens in 0.56 s | 112 in 0.20 s |

The replies were short, so these timings show the server works; they don't measure generation speed. The settings took effect:
- **Thinking was off:** the reply was the function itself, in 11 tokens.
- **Sampling:** vLLM's defaults became `temperature 1.0, top_k 20, top_p 0.8`, which is the checkpoint's defaults with the profile's `top_p`. OpenEvolve sends its own temperature (0.7) with each request.

**What it means for a campaign:** the pilot's generation jobs sent their 24 requests as one batch, and `crc2-evolve-c2` sends 42.
- **The pilot's peak:** in job 97960 (`crc2-evolve-c1`, generation 10), the 24 requests held 18.6% of the 7B's cache, about 64,500 tokens, after prefix caching (63% hits). That's more than this model's whole cache of 58,861 tokens, so a batch won't all run at once; vLLM queues the rest.
- **Measured in R8 (below):** on an A100, a pilot batch took 527 s, against the 7B's 33 s. On an H100, whose cache is 8.6 times as large and which runs FP8 natively, pilot batches took 48–80 s.

**R6** (job 93362, same server settings, OpenEvolve's real prompts, one request at a time):

| | |
|---|---|
| Start to healthy | 75 s |
| Prompt | 1,352 tokens |
| Reply | about 779 completion tokens: the whole program plus an explanation |
| Generation speed | 79.5 tokens/s (vLLM's own log) |
| Seconds per iteration | 10.3 wall clock, nearly all LLM time (no candidate was evaluated) |
| KV cache in use | 0.4% |

**Host memory:** the GPU jobs request `--mem=24G`.
- **Measured:** `sacct` MaxRSS was 3.1 GB for llm-ping (93355), 4.1–4.2 GB for the toy runs (93369, 93386) and 4.2 GB for R7 (93458).
- **Why it's low:** the weights and KV cache live on the GPU. The model files read from `/data` sit in the page cache, which the kernel can reclaim and MaxRSS doesn't count.
- **Why it matters:** the original 64G request kept job 93491 waiting for hours (`Resources`). The A100 nodes with a free GPU had only 27–59 GB of unallocated memory left, while CPUs and GPUs were free. With 24G, job 93495 started at once on `cs001`.
- **Margin:** 24G is about 6× the peak, because Slurm samples memory only every 30 s or so.

## OpenEvolve with the real model (R6)

R6 ran `slurm/openevolve_toy.sbatch`, which ran `openevolve-run` on the function_minimization toy against the sidecar and wrote `runs/toy-<jobid>`. Since M4 the same run is `slurm/openevolve.sbatch SPEC=toy-v1`; the old script was removed in M5. The job:
- It logs at DEBUG, so every raw reply is kept, including replies whose edits fail to apply.
- It forwards the time-limit SIGTERM to OpenEvolve, which then saves `best/`.
- It checks that the API key appears in no log.
- It ends with `scripts/oe_run_summary.py` (or `make oe-summary RUN=<run dir>`).

With `AFAR_LLM_FAKE_REPLY=<file>`, the fake server answers with a recorded reply, so a real model's output can be replayed through OpenEvolve on coe-hpc1.

OpenEvolve 0.4.0 behaviour behind `configs/openevolve/toy.yaml`:
- **Checkpoints:** interval checkpoints are written only after a successful iteration, and none on SIGTERM. So the toy checkpoints every iteration.
- **Seed:** a top-level `random_seed` (default 42) becomes one fixed seed on every LLM request. vLLM honours it, so an unchanged prompt gets an unchanged reply. The toy sets `random_seed: null` and seeds only the database's sampling.
- **Edit format:** the 7B model doesn't follow the SEARCH/REPLACE diff format (job 93362, below). The toy uses full rewrites (`diff_based_evolution: false`).
- **EVOLVE-BLOCK enforcement doesn't work in 0.4.0.** `enforce_evolve_blocks: true` is accepted but never reaches the workers. `_serialize_config` (`process_parallel.py:505-525`) builds their config from a fixed key list that leaves it out, and the workers are where it's applied (`process_parallel.py:330`).
  - Upstream `main` now forwards every `Config` field, but no release has the fix yet (0.4.0 is the latest on PyPI).
  - In jobs 93369 and 93386, 10 of the 18 evolved programs lost their markers. All 10 changed the fixed `run_search` entry point that the evaluator calls, including the best program (iteration 2). None changed the objective function. Once a parent has no markers, OpenEvolve can't enforce anything on its children.
  - Until a release has the fix, the evaluator must reject edits outside the EVOLVE-BLOCK itself. For R7 that means the `invalid_candidate` status in `docs/experiment-contract.md`.

### R6 attempts

| Job | Node / GPU | Result |
|---|---|---|
| 93362 (2026-10-21) | `cs003`, GPU 0 (bus `00000000:17:00.0`) | **Server and OpenEvolve worked together; the edits did not apply.**<br>**What worked:** the preflight was clean, and the CLI used the job's port and `afar-llm`. All 10 requests returned `200`, there was no key in any log, `checkpoint_10` and `best/` were saved, and the job exited 0.<br>**What failed:** 0 of 10 iterations applied ("No valid diffs found in response"). Every reply was a full rewrite in a fenced code block, despite the prompt's diff instructions and example. Replies 2–10 were byte-identical: nothing was added, so the prompt never changed, and every request carried the same seed (191216760).<br>**Offline check:** OpenEvolve's full-rewrite parser accepted both distinct replies, and the toy evaluator scored them 1.34 and 1.47. For comparison, the initial program scored 1.45 locally and 1.21 in the job, because the evaluator is unseeded. Replaying reply 2 locally in full-rewrite mode applied and scored it.<br>**Fix:** full rewrites and no LLM seed. `enforce_evolve_blocks` was turned on too, but it later proved to have no effect in 0.4.0 (above). |
| 93369 (2026-10-21) | `cs003`, GPU 0 | **Passed** (full rewrites, no LLM seed).<br>**Edits:** 9 of 10 replies applied; the other returned its parent unchanged. All 10 replies differed, and no request carried a seed. Parents included evolved programs from iteration 5 on.<br>**Scores:** the best was 1.4887 (initial program 1.2596 in this run). Of the 9 applied candidates, only 2 scored. One imported scipy, which isn't in the venv. Six timed out: one at the evaluator's own 5 s limit, and five at OpenEvolve's 60 s stage-1 limit.<br>**Plumbing:** checkpoints were saved after each success plus `checkpoint_10`, all 10 requests returned `200`, there was no key in any log, and the job exited 0. |
| 93386 (2026-10-21) | `cs003`, GPU 0 | **Resume passed** (`--export=ALL,CHECKPOINT=runs/toy-93369/checkpoints/checkpoint_10,ITERATIONS=10`).<br>**Resume:** the log shows "Checkpoint loaded successfully (iteration 10)", "Resuming from checkpoint at iteration 11", and "Skipping initial program addition (… 10 existing programs)". Iterations 11–20 ran and `checkpoint_20` has `last_iteration` 20 and 19 programs. The best program (1.4887, found at iteration 2) carried over.<br>**Edits:** 9 of 10 applied, building on both loaded and new programs, but none scored. Eight timed out at 60 s; for example, one cooled `while temp > 1e-10` with an inner loop at every temperature, millions of numpy calls in plain Python. One imported scipy.<br>**Plumbing:** all 10 requests returned `200`, there was no key in any log, and the job exited 0. |

**Why candidates hit the 60 s limit:** the toy evaluator (upstream's) runs each candidate in a thread under `with ThreadPoolExecutor(...)`, with a 5 s `future.result` timeout. Leaving that block waits for the thread, so a slow candidate is never stopped, and OpenEvolve's stage-1 limit ends it instead. This is a property of the toy, not of the serving setup. R7's evaluator avoids it by compiling in a subprocess whose whole process group it kills on timeout (`docs/champsim-evaluator.md`).

**R6 timings** (from `scripts/oe_run_summary.py`):

| | Job 93369 (iterations 1–10) | Job 93386 (11–20) |
|---|---|---|
| Wall seconds per iteration | 43.8 | 64.7 |
| LLM per iteration | about 12.3 s (mean 966 completion tokens at about 80 tokens/s) | about 16.8 s (mean 1,264 tokens) |
| Evaluation per applied candidate | mean 34.9 s. Scored candidates took 0.5–7.4 s; the rest is 60 s timeouts. | mean 53.4 s (8 of 9 timed out) |
| Largest prompt | 4,243 tokens | 7,204 tokens (+ 4,096 `max_tokens` ≤ 16,384) |

## Campaign prompts replayed (R8)

`controller/llm_replay.py` sends a campaign's recorded prompts to another model profile, a whole generation at a time, as a generation job sends them. Then it compares the replies with the recorded ones, so two models are compared on the same prompts. The comparison of what the replies are worth is in `docs/levers/generation/model.md`; this section covers serving.

- **Run on HPC3:** `sbatch --export=ALL,PROFILE=<profile>,REPLAY=<campaign>:<gens>+… --exclude=cs002 slurm/llm_replay.sbatch` (an A100 by default; `--gres=gpu:h100:1` for an H100). `REPLAY` joins its sources with `+`, because `--export` splits on commas. It takes a generation job's resources (4 CPUs, 24G) and 1 hour by default; a `--time` on the command line overrides it.
- **Settings:** each request gets its source campaign's temperature and `max_tokens`, which must equal the profile's, or the run is refused. The timeout is 3,600 s with no retries, so a request's whole latency is measured. Under a campaign's settings, a request past the profile's timeout would have been cancelled and resent.
- **Records:** `$AFAR_RESULTS/llm-replay/<profile>-<job>/`
  - `run.json`: the profile, model, server arguments, settings, and each job's node, GPU and commit
  - `batches/<campaign>-g<N>.json`: every reply with its tokens and seconds, the batch's seconds, the source's recorded LLM time, and vLLM's `/metrics` counters before and after
  - `OUT=<dir>` adds to an existing replay of the same profile and skips the batches it already has.
- **Check on coe-hpc1:** `make llm-replay-check R=<profile>-<job>` writes `check.json` and prints the funnel, the paired counts and a timing table per batch.
  - Peak running and waiting requests, KV cache use and generation throughput come from vLLM's 10-second stats lines in `logs/vllm-<job>.log`, within the batch's own window.
  - Preemptions and prefix-cache hits come from the `/metrics` counters, since vLLM's V1 engine doesn't log preemptions.
  - On a compile failure, `check.json` keeps gcc's first error.
- **Self-test:** `make llm-replay-selftest` (seconds, a stand-in model).

**Qwen3.6-27B FP8 on 324 recorded prompts** (2026-11-14 and 15, profile `qwen3.6-27b-fp8-v1`, thinking off). The prompts are `crc2-evolve-c1` generations 1–10 (24 each) and `crc2-evolve-c2-screen-test` generations 1–2 (42 each); both jobs wrote to `qwen3.6-27b-fp8-v1-115959`.

| Job | Node / GPU | Batches |
|---|---|---|
| 115959 | `cs003`, A100-PCIE-40GB | `crc2-evolve-c1` generation 10; `crc2-evolve-c2-screen-test` generation 2 |
| 115990 | `g19`, H100 PCIe 80 GB (queued 18 min) | `crc2-evolve-c1` generations 1–9; `crc2-evolve-c2-screen-test` generation 1 |

| | 27B, A100 (115959) | 27B, H100 (115990) | 7B, A100 (recorded) |
|---|---|---|---|
| Start to healthy | 255 s | 315 s: weights 33.5 s, engine init 206.1 s, of which compilation 28.0 s | 115 s (R5) |
| FP8 | weight-only, through Marlin | native (`FlashInferFp8DeepGEMMDynamicBlockScaledKernel`) | bf16 |
| GPU memory in use | 34,295 MiB of 40,960 | 70,599 MiB of 81,559 | 35,829 MiB |
| KV cache | 58,861 tokens | 39.77 GiB, 504,263 tokens: 30.8 requests at 16,384 | 346,928 tokens |
| A pilot batch (24 prompts) | 527 s (generation 10) | 48–80 s (generations 1–9; median 62) | 18–36 s (generations 1–10) |
| A c2 test batch (42 prompts) | 363 s (generation 2) | 50 s (generation 1) | 33 and 41 s |
| Slowest request | 525 s | 80 s | |
| Requests over the profile's 300 s timeout | 25 of 66 | 0 of 258 | |
| Peak requests running | 8 (pilot), 10 (c2) | 24 (pilot); 32, the profile's `--max-num-seqs` (c2), so 10 waited | 24 (job 97960, pilot generation 10) |
| Peak KV cache use | 99–100% | 15–31% | 18.6% |
| Preemptions | 235 and 114 | 0 | |
| Prompt tokens served from the prefix cache | 7% and 4% | 32–82% | 63% |
| Peak generation throughput | 242–297 tokens/s | 811–1,139 tokens/s | 1,297 tokens/s |

All 324 requests returned, with no errors.

**What happened on the A100:**
- A pilot request holds about 6,900 prompt tokens plus its reply, so the 58,861-token cache ran about 8 at once and the rest queued.
- vLLM kept preempting running requests to make room. A preempted request's cache is dropped and its prompt computed again later, which also evicted the prompts' shared prefixes.

**What it means for a campaign:**
- **GPU time is a minor cost.** Evaluation took 46–120 minutes per pilot generation, and the 7B's generation step about 2 minutes (queue, start-up and replies). A slower model that gives better candidates per generation is worth it.
- **On an H100,** the 27B's generation step would take about 6.5 minutes, with every request far below the 300 s timeout.
- **On an A100,** it would take about 13–14 minutes, and half of a batch's requests run past 300 s.
  - OpenEvolve cancels a request at its timeout and sends it again, up to 3 times, 5 s apart, throwing away the partial reply. If all 4 attempts time out, that prompt gives nothing.
  - So a campaign that runs the 27B on A100s needs a profile whose timeout never fires, for example 1,800 s, and a generation job longer than 30 minutes. That would be a new `-v2` profile, since a profile doesn't change once a run has used it.
- **Availability:** `gpuqs` has 3 H100s (`g16`, `g18`, `g19`) and 12 working A100s. Generation jobs take either (issue 14).
