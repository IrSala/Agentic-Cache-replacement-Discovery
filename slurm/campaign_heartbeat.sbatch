#!/bin/bash
# M6: a campaign's heartbeat (controller/campaign.py heartbeat, docs/campaigns.md). It
# submits its successor first, heartbeat_minutes later (at least 60 outside test specs),
# then restarts the chain if it has no live job. One job name per campaign plus
# --dependency=singleton means two never run at once, and the chain ends with the campaign.
# About 1 CPU for 15 s per run. To remove it: scancel --name=afar-heartbeat-<campaign id>
#
#SBATCH --job-name=afar-heartbeat
#SBATCH --partition=cpuqs
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:05:00

set -euo pipefail
cd "${AFAR_REPO:?}"
source env/site.sh
echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) campaign=${AFAR_CAMPAIGN:?}"
.venv/controller/bin/python -m controller.campaign heartbeat --campaign "$AFAR_CAMPAIGN" &
wait $!
