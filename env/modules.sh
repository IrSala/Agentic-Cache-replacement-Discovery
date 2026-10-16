# Build-time module set for AFAR_HPC: coe-hpc1 only.
#
# Source this file; do not execute it:
#     source env/modules.sh
#
# The Makefile sources it for build and download steps. Modules provide the
# compiler, a newer git and Apptainer on coe-hpc1. Nothing that runs in a job
# may depend on them: coe-hpc1's module tree (/opt/ohpc/pub, a local disk) is
# not shared with COE-HPC3, where jobs run. Run-time paths are in env/site.sh,
# and Python comes from .python/ (env/python.pin), not from a module.

_afar_spack_modules=/opt/ohpc/pub/apps/spack/share/spack/modules/linux-centos7-broadwell
if [[ ! -d "$_afar_spack_modules" ]]; then
    echo "env/modules.sh: build tools exist only on coe-hpc1; build there (this is $(hostname))." >&2
    unset _afar_spack_modules
    return 1
fi

# Batch jobs and `env -i` shells may not have Lmod initialized.
type module >/dev/null 2>&1 || source /etc/profile.d/lmod.sh

module purge
# On MODULEPATH on the login node, but not always in non-interactive shells.
module use "$_afar_spack_modules"
unset _afar_spack_modules

module load gcc-12.2.0-gcc-11.2.0-7gle75f   # compiler that built the known-good ChampSim
module load git-2.39.1-gcc-12.2.0-b274tuh   # vcpkg needs a newer git than the system 1.8.3
module load apptainer/1.3.2                 # container pulls (make llm-fetch)
