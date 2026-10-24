"""Fitness functions, named and versioned (docs/experiment-contract.md §4). Standard library only.

A manifest selects one with "fitness": {"name": ..., "version": ...}. Each
takes a candidate's per-trace records and the baseline records (both keyed by
trace id) and returns (fitness, details). It raises InfraError when a score
would be meaningless: an infrastructure failure, or a missing baseline.

    python -m evaluator.fitness --selftest
"""

import argparse
import math
import sys

from evaluator.policies.base import InfraError


def geomean_ipc_speedup_v1(records, baselines):
    """Geometric mean over traces of ipc_candidate / ipc_lru; 0.0 if any trace failed for candidate reasons."""
    statuses = {t: r["status"] for t, r in records.items()}
    infra = [t for t, s in statuses.items() if s == "infra_error"]
    if infra:
        raise InfraError(f"infrastructure failure on trace(s) {', '.join(infra)}; no score")
    failed = {t: s for t, s in statuses.items() if s != "completed"}
    if failed:
        return 0.0, {"speedups": {}, "failed_traces": failed}
    missing = [t for t in records if t not in baselines]
    if missing:
        raise InfraError(f"no LRU baseline for trace(s) {', '.join(missing)}; run evaluate_candidate --baseline first")
    speedups = {t: r["ipc"] / baselines[t]["ipc"] for t, r in records.items()}
    value = math.exp(math.fsum(math.log(s) for s in speedups.values()) / len(speedups))
    return value, {"speedups": speedups}


REGISTRY = {("geomean_ipc_speedup", 1): geomean_ipc_speedup_v1}


def get(name, version):
    try:
        return REGISTRY[(name, version)]
    except KeyError:
        raise ValueError(f"unknown fitness {name}@{version} (known: "
                         f"{', '.join(f'{n}@{v}' for n, v in REGISTRY)})") from None


def selftest():
    f = get("geomean_ipc_speedup", 1)
    base = {"a": {"ipc": 0.5}, "b": {"ipc": 0.25}}
    done = lambda ipc: {"status": "completed", "ipc": ipc}
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<34} {detail}")

    value, _ = f({"a": done(0.5), "b": done(0.25)}, base)
    check("parity is exactly 1.0", value == 1.0, repr(value))
    value, d = f({"a": done(1.0), "b": done(0.25)}, base)
    check("geomean of 2.0 and 1.0", abs(value - math.sqrt(2)) < 1e-12, f"{value:.6f}")
    value, d = f({"a": done(1.0), "b": {"status": "compile_failed", "ipc": None}}, base)
    check("candidate failure scores 0.0", value == 0.0 and d["failed_traces"] == {"b": "compile_failed"})
    for name, records, baselines in [("infra failure raises", {"a": {"status": "infra_error", "ipc": None}}, base),
                                     ("missing baseline raises", {"c": done(1.0)}, base)]:
        try:
            f(records, baselines)
            check(name, False)
        except InfraError as e:
            check(name, True, str(e)[:60])
    print("fitness selftest passed" if failures == 0 else f"fitness selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true")
    if not parser.parse_args().selftest:
        parser.error("use --selftest")
    sys.exit(selftest())
