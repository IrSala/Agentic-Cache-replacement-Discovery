#!/bin/bash
# M4: one OpenEvolve run, assembled from a run spec (configs/runs/<SPEC>.yaml).
# The spec names the seed, model profile, prompt profile, search profile and
# evaluator (docs/run-specs.md); this script assumes none of them. The model is
# served by vLLM as a sidecar on one A100 (slurm/llm_serve.sh), and candidates
# are evaluated on this node's CPUs (executor `local`).
#
# Check the spec on coe-hpc1 first, then submit from ~/AFAR_HPC on coe-hpc3 (g17):
#     make oe-check SPEC=<name>
#     sbatch --test-only --export=ALL,SPEC=<name> slurm/openevolve.sbatch
#     sbatch --export=ALL,SPEC=<name> slurm/openevolve.sbatch
# Continue a run in its own directory from its newest checkpoint, e.g. after
# it reached the time limit (the spec and its profiles must be unchanged):
#     sbatch --export=ALL,SPEC=<name>,RESUME=runs/<name>-<jobid> slurm/openevolve.sbatch
# CHECKPOINT=<dir> instead starts a new run from that checkpoint. ITERATIONS=N
# overrides the spec's count; after a checkpoint it counts from there.
# Resources are the #SBATCH lines below; override them with sbatch options. The
# spec's parallel_candidates x cpus_per_candidate must fit the CPUs, less 2.
# To avoid a node with a faulty GPU, add --exclude=<node>.
#
# Logs: logs/openevolve-<jobid>.out (this script and the summary),
# logs/vllm-<jobid>.log (server), <run>/logs/ (OpenEvolve, DEBUG level so every
# LLM reply is kept), <run>/evaluations.jsonl (one line per evaluation).
#
# Test on coe-hpc1 without a GPU or Slurm (AFAR_LLM_FAKE=1 serves any profile
# with the fake server; C++ specs need Spack g++ 12, since the default g++ 7.3 lacks <charconv>):
#     AFAR_LLM_FAKE=1 AFAR_CXX=<path to g++ 12> SLURM_JOB_ID=12345 SLURM_SUBMIT_DIR=$PWD \
#         SLURM_CPUS_PER_TASK=16 SPEC=<name> bash slurm/openevolve.sbatch
#
#SBATCH --job-name=openevolve
#SBATCH --partition=gpuqs
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=16        # a quarter of a 64-CPU, 4-GPU node
#SBATCH --mem=32G                 # OpenEvolve and the server peaked at ~4.2 GB (R5-R7); the rest is for builds and simulations
#SBATCH --time=01:00:00
#SBATCH --signal=B:TERM@60        # lets the traps stop OpenEvolve and the server before the time limit
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh

export AFAR_CXX="${AFAR_CXX:-/usr/bin/g++}"
py=.venv/controller/bin/python
spec="${SPEC:?set SPEC=<run spec name in configs/runs/, or a path>}"
cpus="${SLURM_CPUS_PER_TASK:-1}"
if [[ -n "${RESUME:-}" ]]; then
    run="${RESUME%/}"
    CHECKPOINT="$(ls -d "$run"/checkpoints/checkpoint_* 2>/dev/null | sort -t_ -k2 -n | tail -1)"
    [[ -n "$CHECKPOINT" ]] || { echo "RESUME: no checkpoint under ${run}/checkpoints" >&2; exit 1; }
else
    run="runs/$(basename "${spec%.yaml}")-${SLURM_JOB_ID}"
fi
echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)$(git diff --quiet HEAD 2>/dev/null || echo -dirty)"
echo "spec=${spec} run=${run} cpus=${cpus} CHECKPOINT=${CHECKPOINT:-none} ITERATIONS=${ITERATIONS:-from spec}"
echo "compiler: AFAR_CXX=${AFAR_CXX} ($("$AFAR_CXX" --version | head -1))"

# 1. Validate the spec and every part it names, in seconds and before any GPU
#    work; it prints the parts to the log and the job's variables (AFAR_RUN_*) here.
vars="$("$py" -m controller.runspec --check "$spec" --cpus "$cpus" --shell)"
eval "$vars"

# 2. The evaluator policy's own check of this node (compiler, prebuilt tree, traces).
if [[ -n "$AFAR_RUN_POLICY" ]]; then
    "$py" evaluator/openevolve_adapter.py --preflight --policy "$AFAR_RUN_POLICY" \
        --experiment "$AFAR_RUN_EXPERIMENT" --seed "$AFAR_RUN_SEED" --jobs "$AFAR_RUN_CPUS_PER_CANDIDATE" &
    wait $!
fi

# 3. The model server, from the spec's model profile.
source configs/llm/vllm.env
source "$AFAR_RUN_MODEL_PROFILE"
source slurm/llm_serve.sh
llm_gpu_preflight
llm_start
llm_wait_ready 900

# 4. The resolved config, now that the server's address is known; run.json
#    records the parts and this job.
"$py" -m controller.runspec --resolve "$spec" --run-dir "$run" --api-base "$AFAR_LLM_API_BASE" --cpus "$cpus"
export AFAR_RUN_JSON="${PWD}/${run}/run.json"

# 5. OpenEvolve, in the background with wait, so a SIGTERM from --signal reaches
#    the trap promptly. --signal=B: signals only this shell, so the trap passes it
#    on: OpenEvolve then stops its workers and saves best/ before the server stops.
.venv/controller/bin/openevolve-run "$AFAR_RUN_SEED" "$AFAR_RUN_EVALUATOR_FILE" \
    --config "${run}/resolved/config.yaml" --output "$run" --log-level DEBUG \
    ${ITERATIONS:+--iterations "$ITERATIONS"} ${CHECKPOINT:+--checkpoint "$CHECKPOINT"} &
oe_pid=$!
trap 'echo "job: received SIGTERM, stopping OpenEvolve"; kill -TERM "$oe_pid" 2>/dev/null || true; wait "$oe_pid" || true; exit 143' TERM
wait "$oe_pid"

# DEBUG logs hold every request, so check the key did not end up in them. The
# key reaches grep through a pipe (printf is a builtin), never a command line.
if grep -rqF -f <(printf '%s\n' "$AFAR_LLM_API_KEY") "$run/logs" "$run/resolved" "$AFAR_LLM_LOG"; then
    echo "WARNING: the API key appears in ${run}/logs, ${run}/resolved or ${AFAR_LLM_LOG}" >&2
fi

"$py" scripts/oe_run_summary.py "$run"
echo "done"
