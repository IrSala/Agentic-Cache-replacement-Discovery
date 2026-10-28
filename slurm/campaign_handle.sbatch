#!/bin/bash
# M6: the failure handler of one generate or aggregate job (controller/campaign.py handle,
# docs/campaigns.md). Submitted with that job, with --dependency=afternotok:<job> and
# --kill-on-invalid-dep=yes, so it runs only if the job fails, and Slurm removes it if the
# job succeeds. It restarts the job a bounded number of times, or halts the campaign, which
# mails AFAR_MAIL_USER. Slurm also mails if this job itself fails (--mail-type=FAIL).
#
#SBATCH --job-name=afar-handle
#SBATCH --partition=cpuqs
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:10:00

set -euo pipefail
cd "${AFAR_REPO:?}"
source env/site.sh
echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) generation=${AFAR_GENERATION:?} stage=${AFAR_STAGE:?} failed_job=${AFAR_STAGE_JOB:?}"
.venv/controller/bin/python -m controller.campaign handle --campaign "${AFAR_CAMPAIGN:?}" \
    --generation "$AFAR_GENERATION" --stage "$AFAR_STAGE" --job "$AFAR_STAGE_JOB" &
wait $!
