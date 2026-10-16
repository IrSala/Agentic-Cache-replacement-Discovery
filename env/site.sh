# Run-time paths for AFAR_HPC, valid on both coe-hpc1 and COE-HPC3.
#
# Source this file after cloning; do not execute it:
#     source env/site.sh
#
# It loads no modules: jobs on COE-HPC3 must not depend on coe-hpc1's module
# tree. Everything a job needs lives on shared storage (/home, /data, /scratch).

export AFAR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Large, long-lived assets (LLM container images, model weights) live in the
# project group's durable /data area: /home is nearly full and /scratch has no
# published purge policy. Set AFAR_ASSETS before sourcing to relocate them.
export AFAR_ASSETS="${AFAR_ASSETS:-/data/cmpe295-bond/${USER}/afar}"

# Keep Apptainer's layer cache and build space off /home.
export APPTAINER_CACHEDIR="${AFAR_ASSETS}/apptainer/cache"
export APPTAINER_TMPDIR="${AFAR_ASSETS}/apptainer/tmp"
