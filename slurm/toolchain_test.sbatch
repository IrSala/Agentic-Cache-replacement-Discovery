#!/bin/bash
# M2 part A: can HPC3's system g++ build ChampSim against our vcpkg libraries,
# and what does a candidate build cost? See evaluator/toolchain_test.py.
#
# Submit from ~/AFAR_HPC on coe-hpc3 (g17), checking the request first:
#     sbatch --test-only slurm/toolchain_test.sbatch
#     sbatch slurm/toolchain_test.sbatch
#
# Writes $AFAR_RESULTS/toolchain/<jobid>.json and prints the three gates
# (G1 links, G2 incremental build cost, G3 trace on /data vs node-local).
#
#SBATCH --job-name=toolchain-test
#SBATCH --partition=cpuqs
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=01:00:00
#SBATCH --signal=B:TERM@60
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh

echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)$(git diff --quiet HEAD 2>/dev/null || echo -dirty)"
# In the background with wait, so a SIGTERM from --signal reaches the shell promptly.
.venv/evaluator/bin/python -m evaluator.toolchain_test --cxx "${AFAR_CXX:-/usr/bin/g++}" --jobs "${SLURM_CPUS_PER_TASK}" &
wait $!
echo "done"
