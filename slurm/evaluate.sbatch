#!/bin/bash
# M2: evaluate candidates with the champsim_ipc policy on an HPC3 CPU node
# (evaluator/evaluate_candidate.py). Builds the prebuilt base tree first if
# this compiler doesn't have one yet (evaluator/prebuilt.py).
#
# Submit from ~/AFAR_HPC on coe-hpc3 (g17), checking the request first:
#     sbatch --test-only slurm/evaluate.sbatch
#     sbatch slurm/evaluate.sbatch
# The default is M2's acceptance sequence: the self-test, then the LRU baseline,
# the seed and three built-in policies on smoke-v1. Choose what to run with:
#     sbatch --export=ALL,MANIFEST=smoke-v1,TARGETS="baseline seeds/evolved_repl.h" slurm/evaluate.sbatch
# TARGETS entries: baseline | selftest | builtin:<module> | <candidate header path>
# FORCE=1 re-evaluates targets that already have a summary.
#
# Results: $AFAR_RESULTS/baselines/<experiment>/ and
#          $AFAR_RESULTS/evaluations/<experiment>/<candidate id>/summary.json
#
#SBATCH --job-name=evaluate
#SBATCH --partition=cpuqs
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=03:00:00
#SBATCH --signal=B:TERM@60
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh

export AFAR_CXX="${AFAR_CXX:-/usr/bin/g++}"
py=.venv/evaluator/bin/python
jobs="${SLURM_CPUS_PER_TASK:-1}"
manifest="configs/experiments/${MANIFEST:-smoke-v1}.json"
targets="${TARGETS:-selftest baseline seeds/evolved_repl.h builtin:srrip builtin:drrip builtin:ship}"
echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)$(git diff --quiet HEAD 2>/dev/null || echo -dirty)"
echo "manifest=${manifest} targets=${targets} compiler=${AFAR_CXX} ($("$AFAR_CXX" --version | head -1))"

# In the background with wait, so a SIGTERM from --signal reaches the shell promptly.
"$py" -m evaluator.prebuilt --build --cxx "$AFAR_CXX" --jobs "$jobs" &
wait $!

for target in $targets; do
    echo "== ${target}"
    case "$target" in
        selftest)  args=(--selftest) ;;
        baseline)  args=(--manifest "$manifest" --baseline) ;;
        builtin:*) args=(--manifest "$manifest" --builtin "${target#builtin:}") ;;
        *)         args=(--manifest "$manifest" --candidate "$target") ;;
    esac
    "$py" -m evaluator.evaluate_candidate "${args[@]}" --cxx "$AFAR_CXX" --jobs "$jobs" ${FORCE:+--force} &
    wait $!
done
echo "done"
