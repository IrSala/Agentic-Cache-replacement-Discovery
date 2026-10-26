#!/bin/bash
# M3: a Slurm array where each task evaluates one (target, trace) pair of a
# manifest on 1 CPU, with evaluate_candidate --trace. Task i runs
#     target = TARGETS[i / ntraces], trace = traces[i % ntraces]
# so a failed task affects only its own pair, and each task writes its own
# record and summary-<trace>.json.
#
# Submit from ~/AFAR_HPC on coe-hpc3 (g17); `python -m evaluator.calibrate --plan`
# prints the exact commands. Calibration (unscored built-in policies, the default):
#     sbatch --array=0-75%40 --time=01:00:00 --export=ALL,MANIFEST=crc2-cal-L1-v1 slurm/calibrate_array.sbatch
# LRU baselines for a manifest, one trace per task:
#     sbatch --array=0-<ntraces-1>%40 --time=07:30:00 --export=ALL,MANIFEST=crc2-validate-v1,TARGETS=baseline slurm/calibrate_array.sbatch
# TARGETS entries: baseline | builtin:<module> | <candidate header path>. Built-ins
# and candidates run unscored unless SCORED=1 (which needs the baselines first).
#
# Exit status: 0 for any candidate outcome, 1 for an infrastructure failure, so
#     sacct -j <jobid> --format=JobID,State,ExitCode,Elapsed,MaxRSS
# shows which tasks to resubmit (sbatch --array=<those ids> ...). Finished pairs
# are skipped on resubmission, since their summaries are reused.
#
#SBATCH --job-name=calibrate
#SBATCH --partition=cpuqs
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=01:00:00
#SBATCH --signal=B:TERM@60
#SBATCH --output=logs/%x-%A_%a.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh

export AFAR_CXX="${AFAR_CXX:-/usr/bin/g++}"
py=.venv/evaluator/bin/python
manifest="configs/experiments/${MANIFEST:?set MANIFEST=<manifest id>}.json"
read -r -a targets <<< "${TARGETS:-builtin:lru builtin:srrip builtin:drrip builtin:ship}"
mapfile -t traces < <("$py" -c 'import json, sys; print(*(t["id"] for t in json.load(open(sys.argv[1]))["traces"]), sep="\n")' "$manifest")
i="${SLURM_ARRAY_TASK_ID:?submit with --array}"
n=${#traces[@]}
total=$(( ${#targets[@]} * n ))
if (( i >= total )); then
    echo "task ${i} is outside 0-$(( total - 1 )) (${#targets[@]} targets x ${n} traces)" >&2
    exit 1
fi
target="${targets[i / n]}"
trace="${traces[i % n]}"
echo "job=${SLURM_ARRAY_JOB_ID}_${i} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD 2>/dev/null)$(git diff --quiet HEAD 2>/dev/null || echo -dirty)"
echo "manifest=${manifest} target=${target} trace=${trace} compiler=${AFAR_CXX} ($("$AFAR_CXX" --version | head -1))"

scored=(--unscored)
[[ -n "${SCORED:-}" ]] && scored=()
case "$target" in
    baseline)  args=(--baseline) ;;
    builtin:*) args=(--builtin "${target#builtin:}" ${scored[@]+"${scored[@]}"}) ;;
    *)         args=(--candidate "$target" ${scored[@]+"${scored[@]}"}) ;;
esac
# In the background with wait, so a SIGTERM from --signal reaches the shell promptly.
"$py" -m evaluator.evaluate_candidate --manifest "$manifest" --trace "$trace" "${args[@]}" \
    --cxx "$AFAR_CXX" --jobs 1 ${FORCE:+--force} &
wait $!
