#!/bin/bash
# R4, container step 2 of 2: pack the vLLM sandbox (unpacked on coe-hpc1 by
# `make llm-fetch`) into a .sif on an HPC3 CPU node. coe-hpc1's mksquashfs
# deadlocked on this image; HPC3's Rocky 9 tools are much newer.
#
# Submit from ~/AFAR_HPC on coe-hpc3 (g17):
#     sbatch slurm/build_sif.sbatch
#
# The SIF lands next to the sandbox in $AFAR_ASSETS/containers/; the sandbox is
# deleted only after the SIF is verified.
#
#SBATCH --job-name=build-sif
#SBATCH --partition=cpuqs              # defq allows only 2 CPUs per node (MaxCPUsPerNode=2)
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=02:00:00
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SLURM_SUBMIT_DIR}"
source env/site.sh
source configs/llm/serve.env

sif="${AFAR_ASSETS}/containers/${VLLM_SIF}"
sandbox="${AFAR_ASSETS}/containers/${VLLM_SANDBOX}"

echo "job=${SLURM_JOB_ID} node=$(hostname) date=$(date -Is) commit=$(git rev-parse --short HEAD)"
echo "$(apptainer --version); $(head -1 /etc/os-release | cut -d= -f2) $(grep VERSION_ID /etc/os-release | cut -d= -f2)"

if [[ ! -f "$sif" ]]; then
    [[ -f "${sandbox}.complete" ]] || { echo "no finished sandbox at ${sandbox}: run make llm-fetch on coe-hpc1 first" >&2; exit 1; }

    # Build space: node-local /tmp when it has room (much faster than NFS),
    # otherwise the /data default from env/site.sh.
    if (( $(df --output=avail -k /tmp | tail -1) > 40 * 1024 * 1024 )); then
        export APPTAINER_TMPDIR="/tmp/${USER}-build-sif-${SLURM_JOB_ID}"
        mkdir -p "$APPTAINER_TMPDIR"
        trap 'rm -rf "$APPTAINER_TMPDIR"' EXIT
    fi
    echo "APPTAINER_TMPDIR=${APPTAINER_TMPDIR}"

    rm -f "${sif%.sif}.partial.sif"
    time apptainer build "${sif%.sif}.partial.sif" "$sandbox"
    mv "${sif%.sif}.partial.sif" "$sif"
fi

echo "--- verify"
ls -l "$sif"
sha256sum "$sif"
apptainer exec "$sif" python3 -c 'import vllm; print("vllm", vllm.__version__)'
# The `vllm` CLI itself needs a GPU to detect ("Failed to infer device type" on
# CPU nodes), so it is checked by the GPU jobs, not here.

# Only now is the sandbox redundant.
if [[ -d "$sandbox" ]]; then
    echo "--- removing sandbox"
    rm -rf "$sandbox" "${sandbox}.complete"
fi
echo "done"
