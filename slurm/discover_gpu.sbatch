#!/bin/bash
# R1: report what a COE-HPC3 GPU node provides (GPU, storage, containers, our env).
#
# Submit from ~/AFAR_HPC on coe-hpc3:
#     sbatch slurm/discover_gpu.sbatch
#
# The report lands in logs/discover-gpu-<jobid>.out on the shared /home.
#
#SBATCH --job-name=discover-gpu
#SBATCH --partition=gpuqs
#SBATCH --gres=gpu:a100:1          # gpuqs is mostly P100 (unsupported by vLLM); ask for an A100
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=00:10:00
#SBATCH --output=logs/%x-%j.out

# No `set -e`: every probe should run and report, even if an earlier one fails.
set -uo pipefail
cd "${SLURM_SUBMIT_DIR}"

section() { printf '\n========== %s\n' "$1"; }
probe() {
    printf -- '--- $ %s\n' "$1"
    bash -c "$1" 2>&1
    printf -- '(exit %s)\n' "$?"
}

section "Job"
probe 'echo "job=${SLURM_JOB_ID} node=$(hostname) partition=${SLURM_JOB_PARTITION} date=$(date -Is)"'
probe 'git rev-parse HEAD'
probe 'head -2 /etc/os-release; ldd --version | head -1; uname -r'
probe 'nproc; free -g | head -2'

section "GPU"
probe 'echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"'
probe 'nvidia-smi'
probe 'nvidia-smi --query-gpu=name,compute_cap,memory.total,driver_version --format=csv'

section "Storage"
probe 'df -h /home /data /scratch /tmp'
probe 'echo "TMPDIR=${TMPDIR:-unset}"; ls -ld /local /scratch/local /tmp 2>&1'
probe 'lsblk -d -o NAME,SIZE,ROTA,MOUNTPOINT'
probe 'ls -ld /data/cmpe295-bond /scratch/cmpe295-bond'
probe 'source env/site.sh && echo "AFAR_ASSETS=${AFAR_ASSETS}" && mkdir -p "${AFAR_ASSETS}" && touch "${AFAR_ASSETS}/.write-test" && rm "${AFAR_ASSETS}/.write-test" && echo "AFAR_ASSETS writable"'

section "Network (expected: no internet on compute nodes)"
probe 'curl -sS --max-time 5 -o /dev/null -w "%{http_code}\n" https://huggingface.co'

section "Containers and modules"
probe 'df -h /opt/ohpc/pub; echo "MODULEPATH=${MODULEPATH:-unset}"; ls -l /opt/ohpc/pub/apps/python3/'
probe 'type module >/dev/null 2>&1 || source /etc/profile.d/lmod.sh; module avail 2>&1 | grep -iE "apptainer|singularity|cuda|python|gcc|git"'
probe 'command -v gcc g++ git; gcc --version | head -1; git --version'
probe 'which apptainer singularity'

section "AFAR_HPC environment"
probe 'source env/modules.sh && module -t list && python3 --version && g++ --version | head -1'
probe '.venv/controller/bin/python -c "import sys, openevolve, numpy; print(sys.version.split()[0], \"openevolve ok\")"'
probe 'build/baseline/champsim --help > /dev/null && echo "champsim binary ok"'
probe 'ldd build/baseline/champsim | grep -E "not found|libstdc"'

section "Done"
