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

To be filled in by `make llm-fetch`: image tag and digest, model name and revision, and the paths under `$AFAR_ASSETS`.

## Measurements (R5, R6)

To be filled in: server load time, tokens/s, and seconds per OpenEvolve iteration.
