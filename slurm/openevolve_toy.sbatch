#!/bin/bash
# R6: OpenEvolve's function_minimization toy against the pinned model, served
# by vLLM as a sidecar on one A100 (slurm/llm_serve.sh). Output goes to
# runs/toy-<jobid>, one directory per job.
#
# Submit from ~/AFAR_HPC on coe-hpc3 (g17), checking the request first:
#     sbatch --test-only slurm/openevolve_toy.sbatch
#     sbatch slurm/openevolve_toy.sbatch
# Resume from another job's checkpoint for N more iterations:
#     sbatch --export=ALL,CHECKPOINT=runs/toy-<jobid>/checkpoints/checkpoint_10,ITERATIONS=10 \
#         slurm/openevolve_toy.sbatch
# To avoid a node with a faulty GPU, add --exclude=<node>.
#
# Logs: logs/oe-toy-<jobid>.out (this script and the summary),
# logs/vllm-<jobid>.log (server), runs/toy-<jobid>/logs/ (OpenEvolve, DEBUG
# level so every LLM reply is kept, including those whose diffs failed).
#
# Test without a GPU or Slurm on coe-hpc1, against a stand-in server:
#     SLURM_JOB_ID=12345 SLURM_SUBMIT_DIR=$PWD AFAR_LLM_FAKE=1 bash slurm/openevolve_toy.sbatch
#
#SBATCH --job-name=oe-toy
#SBATCH --partition=gpuqs
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=24G                 # measured peak ~4.2 GB (sacct MaxRSS); 64G kept jobs waiting
#SBATCH --time=01:00:00
#SBATCH --signal=B:TERM@60              # lets the traps stop OpenEvolve and the server before the time limit
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh
source configs/llm/serve.env
source slurm/llm_serve.sh

run="runs/toy-${SLURM_JOB_ID}"
echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)$(git diff --quiet HEAD 2>/dev/null || echo -dirty)"
echo "run=${run} CHECKPOINT=${CHECKPOINT:-none} ITERATIONS=${ITERATIONS:-from config}"

llm_gpu_preflight
llm_start
llm_wait_ready 900

# In the background with wait, so a SIGTERM from --signal reaches the trap
# promptly. --signal=B: signals only this shell, so the trap passes it on:
# OpenEvolve then stops its workers and saves best/ before the server stops.
.venv/controller/bin/openevolve-run \
    examples/function_minimization/initial_program.py examples/function_minimization/evaluator.py \
    --config configs/openevolve/toy.yaml \
    --api-base "$AFAR_LLM_API_BASE" --primary-model "$SERVED_MODEL_NAME" \
    --output "$run" --log-level DEBUG \
    ${ITERATIONS:+--iterations "$ITERATIONS"} ${CHECKPOINT:+--checkpoint "$CHECKPOINT"} &
oe_pid=$!
trap 'echo "job: received SIGTERM, stopping OpenEvolve"; kill -TERM "$oe_pid" 2>/dev/null || true; wait "$oe_pid" || true; exit 143' TERM
wait "$oe_pid"

# DEBUG logs hold every request, so check the key did not end up in them. The
# key reaches grep through a pipe (printf is a builtin), never a command line.
if grep -rqF -f <(printf '%s\n' "$AFAR_LLM_API_KEY") "$run/logs" "$AFAR_LLM_LOG"; then
    echo "WARNING: the API key appears in ${run}/logs or ${AFAR_LLM_LOG}" >&2
fi

.venv/controller/bin/python scripts/oe_run_summary.py "$run"
echo "done"
