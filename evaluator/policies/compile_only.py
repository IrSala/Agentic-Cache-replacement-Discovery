"""The compile_only policy (R7): static checks plus a syntax-only compile. Standard library only.

No ChampSim build and no simulation, so every candidate that compiles scores
1.0. Its syntax check is also champsim_ipc's first build stage, because it
catches a hook with the wrong signature, which ChampSim would otherwise
compile and silently never call.
"""

import functools
import shutil
import subprocess
import tempfile
from pathlib import Path

from evaluator import static_checks
from evaluator.policies.base import Build, InfraError, Outcome, Policy
from evaluator.proc import run_killable

ROOT = Path(__file__).resolve().parents[2]
CHAMPSIM = ROOT / "external" / "ChampSim"
SEED = ROOT / "seeds" / "evolved_repl.h"
MAX_SOURCE_BYTES = 32 * 1024  # the contract's default limits.max_source_bytes; a manifest can set it
COMPILE_TIMEOUT_S = 60        # below evaluator.timeout in the OpenEvolve configs, so this kill comes first
OUTPUT_TAIL_CHARS = 4096

# Compiled next to the candidate. ChampSim calls a hook only if these traits
# detect it (inc/cache.h:447-494); otherwise it silently uses a default, so a
# hook with the wrong signature would build and run but never be called. The
# variants are exactly the ones cache.h accepts.
CHECK_CC = """\
#include "evolved_repl.h"

#include <type_traits>

namespace afar_check
{
using R = champsim::modules::replacement;
using T = evolved_repl&;
using champsim::address;
using access_int = std::underlying_type_t<access_type>;
using block = const champsim::cache_block*;

constexpr bool finds_victim = R::has_find_victim<T, uint32_t, uint64_t, long, block, address, address, access_type>
                              || R::has_find_victim<T, uint32_t, uint64_t, long, block, address, address, access_int>
                              || R::has_find_victim<T, uint32_t, uint64_t, long, block, uint64_t, uint64_t, access_int>;

constexpr bool updates_state = R::has_update_state<T, uint32_t, long, long, address, address, access_type, bool>
                               || R::has_update_state<T, uint32_t, long, long, address, address, address, access_type, bool>
                               || R::has_update_state<T, uint32_t, long, long, address, address, address, access_int, bool>
                               || R::has_update_state<T, uint32_t, long, long, uint64_t, uint64_t, uint64_t, access_int, bool>;
} // namespace afar_check

static_assert(std::is_constructible_v<evolved_repl, CACHE*>, "evolved_repl needs a constructor taking CACHE*");
static_assert(afar_check::finds_victim, "find_victim is missing or has a signature ChampSim never calls; use the seed's signature");
static_assert(afar_check::updates_state, "update_replacement_state is missing or has a signature ChampSim never calls; use the seed's signature");
"""


@functools.lru_cache(maxsize=None)
def compiler_version(cxx):
    out = subprocess.run([cxx, "--version"], capture_output=True, text=True, timeout=30).stdout
    return out.splitlines()[0] if out else "unknown"


def check_infra(cxx):
    """Return the seed text, or raise InfraError if the environment can't compile candidates."""
    if shutil.which(cxx) is None:
        raise InfraError(f"compiler {cxx!r} not found (set AFAR_CXX)")
    needed = [SEED, CHAMPSIM / ".csconfig" / "module_decl.inc"]
    needed += [CHAMPSIM / name for name in ("global.options", "absolute.options", "module.options")]
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        raise InfraError(f"missing {', '.join(missing)}; run `make champsim` on coe-hpc1")
    return SEED.read_text()


def syntax_check(code, cxx, timeout=COMPILE_TIMEOUT_S):
    """Compile the candidate with ChampSim's flags and the hook static_asserts, without linking."""
    cmd = [cxx, f"@{CHAMPSIM}/global.options", f"@{CHAMPSIM}/absolute.options", f"@{CHAMPSIM}/module.options",
           f"-I{CHAMPSIM}/.csconfig", "-fsyntax-only", "-fdiagnostics-color=never", "-fmax-errors=10", "check.cc"]
    with tempfile.TemporaryDirectory(prefix="afar-compile-") as work:
        Path(work, "evolved_repl.h").write_text(code)
        Path(work, "check.cc").write_text(CHECK_CC)
        exit_code, output, seconds = run_killable(cmd, work, timeout)
    if exit_code is None:
        return Build("timeout", f"the compiler was still running after {timeout}s and was killed", seconds, output)
    if exit_code != 0:
        return Build("compile_failed", "the compiler reported errors", seconds, output)
    return Build("completed", "", seconds, output)


class CompileOnly(Policy):
    name = "compile_only"

    def static_check(self, candidate, ctx):
        self._seed = check_infra(ctx.cxx)
        limit = ctx.manifest["limits"]["max_source_bytes"] if ctx.manifest else MAX_SOURCE_BYTES
        return static_checks.static_problem(candidate.code, self._seed, limit)

    def build(self, candidate, ctx):
        return syntax_check(candidate.code, ctx.cxx)

    def _outcome(self, status, reason, build, ctx):
        ok = status == "completed"
        seconds = build.seconds if build else 0.0
        artifacts = {"status": status, "compiler": compiler_version(ctx.cxx)}
        if reason:
            artifacts["reason"] = reason
        if build and build.output.strip():
            artifacts["compiler_output"] = build.output[-OUTPUT_TAIL_CHARS:]
        return Outcome(status=status, fitness=1.0 if ok else 0.0, reason=reason, artifacts=artifacts,
                       metrics={"combined_score": 1.0 if ok else 0.0, "compiles": 1.0 if ok else 0.0,
                                "compile_seconds": round(seconds, 2)})

    def fail(self, status, reason, candidate, ctx, build=None):
        return self._outcome(status, reason, build, ctx)

    def score(self, candidate, build, records, ctx):
        return self._outcome("completed", "", build, ctx)
