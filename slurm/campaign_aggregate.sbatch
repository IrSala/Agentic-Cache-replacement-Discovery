#!/bin/bash
# M5: the aggregation stage of a staged campaign (controller/campaign.py
# aggregate): retry missing evaluations, or add the generation to OpenEvolve's
# database, checkpoint it, and submit the next generation (or finish, or halt).
# Submitted by the campaign itself, after its evaluation array (afterany).
#
#SBATCH --job-name=afar-aggregate
#SBATCH --partition=cpuqs
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:20:00
#SBATCH --signal=B:TERM@60

set -euo pipefail
cd "${AFAR_REPO:?}"
source env/site.sh
echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) generation=${AFAR_GENERATION:?}"
.venv/controller/bin/python -m controller.campaign aggregate --campaign "${AFAR_CAMPAIGN:?}" --generation "$AFAR_GENERATION" &
wait $!
