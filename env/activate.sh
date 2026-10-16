# Activate AFAR_HPC's run-time environment: site paths + the controller venv.
#
# Source this file from any directory, on coe-hpc1 or COE-HPC3:
#     source env/activate.sh
#
# It loads no modules. Building happens through `make` on coe-hpc1, which loads
# env/modules.sh itself.

_afar_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source "${_afar_root}/env/site.sh"
source "${_afar_root}/.venv/controller/bin/activate"

unset _afar_root
