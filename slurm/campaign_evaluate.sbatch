#!/bin/bash
# M5: the evaluation stage of a staged campaign: one array task per candidate
# (controller/campaign.py evaluate-task), on the run spec's cpus_per_candidate.
# Exit status 0 for any candidate outcome, 1 for an infrastructure failure, which
# the aggregation retries on other nodes. Submitted by the campaign itself.
#
#SBATCH --job-name=afar-evaluate
#SBATCH --partition=cpuqs
#SBATCH --signal=B:TERM@60

set -euo pipefail
cd "${AFAR_REPO:?}"
source env/site.sh
export AFAR_CXX="${AFAR_CXX:-/usr/bin/g++}"
echo "job=${SLURM_ARRAY_JOB_ID:-}_${SLURM_ARRAY_TASK_ID:-} node=$(hostname) date=$(date -Is) generation=${AFAR_GENERATION:?} task=${SLURM_ARRAY_TASK_ID:?}"
.venv/controller/bin/python -m controller.campaign evaluate-task --campaign "${AFAR_CAMPAIGN:?}" \
    --generation "$AFAR_GENERATION" --index "$SLURM_ARRAY_TASK_ID" &
wait $!
