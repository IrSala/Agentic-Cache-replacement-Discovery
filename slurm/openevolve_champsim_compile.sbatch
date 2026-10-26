#!/bin/bash
# R7, kept as a wrapper until M5: the run spec configs/runs/champsim-compile-v1.yaml
# through the generic job, slurm/openevolve.sbatch (see it for options and logs).
# These lines keep R7's resources and job name. New runs should use:
#     sbatch --export=ALL,SPEC=champsim-compile-v1 slurm/openevolve.sbatch
# CHECKPOINT=, RESUME= and ITERATIONS= pass through, as there.
#
#SBATCH --job-name=oe-cs-compile
#SBATCH --partition=gpuqs
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=24G                 # measured peak ~4.2 GB (sacct MaxRSS); 64G kept jobs waiting
#SBATCH --time=01:00:00
#SBATCH --signal=B:TERM@60
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
export SPEC=champsim-compile-v1
exec bash slurm/openevolve.sbatch
