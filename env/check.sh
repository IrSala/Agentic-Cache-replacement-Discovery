#!/usr/bin/env bash
# Verify the environment.
#
#   env/check.sh --build     build tools and sources (coe-hpc1, env/modules.sh loaded)
#   env/check.sh --runtime   what jobs need (either cluster, no modules)
#
# `make check` runs both: --build with modules, then --runtime in a clean
# environment like a batch job's. On g17, or in slurm/check.sbatch, use --runtime.
set -uo pipefail
cd "$(dirname "$0")/.."
source env/site.sh
source env/python.pin

failures=0
pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
warn() { printf '  warn  %s\n' "$1"; }
check() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}

# Highest GLIBC_x.y symbol version a binary references (ELF version strings).
max_glibc() {
    .venv/evaluator/bin/python - "$1" <<'EOF'
import re, sys
data = open(sys.argv[1], "rb").read()
found = {tuple(map(int, v.split(b"."))) for v in re.findall(rb"GLIBC_(\d+\.\d+)", data)}
print(".".join(map(str, max(found))) if found else "none")
EOF
}
# True if version $1 <= version $2.
version_le() { [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" == "$1" ]]; }

build_checks() {
    echo "Build tools (env/modules.sh, coe-hpc1):"
    check "g++ $(g++ -dumpfullversion) supports C++17 (ChampSim)" \
        bash -c 'echo "int main(){}" | g++ -std=c++17 -x c++ -o /dev/null -'
    check "git $(git --version | awk '{print $3}')" git --version

    echo "Sources:"
    local unpinned
    unpinned="$(git submodule status --recursive | grep -v '^ ' || true)"
    if [[ -z "$unpinned" ]]; then
        pass "submodules at pinned commits: $(git submodule status --recursive | awk '{printf "%s@%.7s ", $2, $1}')"
    else
        fail "submodules not at pinned commits (run: make submodules): $unpinned"
    fi
    check "vcpkg dependencies installed" test -d external/ChampSim/vcpkg_installed
}

runtime_checks() {
    echo "Run-time on $(hostname) ($(. /etc/os-release && echo "$PRETTY_NAME"), $(ldd --version | head -1 | awk '{print "glibc", $NF}')):"

    for role in controller evaluator; do
        local py=".venv/${role}/bin/python"
        local real
        real="$(readlink -f "$py")"
        if [[ "$real" == "${AFAR_ROOT}/.python/"* ]] && "$py" -c '' 2>/dev/null; then
            pass "${role} venv runs $("$py" -c 'import platform; print(platform.python_version())') from .python/"
        else
            fail "${role} venv interpreter is ${real:-missing}, expected under .python/ (run: make venvs on coe-hpc1)"
            continue
        fi
        check "${role} venv matches env/${role}-requirements.txt" \
            diff <("$py" -m pip freeze) <(grep -vE '^[[:space:]]*(#|$)' "env/${role}-requirements.txt")
    done
    check "python is ${PYTHON_VERSION} (env/python.pin)" \
        test "$(.python/bin/python3 -c 'import platform; print(platform.python_version())')" = "${PYTHON_VERSION}"
    check "openevolve $(.venv/controller/bin/python -c 'import importlib.metadata as m; print(m.version("openevolve"))' 2>/dev/null) imports" \
        .venv/controller/bin/python -c 'import openevolve'
    check "openevolve-run --help" .venv/controller/bin/openevolve-run --help

    local bin=build/baseline/champsim
    check "baseline binary runs (${bin} --help)" "$bin" --help
    if ldd "$bin" 2>/dev/null | grep -qE 'libstdc\+\+|libgcc_s|not found'; then
        fail "baseline binary depends on a shared libstdc++/libgcc or a missing library"
    else
        pass "baseline binary links only system libraries (static libstdc++)"
    fi
    local need
    for f in "$bin" .python/bin/python3; do
        need="$(max_glibc "$(readlink -f "$f")")"
        if version_le "$need" 2.17; then
            pass "$(basename "$f") needs glibc ${need} (<= 2.17, portable to COE-HPC3)"
        else
            fail "$(basename "$f") needs glibc ${need} (> 2.17, would not run on coe-hpc1)"
        fi
    done

    if mkdir -p "$AFAR_ASSETS" 2>/dev/null && touch "$AFAR_ASSETS/.write-test" 2>/dev/null; then
        rm -f "$AFAR_ASSETS/.write-test"
        pass "AFAR_ASSETS writable ($AFAR_ASSETS)"
    else
        fail "AFAR_ASSETS not writable ($AFAR_ASSETS); set AFAR_ASSETS before sourcing env/site.sh"
    fi
    if command -v apptainer >/dev/null; then
        pass "apptainer $(apptainer --version | awk '{print $NF}')"
    else
        warn "apptainer not on PATH (needed only for LLM serving jobs on COE-HPC3)"
    fi
}

case "${1:-}" in
    --build)   build_checks ;;
    --runtime) runtime_checks ;;
    *) echo "usage: env/check.sh --build | --runtime" >&2; exit 2 ;;
esac

if (( failures )); then
    echo "${failures} check(s) failed."
    exit 1
fi
echo "All checks passed."
