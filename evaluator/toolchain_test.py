"""M2 part A: can this node's compiler build ChampSim, and what does a candidate build cost?

    python -m evaluator.toolchain_test --cxx /usr/bin/g++ --jobs 4     # full test (slurm/toolchain_test.sbatch)
    python -m evaluator.toolchain_test --cxx <g++> --jobs 2 --quick    # steps 1-4 only, for coe-hpc1

Steps:
  1. environment: compiler, make, python3, static libstdc++, prlimit, CPU
  2. full build of LRU in a fresh tree copy                         (gate G1: does it link?)
  3. smoke-tiny-v1 with that binary: must give 43,703 cycles
  4. copy the built tree (as a prebuilt cache would be used), add the seed
     through --replacement-dir, rebuild, run smoke-tiny-v1 (must be 43,703:
     the seed is an LRU port)                                       (gate G2: what recompiles?)
  5. built-in SRRIP the same way (cycles recorded as a sanity check)
  6. smoke-v1 with the trace on /data, then copied to node-local tmp   (gate G3)

Writes $AFAR_RESULTS/toolchain/<job or host-time>.json and prints a summary.
"""

import argparse
import datetime
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from evaluator import champsim_build as cb
from evaluator import results
from evaluator.manifest import Manifest

ROOT = cb.ROOT
SMOKE_TINY_CYCLES = 43703   # make smoke on 602.gcc_s-1850B (E1)
LIMITS = {"as_bytes": 8 << 30, "fsize_bytes": 1 << 30}


def version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).splitlines()[0].strip()
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return None


def cpu_model():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def run_manifest(binary, manifest, trace_path, work, name, timeout):
    """Run the manifest's (first) trace; return a step dict with cycles and seconds."""
    sim = manifest["simulation"]
    json_out, log = work / f"{name}.json", work / f"{name}.log"
    limits = dict(LIMITS, cpu_s=timeout + 60)
    r = cb.run(binary, trace_path, sim["warmup_instructions"], sim["simulation_instructions"],
               json_out, log, timeout, work, limits=limits)
    step = {"ok": r.ok, "seconds": round(r.seconds, 2), "exit_code": r.exit_code}
    if r.ok:
        m = results.champsim_metrics(json_out)
        step.update(cycles=m["cycles"], instructions=m["instructions"], ipc=m["ipc"])
    else:
        step["log_tail"] = r.log[-1500:]
    return step


def build_step(r):
    step = {"ok": r.ok, "seconds": round(r.seconds, 2), "exit_code": r.exit_code,
            "compiled_count": len(r.compiled), "compiled": r.compiled}
    if not r.ok:
        step["log_tail"] = r.log[-3000:]
    return step


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cxx", default=os.environ.get("AFAR_CXX", "g++"))
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", "2")))
    parser.add_argument("--quick", action="store_true", help="steps 1-4 only (for a login node)")
    parser.add_argument("--build-timeout", type=int, default=1800)
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    args = parser.parse_args()

    tiny = Manifest(ROOT / "configs" / "experiments" / "smoke-tiny-v1.json")
    smoke = Manifest(ROOT / "configs" / "experiments" / "smoke-v1.json")
    trace = tiny.trace_path(tiny["traces"][0])
    label = os.environ.get("SLURM_JOB_ID") or f"{socket.gethostname().split('.')[0]}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    work = Path(tempfile.mkdtemp(prefix=f"afar-toolchain-{label}-", dir=os.environ.get("TMPDIR")))
    static = cb.has_static_libstdcxx(args.cxx)
    report = {
        "label": label, "started_at": results.now(), "host": socket.gethostname(), "quick": args.quick,
        "env": {"cxx": args.cxx, "cxx_version": version([args.cxx, "--version"]), "make": version(["make", "--version"]),
                "python3": version(["python3", "--version"]), "static_libstdcxx": static,
                "prlimit": shutil.which("prlimit"), "cpu": cpu_model(), "jobs": args.jobs, "work": str(work)},
        "steps": {},
    }
    steps = report["steps"]
    print(f"toolchain test {label} on {report['host']}: {report['env']['cxx_version']}, "
          f"static libstdc++: {static}, make: {report['env']['make']}, jobs: {args.jobs}")

    try:
        # 2. Full build of LRU.
        tree_lru = cb.prepare_tree(work / "tree-lru")
        cfg = cb.configure(tree_lru, cb.candidate_config("lru"), work / "bin-lru")
        if not cfg.ok:
            steps["configure_lru"] = {"ok": False, "log_tail": cfg.log[-3000:]}
            raise SystemExit("config.sh failed")
        steps["full_build_lru"] = build_step(cb.build(tree_lru, args.cxx, args.jobs, args.build_timeout, static))
        print(f"  full build (lru): ok={steps['full_build_lru']['ok']} {steps['full_build_lru']['seconds']}s, "
              f"{steps['full_build_lru']['compiled_count']} objects")
        if not steps["full_build_lru"]["ok"]:
            raise SystemExit("full build failed (gate G1)")

        # 3. smoke-tiny with LRU.
        steps["smoke_tiny_lru"] = run_manifest(work / "bin-lru" / "champsim", tiny, trace, work, "tiny-lru", 300)
        steps["smoke_tiny_lru"]["expected_cycles"] = SMOKE_TINY_CYCLES
        print(f"  smoke-tiny (lru): {steps['smoke_tiny_lru'].get('cycles')} cycles (expect {SMOKE_TINY_CYCLES})")

        # 4. Incremental: copy the built tree, add the seed, rebuild.
        tree_seed = cb.copy_tree(tree_lru, work / "tree-seed")
        repl = work / "repl" / "evolved_repl"
        repl.mkdir(parents=True)
        shutil.copy2(ROOT / "seeds" / "evolved_repl.h", repl / "evolved_repl.h")
        cfg = cb.configure(tree_seed, cb.candidate_config("evolved_repl"), work / "bin-seed", replacement_dir=work / "repl")
        if not cfg.ok:
            steps["configure_seed"] = {"ok": False, "log_tail": cfg.log[-3000:]}
            raise SystemExit("config.sh failed for the seed")
        steps["incremental_build_seed"] = build_step(cb.build(tree_seed, args.cxx, args.jobs, args.build_timeout, static))
        inc = steps["incremental_build_seed"]
        print(f"  incremental build (seed after copy): ok={inc['ok']} {inc['seconds']}s, "
              f"recompiled {inc['compiled_count']}: {', '.join(inc['compiled'])}")
        if inc["ok"]:
            steps["smoke_tiny_seed"] = run_manifest(work / "bin-seed" / "champsim", tiny, trace, work, "tiny-seed", 300)
            steps["smoke_tiny_seed"]["expected_cycles"] = SMOKE_TINY_CYCLES
            print(f"  smoke-tiny (seed): {steps['smoke_tiny_seed'].get('cycles')} cycles (expect {SMOKE_TINY_CYCLES})")

        if not args.quick:
            # 5. Built-in SRRIP, incremental.
            tree_srrip = cb.copy_tree(tree_lru, work / "tree-srrip")
            cfg = cb.configure(tree_srrip, cb.candidate_config("srrip"), work / "bin-srrip")
            steps["incremental_build_srrip"] = build_step(cb.build(tree_srrip, args.cxx, args.jobs, args.build_timeout, static))
            if cfg.ok and steps["incremental_build_srrip"]["ok"]:
                steps["smoke_tiny_srrip"] = run_manifest(work / "bin-srrip" / "champsim", tiny, trace, work, "tiny-srrip", 300)
                print(f"  smoke-tiny (srrip): {steps['smoke_tiny_srrip'].get('cycles')} cycles")

            # 6. smoke-v1 with the trace on /data, then on node-local tmp.
            limit = smoke["limits"]["run_timeout_s"]
            steps["smoke_v1_trace_on_data"] = run_manifest(work / "bin-lru" / "champsim", smoke, trace, work, "v1-data", limit)
            local = work / trace.name
            shutil.copy2(trace, local)
            steps["smoke_v1_trace_local"] = run_manifest(work / "bin-lru" / "champsim", smoke, local, work, "v1-local", limit)
            print(f"  smoke-v1: trace on /data {steps['smoke_v1_trace_on_data']['seconds']}s, "
                  f"local copy {steps['smoke_v1_trace_local']['seconds']}s")
    finally:
        report["finished_at"] = results.now()
        out = Path(os.environ.get("AFAR_RESULTS", work)) / "toolchain" / f"{label}.json"
        results.write_atomic(out, report)
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        print(f"report: {out}")

    # Gates.
    full, inc = steps.get("full_build_lru", {}), steps.get("incremental_build_seed", {})
    tiny_ok = all(steps.get(k, {}).get("cycles") == SMOKE_TINY_CYCLES for k in ("smoke_tiny_lru", "smoke_tiny_seed"))
    print("gates:")
    print(f"  G1 builds and links with {args.cxx} ({'static' if static else 'dynamic'} libstdc++): {full.get('ok')}")
    print(f"  smoke-tiny 43,703 cycles for both LRU and the seed: {tiny_ok}")
    if full.get("ok") and inc.get("ok"):
        print(f"  G2 incremental/full build time: {inc['seconds']}/{full['seconds']} s = {100 * inc['seconds'] / full['seconds']:.0f}%")
    if "smoke_v1_trace_local" in steps:
        print(f"  G3 smoke-v1 run: /data {steps['smoke_v1_trace_on_data']['seconds']} s vs local {steps['smoke_v1_trace_local']['seconds']} s")
    return 0 if full.get("ok") and inc.get("ok") and tiny_ok else 1


if __name__ == "__main__":
    sys.exit(main())
