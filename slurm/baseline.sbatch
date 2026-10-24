#!/bin/bash
# M1: run the LRU baseline for each manifest on an HPC3 CPU node and write
# validated result records to $AFAR_RESULTS/baselines/<experiment>/.
#
# Submit from ~/AFAR_HPC on coe-hpc3 (g17), checking the request first:
#     sbatch --test-only slurm/baseline.sbatch
#     sbatch slurm/baseline.sbatch
# Choose manifests (default: both smoke manifests):
#     sbatch --export=ALL,MANIFESTS="smoke-v1" slurm/baseline.sbatch
#
# smoke-tiny-v1 must reproduce make smoke's 43,703 cycles; a different count
# fails the job. It always reruns; other manifests skip traces that already
# have a completed record for the same manifest, so a requeued job is harmless.
#
# Test on coe-hpc1 with the seconds-long manifest only:
#     SLURM_JOB_ID=12345 SLURM_SUBMIT_DIR=$PWD MANIFESTS=smoke-tiny-v1 bash slurm/baseline.sbatch
#
#SBATCH --job-name=baseline
#SBATCH --partition=cpuqs
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#SBATCH --signal=B:TERM@60
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh

py=.venv/evaluator/bin/python
manifests="${MANIFESTS:-smoke-tiny-v1 smoke-v1}"
echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)$(git diff --quiet HEAD 2>/dev/null || echo -dirty)"
echo "manifests=${manifests} AFAR_ASSETS=${AFAR_ASSETS} AFAR_RESULTS=${AFAR_RESULTS}"

for exp in $manifests; do
    m="configs/experiments/${exp}.json"
    echo "== ${exp}"
    "$py" -m evaluator.manifest --check "$m" --verify-traces
    expect=()
    if [[ "$exp" == smoke-tiny-v1 ]]; then
        # A reproducibility check, so always rerun it: make smoke's count on this trace (E1).
        expect=(--expect-cycles 43703 --force)
    fi
    # In the background with wait, so a SIGTERM from --signal reaches the shell promptly.
    "$py" -m evaluator.run_baseline --manifest "$m" "${expect[@]}" &
    wait $!
    "$py" -m evaluator.results --validate "${AFAR_RESULTS}/baselines/${exp}"/*.json
done
echo "done"
