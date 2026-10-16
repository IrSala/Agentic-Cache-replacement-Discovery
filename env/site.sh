# Site-specific paths for AFAR_HPC on SJSU CoE HPC.
#
# Source this file after env/modules.sh; do not execute it:
#     source env/site.sh
#
# Large, long-lived assets (LLM container images, model weights) live in the
# project group's durable /data area: /home is nearly full and /scratch has no
# published purge policy. Set AFAR_ASSETS before sourcing to relocate them.

export AFAR_ASSETS="${AFAR_ASSETS:-/data/cmpe295-bond/${USER}/afar}"

# Keep Apptainer's layer cache and build space off /home.
export APPTAINER_CACHEDIR="${AFAR_ASSETS}/apptainer/cache"
export APPTAINER_TMPDIR="${AFAR_ASSETS}/apptainer/tmp"
