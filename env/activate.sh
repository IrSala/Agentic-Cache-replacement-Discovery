# Load the module set and activate the controller venv (OpenEvolve).
#
# Source this file from any directory:
#     source env/activate.sh

_afar_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

source "${_afar_root}/env/modules.sh"
source "${_afar_root}/.venv/controller/bin/activate"

unset _afar_root
