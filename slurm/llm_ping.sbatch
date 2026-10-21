#!/bin/bash
# R5: serve the pinned model with vLLM on one A100 and check it through
# OpenEvolve's client (slurm/llm_serve.sh, scripts/llm_ping.py).
#
# Submit from ~/AFAR_HPC on coe-hpc3 (g17), checking the request first:
#     sbatch --test-only slurm/llm_ping.sbatch
#     sbatch slurm/llm_ping.sbatch
# To avoid a node with a faulty GPU, add --exclude=<node> to both.
#
# Logs: logs/llm-ping-<jobid>.out (this script) and logs/vllm-<jobid>.log (server).
#
# Test without a GPU or Slurm on coe-hpc1, against a stand-in server:
#     SLURM_JOB_ID=12345 SLURM_SUBMIT_DIR=$PWD AFAR_LLM_FAKE=1 bash slurm/llm_ping.sbatch
#
#SBATCH --job-name=llm-ping
#SBATCH --partition=gpuqs
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=00:30:00
#SBATCH --signal=B:TERM@60              # lets the trap stop the server before the time limit
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh
source configs/llm/serve.env
source slurm/llm_serve.sh

echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)"
if [[ "${AFAR_LLM_FAKE:-0}" != 1 ]]; then
    echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
    apptainer --version
    apptainer exec --nv "${AFAR_ASSETS}/containers/${VLLM_SIF}" vllm --version
fi

llm_gpu_preflight
llm_start
llm_wait_ready 900
if [[ "${AFAR_LLM_FAKE:-0}" != 1 ]]; then
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
fi

# In the background with wait, so a SIGTERM from --signal reaches the trap promptly.
.venv/controller/bin/python scripts/llm_ping.py --config configs/openevolve/toy.yaml --api-base "$AFAR_LLM_API_BASE" &
wait $!
echo "done"
