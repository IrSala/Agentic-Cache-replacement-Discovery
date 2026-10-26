"""Trace calibration (M3): which traces separate replacement policies, and how short a run can be.

    python -m evaluator.calibrate --plan               write the calibration manifests, print the array commands
    python -m evaluator.calibrate --analyze [--write]  report from the records; --write adds the experiment manifests
    python -m evaluator.calibrate --check-baselines    their LRU baselines must reproduce calibration's cycles
    python -m evaluator.calibrate --dev                write crc2-dev-v1, the M4 development manifest
    python -m evaluator.calibrate --selftest

The calibration manifests (configs/experiments/crc2-cal-<length>-v1.json) hold
all 19 CRC-2 traces pinned in configs/traces/crc2-archagent19.json, at three
lengths. slurm/calibrate_array.sbatch runs each (policy, trace) pair as one
array task: ChampSim's built-in LRU, SRRIP, DRRIP and SHiP, unscored, into
$AFAR_RESULTS/calibration/<manifest>/builtin-<policy>/. These rules were fixed
before any result (docs/trace-calibration.md):

  admission  at the reference length: LRU demand MPKI > 1, and the four
             policies' IPCs spread by at least 1% of LRU's. A trace that ends
             before the requested instruction count is excluded.
  length     the shortest of L1 and L2 whose suite geomean speedups (admitted
             traces) put SRRIP, DRRIP and SHiP in the reference's order, each
             within 0.5 percentage points of its reference value. L2, flagged,
             if neither qualifies.
  issue 11   per trace and length, the greatest common divisor of the cycle
             differences between policies, and whether DRRIP equals LRU.

--write turns the decision into crc2-evolve-v1.json (admitted traces at the
chosen length) and crc2-validate-v1.json (the same traces at the reference
length). Manifests are immutable once used, so an existing file is never
overwritten with different content.
"""

import argparse
import json
import math
import os
import sys
from functools import reduce
from pathlib import Path

from evaluator import results

ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS = ROOT / "configs" / "experiments"
PIN = ROOT / "configs" / "traces" / "crc2-archagent19.json"
TEMPLATE = EXPERIMENTS / "smoke-v1.json"
POLICIES = ("lru", "srrip", "drrip", "ship")
CONTENDERS = ("srrip", "drrip", "ship")

# length -> warmup, simulation, run_timeout_s, Slurm --time. A run costs about
# 3.1 us per instruction plus 4.5 us per simulated cycle on the E5-2660 v4
# (coe-hpc1 and HPC3's nodes), so the slowest trace, mcf_46B at IPC 0.13, runs
# at about 26K instructions/s: 13 min, 46 min and 2.7 h for these lengths.
# Timeouts allow about 2.5x that.
LENGTHS = {
    "L1": (10_000_000, 10_000_000, 2400, "01:00:00"),
    "L2": (20_000_000, 50_000_000, 7200, "02:30:00"),
    "ref": (50_000_000, 200_000_000, 25200, "07:30:00"),
}
TINY = (10_000, 10_000, 300)   # crc2-tiny-v1: does every trace load? (seconds, any host)
REFERENCE, CANDIDATE_LENGTHS = "ref", ("L1", "L2")
FINAL = (("crc2-evolve-v1", "evolution"), ("crc2-validate-v1", "validation"))
# crc2-dev-v1 (M4): admitted traces with large spreads between the built-ins at
# L1 (58%, 16%, 21%, 8%), with SHiP, SRRIP and DRRIP each best on at least one,
# and runs of at most 6 min, so a candidate takes about 6 min on 4 CPUs. For testing the
# evolution pipeline, never for claims.
DEV = ("sphinx3_2520B", "cactusADM_734B", "soplex_66B", "omnetpp_340B")
DEV_ID, DEV_LENGTH = "crc2-dev-v1", "L1"
MPKI_MIN, SPREAD_MIN, SPEEDUP_TOLERANCE = 1.0, 0.01, 0.005
STEP_MIN = 1000
ARRAY_LIMIT = 40


def cal_id(length):
    return f"crc2-cal-{length}-v1"


def results_dir():
    value = os.environ.get("AFAR_RESULTS")
    if not value:
        sys.exit("calibrate: AFAR_RESULTS is not set; source env/site.sh first")
    return Path(value)


def manifest_data(experiment_id, purpose, traces, length):
    warmup, sim, run_timeout = TINY if length == "tiny" else LENGTHS[length][:3]
    data = json.loads(TEMPLATE.read_text())
    data.update(experiment_id=experiment_id, purpose=purpose,
                simulation={"warmup_instructions": warmup, "simulation_instructions": sim},
                traces=[{"id": t["id"], "path": t["path"], "sha256": t["sha256"]} for t in traces])
    data["limits"]["run_timeout_s"] = run_timeout
    return data


def write_manifest(data):
    """Write configs/experiments/<id>.json; refuse to change an existing one."""
    path = EXPERIMENTS / f"{data['experiment_id']}.json"
    text = json.dumps(data, indent=2) + "\n"
    if path.exists() and path.read_text() != text:
        sys.exit(f"calibrate: {path} exists with different content; manifests are immutable, so make a -v2")
    path.write_text(text)
    return path


# --- planning ---------------------------------------------------------------

def plan():
    pin = json.loads(PIN.read_text())
    traces = pin["traces"]
    path = write_manifest(manifest_data(
        "crc2-tiny-v1", f"Format check (docs/trace-calibration.md): the {len(traces)} CRC-2 traces at 10K + 10K "
        "instructions, so each loads and gives a valid record in seconds. Not for campaigns.", traces, "tiny"))
    print(f"{path.relative_to(ROOT)}: {len(traces)} traces, 10,000 + 10,000 instructions")
    for length, (warmup, sim, _, _) in LENGTHS.items():
        short = [t["id"] for t in traces if t["instructions"] < warmup + sim]
        role = "the reference length" if length == REFERENCE else "a candidate evolution length"
        path = write_manifest(manifest_data(
            cal_id(length),
            f"M3 trace calibration (docs/trace-calibration.md), {role}: the {len(traces)} CRC-2 traces "
            f"ArchAgent used, at {warmup // 10**6}M + {sim // 10**6}M instructions. Run unscored with "
            "slurm/calibrate_array.sbatch; not for campaigns.", traces, length))
        print(f"{path.relative_to(ROOT)}: {len(traces)} traces, {warmup:,} + {sim:,} instructions"
              + (f"; shorter than that: {', '.join(short)}" if short else ""))
    tasks = len(POLICIES) * len(traces)
    print(f"\n{tasks} tasks per length ({len(POLICIES)} policies x {len(traces)} traces). Submit on g17:")
    for length, (_, _, _, wall) in LENGTHS.items():
        print(f"  sbatch --array=0-{tasks - 1}%{ARRAY_LIMIT} --time={wall} "
              f"--export=ALL,MANIFEST={cal_id(length)} slurm/calibrate_array.sbatch")
    return 0


# --- analysis ---------------------------------------------------------------

def load(root, length, traces):
    """(policy, trace) -> record, and a list of problems (missing, failed or invalid)."""
    records, problems = {}, []
    for policy in POLICIES:
        for tid in traces:
            path = root / "calibration" / cal_id(length) / f"builtin-{policy}" / f"{tid}.json"
            if not path.exists():
                problems.append(f"{length} {policy} {tid}: no record")
                continue
            record = json.loads(path.read_text())
            found = results.validate(record)
            if found:
                problems.append(f"{length} {policy} {tid}: invalid record ({found[0]})")
            elif record["status"] != "completed":
                problems.append(f"{length} {policy} {tid}: {record['status']}")
            else:
                records[policy, tid] = record
    return records, problems


def geomean(values):
    return math.exp(math.fsum(math.log(v) for v in values) / len(values))


def step(cycles):
    """GCD of the cycle differences between policies (0 when all are equal)."""
    return reduce(math.gcd, (abs(c - cycles[0]) for c in cycles[1:]), 0)


def analyze(data, sims):
    """The pre-set rules applied to data: length -> (policy, trace) -> record (or its metrics).

    sims: length -> requested simulation instructions. Returns a dict for the report."""
    ref = data[REFERENCE]
    traces = sorted({t for _, t in ref})
    per_trace, admitted = {}, []
    for tid in traces:
        m = {p: ref[p, tid] for p in POLICIES}
        ipcs = [m[p]["ipc"] for p in POLICIES]
        lru = m["lru"]
        row = {"lru_ipc": lru["ipc"], "lru_mpki": lru["llc_demand_mpki"], "lru_hit_rate": lru["llc_hit_rate"],
               "spread": (max(ipcs) - min(ipcs)) / lru["ipc"],
               "speedups": {p: m[p]["ipc"] / lru["ipc"] for p in CONTENDERS},
               "complete": all(m[p]["instructions"] >= sims[REFERENCE] for p in POLICIES)}
        row["admitted"] = row["complete"] and row["lru_mpki"] > MPKI_MIN and row["spread"] >= SPREAD_MIN
        per_trace[tid] = row
        admitted += [tid] * row["admitted"]

    def suite(length):
        if not admitted:
            return {}
        return {p: geomean([data[length][p, t]["ipc"] / data[length]["lru", t]["ipc"] for t in admitted])
                for p in CONTENDERS}

    suites = {length: suite(length) for length in data}
    order = lambda s: sorted(s, key=s.get, reverse=True)
    checks, chosen = {}, None
    for length in CANDIDATE_LENGTHS:
        if length not in data or not admitted:
            continue
        same_order = order(suites[length]) == order(suites[REFERENCE])
        worst = max(abs(suites[length][p] - suites[REFERENCE][p]) for p in CONTENDERS)
        best_agree = sum(max(CONTENDERS + ("lru",), key=lambda p: data[length][p, t]["ipc"])
                         == max(CONTENDERS + ("lru",), key=lambda p: ref[p, t]["ipc"]) for t in admitted)
        checks[length] = {"same_order": same_order, "worst_error": worst, "best_policy_agrees": best_agree,
                          "passes": same_order and worst <= SPEEDUP_TOLERANCE}
        if chosen is None and checks[length]["passes"]:
            chosen = length
    flagged = chosen is None
    chosen = chosen or CANDIDATE_LENGTHS[-1]

    steps = {length: {t: {"gcd": step([d[p, t]["cycles"] for p in POLICIES]),
                          "drrip_equals_lru": d["drrip", t]["cycles"] == d["lru", t]["cycles"]}
                      for t in traces} for length, d in data.items()}
    stepped = [t for t, s in steps[REFERENCE].items() if s["gcd"] > STEP_MIN]
    return {"traces": per_trace, "admitted": admitted, "suites": suites, "checks": checks,
            "chosen": chosen, "flagged": flagged, "steps": steps,
            "issue11_persists": len(stepped) > len(traces) / 2, "stepped_traces": stepped}


def report(result):
    pct = lambda x: f"{100 * (x - 1):+.2f}%"
    lines = ["## Per trace, at the reference length", "",
             "| Trace | LRU IPC | LRU demand MPKI | LRU hit rate | SRRIP | DRRIP | SHiP | Spread | Admitted |",
             "|---|---|---|---|---|---|---|---|---|"]
    for tid, r in result["traces"].items():
        why = "yes" if r["admitted"] else ("no: ended early" if not r["complete"] else
                                           "no: MPKI" if r["lru_mpki"] <= MPKI_MIN else "no: spread")
        lines.append(f"| {tid} | {r['lru_ipc']:.4f} | {r['lru_mpki']:.2f} | {r['lru_hit_rate']:.3f} | "
                     + " | ".join(pct(r["speedups"][p]) for p in CONTENDERS) + f" | {100 * r['spread']:.2f}% | {why} |")
    lines += ["", f"Admitted: {len(result['admitted'])} of {len(result['traces'])}.", "",
              "## Suite geomean speedup over LRU (admitted traces)", "",
              "| Length | SRRIP | DRRIP | SHiP | Same order as ref | Worst error | Best policy agrees | Passes |",
              "|---|---|---|---|---|---|---|---|"]
    for length, s in result["suites"].items():
        c = result["checks"].get(length)
        tail = (f" | {'yes' if c['same_order'] else 'no'} | {100 * c['worst_error']:.2f} pp | "
                f"{c['best_policy_agrees']}/{len(result['admitted'])} | {'yes' if c['passes'] else 'no'} |") if c else " | reference | | | |"
        lines.append(f"| {length} | " + " | ".join(pct(s[p]) for p in CONTENDERS) + tail)
    lines += ["", f"Evolution length: **{result['chosen']}**"
              + (" (flagged: neither L1 nor L2 met the rule)" if result["flagged"] else ""), "",
              "## Issue 11: cycle steps", "",
              "| Trace | " + " | ".join(f"{l} GCD" for l in result["steps"]) + " | DRRIP = LRU at ref |",
              "|---|" + "---|" * len(result["steps"]) + "---|"]
    for tid in result["traces"]:
        lines.append(f"| {tid} | " + " | ".join(f"{result['steps'][l][tid]['gcd']:,}" for l in result["steps"])
                     + f" | {'yes' if result['steps'][REFERENCE][tid]['drrip_equals_lru'] else 'no'} |")
    lines += ["", f"Traces with a common step over {STEP_MIN:,} cycles at ref: {len(result['stepped_traces'])} "
              f"of {len(result['traces'])}; issue 11 {'persists' if result['issue11_persists'] else 'does not persist'}."]
    return "\n".join(lines)


def run_analysis(write):
    pin = json.loads(PIN.read_text())
    traces = [t["id"] for t in pin["traces"]]
    root = results_dir()
    data, problems = {}, []
    for length in LENGTHS:
        records, found = load(root, length, traces)
        problems += found
        data[length] = records
        envs = {(r["env"]["champsim_commit"], r["env"]["cxx_version"], r["env"]["base_config_sha256"])
                for r in records.values()}
        if len(envs) > 1:
            problems.append(f"{length}: records come from more than one toolchain: {sorted(envs)}")
    if problems:
        print(f"{len(problems)} problem(s); the analysis needs every run completed:")
        for p in problems[:40]:
            print(f"  {p}")
        return 1
    result = analyze(data, {l: LENGTHS[l][1] for l in LENGTHS})
    print(report(result))
    out = root / "calibration" / "crc2-v1-analysis.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"\n-> {out}")
    if write:
        keep = [t for t in pin["traces"] if t["id"] in result["admitted"]]
        flag = (f" {result['chosen']} is the fallback, flagged: it ranks the built-in policies like the reference but "
                f"misses their suite speedups by more than {100 * SPEEDUP_TOLERANCE:.1f} pp, so confirm top "
                "candidates on crc2-validate-v1.") if result["flagged"] else ""
        lengths = {"crc2-evolve-v1": result["chosen"], "crc2-validate-v1": REFERENCE}
        for exp, role in FINAL:
            warmup, sim = LENGTHS[lengths[exp]][:2]
            path = write_manifest(manifest_data(
                exp, f"M3 {role} set (docs/trace-calibration.md): the {len(keep)} CRC-2 traces admitted by "
                     f"calibration, at {warmup // 10**6}M + {sim // 10**6}M instructions."
                     + (flag if role == "evolution" else ""), keep, lengths[exp]))
            print(f"-> {path.relative_to(ROOT)}")
        print(f"\nLRU baselines, one trace per task. Submit on g17:")
        for exp, _ in FINAL:
            print(f"  sbatch --array=0-{len(keep) - 1}%{ARRAY_LIMIT} --time={LENGTHS[lengths[exp]][3]} "
                  f"--export=ALL,MANIFEST={exp},TARGETS=baseline slurm/calibrate_array.sbatch")
        print("then: python -m evaluator.calibrate --check-baselines")
    return 0


def write_dev():
    pin = {t["id"]: t for t in json.loads(PIN.read_text())["traces"]}
    warmup, sim, _, wall = LENGTHS[DEV_LENGTH]
    path = write_manifest(manifest_data(
        DEV_ID, f"M4 development set (docs/run-specs.md): {len(DEV)} admitted CRC-2 traces with large spreads between "
                f"the built-in policies, at {warmup // 10**6}M + {sim // 10**6}M instructions, so a candidate takes "
                "minutes. For testing the evolution pipeline; not for claims (use crc2-evolve-v1 and crc2-validate-v1).",
        [pin[t] for t in DEV], DEV_LENGTH))
    print(f"-> {path.relative_to(ROOT)}")
    print(f"LRU baselines, one trace per task. Submit on g17:\n  sbatch --array=0-{len(DEV) - 1}%{ARRAY_LIMIT} --time={wall} "
          f"--export=ALL,MANIFEST={DEV_ID},TARGETS=baseline slurm/calibrate_array.sbatch")
    return 0


def check_baselines():
    """Each final manifest's LRU baselines must reproduce the calibration's LRU cycles exactly
    (same traces and length, a separate run, usually on other nodes)."""
    root, failures = results_dir(), 0
    for exp in [e for e, _ in FINAL] + [DEV_ID]:
        data = json.loads((EXPERIMENTS / f"{exp}.json").read_text())
        sim = data["simulation"]
        length = next(l for l, v in LENGTHS.items()
                      if v[:2] == (sim["warmup_instructions"], sim["simulation_instructions"]))
        same, problems = 0, []
        for t in data["traces"]:
            base = root / "baselines" / exp / f"{t['id']}.json"
            cal = root / "calibration" / cal_id(length) / "builtin-lru" / f"{t['id']}.json"
            if not base.exists():
                problems.append(f"{t['id']}: no baseline")
                continue
            b, c = json.loads(base.read_text()), json.loads(cal.read_text())
            if b["status"] != "completed":
                problems.append(f"{t['id']}: baseline {b['status']}")
            elif (b["cycles"], b["instructions"]) != (c["cycles"], c["instructions"]):
                problems.append(f"{t['id']}: {b['cycles']:,} cycles on {b['env']['hostname']}, "
                                f"{c['cycles']:,} in calibration on {c['env']['hostname']}")
            else:
                same += 1
        failures += len(problems)
        print(f"{exp} ({length}): {same}/{len(data['traces'])} baselines reproduce the calibration's LRU cycles exactly")
        for p in problems:
            print(f"  FAIL  {p}")
    return 1 if failures else 0


# --- selftest ---------------------------------------------------------------

def selftest():
    """The rules on synthetic metrics: admission, length choice and the step GCD."""
    def metrics(ipc, mpki=5.0, cycles=None, instructions=200_000_000):
        return {"ipc": ipc, "llc_demand_mpki": mpki, "llc_hit_rate": 0.5, "instructions": instructions,
                "cycles": cycles if cycles is not None else round(instructions / ipc)}

    sims = {l: LENGTHS[l][1] for l in LENGTHS}
    # trace -> (LRU MPKI, ref IPCs for lru/srrip/drrip/ship)
    shapes = {"good": (5.0, (1.00, 1.02, 1.03, 1.05)), "flat": (5.0, (1.00, 1.001, 1.002, 1.003)),
              "cachefit": (0.5, (1.00, 1.02, 1.03, 1.05)), "good2": (8.0, (0.50, 0.51, 0.52, 0.53))}
    data = {}
    for length, scale in (("ref", 0.0), ("L1", 0.03), ("L2", 0.002)):
        data[length] = {}
        for tid, (mpki, ipcs) in shapes.items():
            for p, ipc in zip(POLICIES, ipcs):
                # L1 distorts the contenders a lot, L2 a little
                skew = 1 + (scale * (POLICIES.index(p) - 2) if p != "lru" else 0)
                data[length][p, tid] = metrics(ipc * skew, mpki, instructions=sims[length])
    short = dict(data["ref"][("lru", "good2")], instructions=sims["ref"] - 1)
    cases = []
    r = analyze(data, sims)
    cases.append(("admission", r["admitted"] == ["good", "good2"]))
    cases.append(("L1 fails, L2 chosen", r["checks"]["L1"]["passes"] is False and r["chosen"] == "L2" and not r["flagged"]))
    trimmed = {l: d for l, d in data.items() if l != "L2"}
    r2 = analyze(trimmed, sims)
    cases.append(("neither passes: L2 flagged", r2["chosen"] == "L2" and r2["flagged"]))
    data2 = {l: dict(d) for l, d in data.items()}
    data2["ref"]["lru", "good2"] = short
    cases.append(("ended early: excluded", analyze(data2, sims)["admitted"] == ["good"]))
    cases.append(("step GCD", step([18624235, 18636235, 18624235, 18600235]) == 12000 and step([5, 5, 5, 5]) == 0))
    text = report(r)
    cases.append(("report renders", "Evolution length: **L2**" in text and "| good | " in text))

    failures = 0
    for name, ok in cases:
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    print("calibrate selftest passed" if failures == 0 else f"calibrate selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--plan", action="store_true")
    which.add_argument("--analyze", action="store_true")
    which.add_argument("--check-baselines", action="store_true",
                       help="do the final manifests' LRU baselines reproduce the calibration's cycles?")
    which.add_argument("--dev", action="store_true", help=f"write {DEV_ID}, the M4 development manifest")
    which.add_argument("--selftest", action="store_true")
    parser.add_argument("--write", action="store_true", help="with --analyze: write crc2-evolve-v1 and crc2-validate-v1")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if args.plan:
        return plan()
    if args.dev:
        return write_dev()
    if args.check_baselines:
        return check_baselines()
    return run_analysis(args.write)


if __name__ == "__main__":
    sys.exit(main())
