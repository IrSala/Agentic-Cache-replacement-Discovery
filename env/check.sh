#!/usr/bin/env bash
# Verify the environment end to end.
#
# Usage: make check   (loads env/modules.sh first)
set -uo pipefail
cd "$(dirname "$0")/.."

failures=0
pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
check() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi
}

echo "Toolchain (from env/modules.sh):"
check "python3 $(python3 -c 'import platform; print(platform.python_version())') is >= 3.10 (OpenEvolve)" \
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'
check "g++ $(g++ -dumpfullversion) supports C++17 (ChampSim)" \
    bash -c 'echo "int main(){}" | g++ -std=c++17 -x c++ -o /dev/null -'
check "git $(git --version | awk '{print $3}')" git --version

echo "ChampSim:"
unpinned="$(git submodule status --recursive | grep -v '^ ' || true)"
if [[ -z "$unpinned" ]]; then
    pass "submodules at pinned commits: $(git submodule status --recursive | awk '{printf "%s@%.7s ", $2, $1}')"
else
    fail "submodules not at pinned commits (run: make submodules): $unpinned"
fi
check "vcpkg dependencies installed" test -d external/ChampSim/vcpkg_installed
check "baseline binary runs (build/baseline/champsim --help)" build/baseline/champsim --help

echo "Python venvs:"
for role in controller evaluator; do
    py=".venv/${role}/bin/python"
    check "${role} venv matches env/${role}-requirements.txt" \
        diff <("$py" -m pip freeze) <(grep -vE '^[[:space:]]*(#|$)' "env/${role}-requirements.txt")
done
check "openevolve $(.venv/controller/bin/python -c 'import importlib.metadata as m; print(m.version("openevolve"))' 2>/dev/null) imports" \
    .venv/controller/bin/python -c 'import openevolve'
check "openevolve-run --help" .venv/controller/bin/openevolve-run --help

if (( failures )); then
    echo "${failures} check(s) failed."
    exit 1
fi
echo "All checks passed."
