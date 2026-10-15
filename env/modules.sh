# Canonical module set for AFAR_HPC on SJSU CoE HPC.
#
# Source this file; do not execute it:
#     source env/modules.sh
#
# It is sourced by the Makefile, by env/activate.sh, and (later) by every
# sbatch script. When porting to another cluster, this is the only file that
# should need editing.

# Batch jobs and `env -i` shells may not have Lmod initialized.
type module >/dev/null 2>&1 || source /etc/profile.d/lmod.sh

module purge

# The Spack module tree is added by /etc/profile.d/z-spack.sh on the login
# node, but it has been missing inside batch jobs. Add it explicitly.
module use /opt/ohpc/pub/apps/spack/share/spack/modules/linux-centos7-broadwell

module load python3/3.11.5                  # OpenEvolve needs >=3.10; also runs ChampSim's config.sh
module load gcc-12.2.0-gcc-11.2.0-7gle75f   # compiler that built the known-good ChampSim
module load git-2.39.1-gcc-12.2.0-b274tuh   # vcpkg needs a newer git than the system 1.8.3
