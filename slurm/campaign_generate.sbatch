#!/bin/bash
# M5: the generation stage of a staged campaign (controller/campaign.py,
# docs/campaigns.md). Serves the campaign's model on one A100, writes one
# generation of candidates, and submits their evaluation array and aggregation.
# Submitted by the campaign itself (make campaign-start, then each aggregation),
# with AFAR_CAMPAIGN and AFAR_GENERATION set and resources on the command line.
#
#SBATCH --job-name=afar-generate
#SBATCH --partition=gpuqs
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=24G
#SBATCH --time=00:30:00
#SBATCH --signal=B:TERM@60

set -euo pipefail
cd "${AFAR_REPO:?}"
source env/site.sh
py=.venv/controller/bin/python
campaign="${AFAR_CAMPAIGN:?}" generation="${AFAR_GENERATION:?}"
echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null) campaign=${campaign} generation=${generation}"

# Exit 3 means this node looks unusable, so the job's handler keeps the restart off it
# (controller/campaign.py handle); after any other failure the restart may run anywhere.
node_fault=3

# Later stages are submitted from this node, so check that it can (M0 checked only c20).
sbatch --test-only --partition=cpuqs --wrap=true >/dev/null 2>&1 || { echo "this node can't submit jobs (sbatch --test-only failed)" >&2; exit $node_fault; }

if "$py" -m controller.campaign generate --campaign "$campaign" --generation "$generation" --needs-llm; then
    source configs/llm/vllm.env
    source "$("$py" -m controller.campaign generate --campaign "$campaign" --generation "$generation" --model-profile)"
    source slurm/llm_serve.sh
    llm_gpu_preflight || exit $node_fault
    llm_start || exit $node_fault
    llm_wait_ready 900 || exit $node_fault
    api=(--api-base "$AFAR_LLM_API_BASE")
else
    api=()      # already generated, stopped, or not the current stage: no model needed
fi
# In the background with wait, so a SIGTERM from --signal reaches this shell promptly.
"$py" -m controller.campaign generate --campaign "$campaign" --generation "$generation" ${api[@]+"${api[@]}"} &
wait $!
if [[ -n "${AFAR_LLM_API_KEY:-}" ]] && grep -rqF -f <(printf '%s\n' "$AFAR_LLM_API_KEY") "${campaign}/generations/${generation}" "${campaign}/resolved"; then
    echo "WARNING: the API key appears in ${campaign}/generations/${generation} or ${campaign}/resolved" >&2
fi
echo "done"
