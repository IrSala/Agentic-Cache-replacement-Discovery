"""R7: OpenEvolve evaluator stub for ChampSim replacement candidates. It runs
static checks and a compile-only check; no build, no simulation (Phase 3).

OpenEvolve imports this file and calls evaluate(program_path) with a temporary
copy of the candidate header (openevolve/evaluator.py). Statuses and scores
follow docs/experiment-contract.md:

    completed          compiles, and ChampSim detects its hooks     combined_score 1.0 (stub)
    invalid_candidate  too large, EVOLVE-BLOCK markers wrong, or
                       text outside the block differs from the seed  0.0
    compile_failed     the compiler reports errors, including the
                       hook-detection static_asserts below           0.0
    timeout            the compiler ran past COMPILE_TIMEOUT_S; its
                       whole process group is killed                 0.0

Infrastructure problems (no compiler, ChampSim not configured, no seed) raise,
as the contract requires, rather than scoring the candidate.

The compiler is $AFAR_CXX (default: g++ on PATH). Self-test, no GPU or LLM:
    .venv/controller/bin/python evaluator/compile_check.py --selftest
"""

import argparse
import functools
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from openevolve.evaluation_result import EvaluationResult

ROOT = Path(__file__).resolve().parent.parent
CHAMPSIM = ROOT / "external" / "ChampSim"
SEED = ROOT / "seeds" / "evolved_repl.h"

MAX_SOURCE_BYTES = 32 * 1024  # the contract's limits.max_source_bytes; a manifest sets it from Phase 3
COMPILE_TIMEOUT_S = 60        # below evaluator.timeout in the OpenEvolve config, so this kill comes first
OUTPUT_TAIL_CHARS = 4096
START, END = "// EVOLVE-BLOCK-START", "// EVOLVE-BLOCK-END"
CLASS_HEAD = "class evolved_repl : public champsim::modules::replacement"

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


class InfraError(RuntimeError):
    """The environment is broken; not the candidate's fault (contract: infra_error)."""


def split_block(text):
    """Return (before, block, after) as line lists, split at the marker lines, or None."""
    if text.count("EVOLVE-BLOCK-START") != 1 or text.count("EVOLVE-BLOCK-END") != 1:
        return None
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == START]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None
    s, e = starts[0], ends[0]
    return lines[: s + 1], lines[s + 1 : e], lines[e:]


def fixed_text(lines):
    """The fixed text as compared: trailing whitespace on each line and at the end ignored."""
    return "\n".join(line.rstrip() for line in lines).rstrip()


def static_problem(code, seed):
    """None if the candidate passes the static checks; otherwise the reason."""
    if len(code.encode()) > MAX_SOURCE_BYTES:
        return f"file is {len(code.encode())} bytes; the limit is {MAX_SOURCE_BYTES}"
    parts = split_block(code)
    if parts is None:
        return f"needs exactly one '{START}' line followed by one '{END}' line"
    before, _, after = parts
    seed_before, _, seed_after = split_block(seed)
    if fixed_text(before) != fixed_text(seed_before):
        return "text before the EVOLVE-BLOCK differs from the seed; only the block may change"
    if fixed_text(after) != fixed_text(seed_after):
        return "text after the EVOLVE-BLOCK differs from the seed; only the block may change"
    if CLASS_HEAD not in code:
        return f"'{CLASS_HEAD}' is missing"
    return None


def run_killable(cmd, cwd, timeout):
    """Run cmd in its own process group and kill the whole group on timeout.

    subprocess.run(timeout=) kills only the direct child (the g++ driver, not
    cc1plus), and preexec_fn is unsafe here because OpenEvolve calls evaluate()
    in a thread, so this uses Popen(process_group=0) and os.killpg.
    Returns (exit code, or None after a timeout; output; seconds).
    """
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace", process_group=0)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out, time.monotonic() - start
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out, time.monotonic() - start


@functools.lru_cache(maxsize=None)
def compiler_version(cxx):
    out = subprocess.run([cxx, "--version"], capture_output=True, text=True, timeout=30).stdout
    return out.splitlines()[0] if out else "unknown"


def check_infra(cxx):
    """Return the seed text, or raise InfraError if the environment can't evaluate."""
    if shutil.which(cxx) is None:
        raise InfraError(f"compiler {cxx!r} not found (set AFAR_CXX)")
    needed = [SEED, CHAMPSIM / ".csconfig" / "module_decl.inc"]
    needed += [CHAMPSIM / name for name in ("global.options", "absolute.options", "module.options")]
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        raise InfraError(f"missing {', '.join(missing)}; run `make champsim` on coe-hpc1")
    return SEED.read_text()


def result(status, cxx, reason="", output="", seconds=0.0):
    ok = status == "completed"
    metrics = {"combined_score": 1.0 if ok else 0.0, "compiles": 1.0 if ok else 0.0,
               "compile_seconds": round(seconds, 2)}
    artifacts = {"status": status, "compiler": compiler_version(cxx)}
    if reason:
        artifacts["reason"] = reason
    if output.strip():
        artifacts["compiler_output"] = output[-OUTPUT_TAIL_CHARS:]
    return EvaluationResult(metrics=metrics, artifacts=artifacts)


def evaluate(program_path):
    """OpenEvolve's entry point."""
    cxx = os.environ.get("AFAR_CXX", "g++")
    seed = check_infra(cxx)
    code = Path(program_path).read_text(errors="replace")

    problem = static_problem(code, seed)
    if problem:
        return result("invalid_candidate", cxx, reason=problem)

    cmd = [cxx, f"@{CHAMPSIM}/global.options", f"@{CHAMPSIM}/absolute.options", f"@{CHAMPSIM}/module.options",
           f"-I{CHAMPSIM}/.csconfig", "-fsyntax-only", "-fdiagnostics-color=never", "-fmax-errors=10", "check.cc"]
    with tempfile.TemporaryDirectory(prefix="afar-compile-") as work:
        Path(work, "evolved_repl.h").write_text(code)
        Path(work, "check.cc").write_text(CHECK_CC)
        code_or_none, output, seconds = run_killable(cmd, work, COMPILE_TIMEOUT_S)
    if code_or_none is None:
        return result("timeout", cxx, reason=f"the compiler was still running after {COMPILE_TIMEOUT_S}s and was killed",
                      output=output, seconds=seconds)
    if code_or_none != 0:
        return result("compile_failed", cxx, reason="the compiler reported errors", output=output, seconds=seconds)
    return result("completed", cxx, output=output, seconds=seconds)


def selftest():
    """Check every status on variants of the seed, plus the timeout kill. Exit 1 on any mismatch."""
    seed = SEED.read_text()
    body = "// Find the way whose last use cycle is most distant"
    cases = [
        ("seed", seed, "completed", None),
        ("block-only edit (MRU)", seed.replace(body, "// evolved").replace("std::min_element", "std::max_element"),
         "completed", None),
        ("no trailing newline", seed.rstrip(), "completed", None),
        ("trailing spaces on fixed lines", seed.replace("#include <map>\n", "#include <map>   \n"), "completed", None),
        ("changed include", seed.replace("#include <deque>\n", "#include <list>\n"), "invalid_candidate", "before the EVOLVE-BLOCK"),
        ("END marker removed", seed.replace(END, ""), "invalid_candidate", "exactly one"),
        ("over the size limit", seed.replace(body, "// " + "x" * MAX_SOURCE_BYTES), "invalid_candidate", "limit"),
        ("syntax error", seed.replace("uint64_t cycle = 0;", "uint64_t cycle = 0"), "compile_failed", "error"),
        ("wrong hook signature", seed.replace("access_type type, uint8_t hit)", "access_type type, uint8_t* hit)"),
         "compile_failed", "update_replacement_state is missing"),
    ]
    failures = 0
    print(f"compiler: {compiler_version(os.environ.get('AFAR_CXX', 'g++'))}")
    with tempfile.TemporaryDirectory(prefix="afar-selftest-") as work:
        for name, text, want, needle in cases:
            path = Path(work, "candidate.h")
            path.write_text(text)
            res = evaluate(str(path))
            got = res.artifacts["status"]
            detail = res.artifacts.get("reason", "") + "\n" + res.artifacts.get("compiler_output", "")
            ok = got == want and (needle is None or needle in detail)
            failures += not ok
            print(f"  {'ok  ' if ok else 'FAIL'}  {name:<32} {got:<18} score={res.metrics['combined_score']} "
                  f"{res.metrics['compile_seconds']:.1f}s")
            if not ok:
                print(f"        expected {want}" + (f" mentioning {needle!r}" if needle else "") + f"; got:\n{detail[-1500:]}")

        # A process that leaves a grandchild behind must die with its whole group.
        # With process_group=0, bash's own pid ($$) is the group id.
        exit_code, _, seconds = run_killable(["bash", "-c", "echo $$ > pgid; sleep 300 & sleep 300"], work, timeout=1)
        group = int(Path(work, "pgid").read_text())
        survivors = True
        for _ in range(20):
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                survivors = False
                break
            time.sleep(0.1)
        ok = exit_code is None and not survivors
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {'timeout kills the process group':<32} "
              f"{'killed' if exit_code is None else 'not killed'} after {seconds:.1f}s, "
              f"{'no survivors' if not survivors else 'SURVIVORS LEFT'}")

    print("selftest passed" if failures == 0 else f"selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true", help="check every status on variants of the seed")
    args = parser.parse_args()
    if not args.selftest:
        parser.error("nothing to do; OpenEvolve calls evaluate() directly (or use --selftest)")
    sys.exit(selftest())
