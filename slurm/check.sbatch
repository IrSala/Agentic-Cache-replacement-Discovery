#!/bin/bash
# E2: run AFAR_HPC's run-time checks on a COE-HPC3 CPU node.
#
# Submit from the repo root on coe-hpc3 (g17):
#     sbatch slurm/check.sbatch
#
# The result lands in logs/afar-check-<jobid>.out on the shared /home.
#
#SBATCH --job-name=afar-check
#SBATCH --partition=defq
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:10:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail
cd "${SLURM_SUBMIT_DIR}"

echo "job=${SLURM_JOB_ID} node=$(hostname) partition=${SLURM_JOB_PARTITION} date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)"

# A clean environment: nothing inherited from the submitting shell (no modules).
env -i HOME="$HOME" USER="$USER" PATH=/usr/bin:/bin bash env/check.sh --runtime
