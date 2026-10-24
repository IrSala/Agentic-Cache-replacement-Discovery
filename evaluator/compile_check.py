"""R7: OpenEvolve's evaluator for the compile_only policy (evaluator/policies/compile_only.py):
static checks and a compile-only check; no build, no simulation.

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
import os
import sys
import tempfile
import time
from pathlib import Path

from openevolve.evaluation_result import EvaluationResult

ROOT = Path(__file__).resolve().parent.parent
# OpenEvolve imports this file by path, so make the evaluator package importable.
sys.path.insert(0, str(ROOT))
from evaluator.proc import run_killable  # noqa: E402

from evaluator.policies import Candidate, Context, InfraError  # noqa: E402,F401
from evaluator.policies.compile_only import (MAX_SOURCE_BYTES, SEED, CompileOnly,  # noqa: E402
                                             compiler_version)
from evaluator.static_checks import END  # noqa: E402

_POLICY = CompileOnly()


def evaluate(program_path):
    """OpenEvolve's entry point: the compile_only policy, as an EvaluationResult."""
    code = Path(program_path).read_text(errors="replace")
    candidate = Candidate(candidate_id="openevolve", sha256="0" * 64, code=code)
    outcome = _POLICY.evaluate(candidate, Context(cxx=os.environ.get("AFAR_CXX", "g++")))
    return EvaluationResult(metrics=outcome.metrics, artifacts=outcome.artifacts)


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
