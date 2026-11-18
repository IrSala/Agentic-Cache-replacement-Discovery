#!/bin/bash
# R8: replay campaigns' recorded prompts against a model profile on one GPU
# (controller/llm_replay.py, docs/llm-serving.md). Each source generation is one batch,
# sent at once as a generation job sends it, on a generation job's resources (4 CPUs, 24G).
# Afterwards, compare the replies on coe-hpc1: make llm-replay-check R=<profile>-<jobid>
#
# Submit from ~/AFAR_HPC on coe-hpc3 (g17), checking the request first:
#     sbatch --test-only --export=ALL,PROFILE=qwen3.6-27b-fp8-v1,REPLAY=crc2-evolve-c1:10 --exclude=cs002 slurm/llm_replay.sbatch
#     sbatch --export=ALL,PROFILE=qwen3.6-27b-fp8-v1,REPLAY=crc2-evolve-c1:10+crc2-evolve-c2-screen-test:2 --exclude=cs002 slurm/llm_replay.sbatch
# REPLAY lists <campaign>:<generation> or <campaign>:<first>-<last>, joined by + (--export
# splits on commas), in the order they're sent. OUT=<dir> continues an earlier replay of the same profile, skipping the
# batches it already wrote. On an H100 instead, add --gres=gpu:h100:1 (g16, g18 or g19; not nsfq).
#
# Logs: logs/llm-replay-<jobid>.out (this script) and logs/vllm-<jobid>.log (server).
# Output: $AFAR_RESULTS/llm-replay/<profile>-<jobid>/, or OUT.
#
# Test without a GPU or Slurm on coe-hpc1, against a stand-in server answering with recorded replies:
#     SLURM_JOB_ID=12345 SLURM_SUBMIT_DIR=$PWD PROFILE=fake-v1 AFAR_LLM_FAKE_REPLY_DIR=<dir> \
#         REPLAY=crc2-evolve-c1:10 OUT=<scratch dir> bash slurm/llm_replay.sbatch
#
#SBATCH --job-name=llm-replay
#SBATCH --partition=gpuqs
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=24G
#SBATCH --time=01:00:00
#SBATCH --signal=B:TERM@60              # lets the trap stop the server before the time limit
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh
source configs/llm/vllm.env
profile="${PROFILE:?set PROFILE=<model profile>}"
source "configs/models/${profile}.env"
source slurm/llm_serve.sh
: "${REPLAY:?set REPLAY=<campaign>:<generations>[+...]}"
out="${OUT:-${AFAR_RESULTS}/llm-replay/${profile}-${SLURM_JOB_ID}}"

echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)$(git diff --quiet HEAD 2>/dev/null || echo -dirty) profile=${profile} replay=${REPLAY}"
if ! llm_is_fake; then
    echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader -i "$CUDA_VISIBLE_DEVICES"
fi

llm_gpu_preflight
llm_is_fake || export AFAR_GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader -i "$CUDA_VISIBLE_DEVICES")"
llm_start
llm_wait_ready 900
llm_is_fake || nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader -i "$CUDA_VISIBLE_DEVICES"

sources=()
IFS='+,' read -ra parts <<< "$REPLAY"
for s in "${parts[@]}"; do
    sources+=(--source "$s")
done
# In the background with wait, so a SIGTERM from --signal reaches the trap promptly.
.venv/controller/bin/python -m controller.llm_replay run "${sources[@]}" --profile "$profile" \
    --api-base "$AFAR_LLM_API_BASE" --out "$out" --vllm-log "$AFAR_LLM_LOG" &
wait $!
if grep -rsqF -f <(printf '%s\n' "$AFAR_LLM_API_KEY") "$out"; then
    echo "WARNING: the API key appears in ${out}" >&2
fi
echo "done"
