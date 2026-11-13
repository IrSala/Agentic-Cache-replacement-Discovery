"""Cost and efficiency of staged campaigns (docs/campaigns.md, section 8): what a campaign did
with the CPUs and GPU it held, and what a spec will cost before it runs. Both read files only,
so they work from either cluster.

    python -m controller.report campaign CAMPAIGN [--json] [--sacct]
    python -m controller.report estimate SPEC [--json]

campaign  Per generation: the generation job's queue wait and run, the LLM's time, and the
          evaluation window (half, three quarters and all of its tasks done, from the array's
          submission), with the tail after half and the aggregation. For the campaign: the
          CPU-hours its evaluation tasks held (CPUs x each task's evaluation time) and spent
          simulating (the trace records' run_seconds), their ratio (the efficiency seff and
          jobstats report), CPU-hours per evaluation, and each trace's run times. Cached
          evaluations (a candidate scored before) cost nothing and are counted apart.
          --sacct (on g17 only: coe-hpc1's Slurm is another cluster) adds Slurm's TotalCPU and
          MaxRSS for the evaluation tasks.
estimate  Replays a candidate's traces longest first on cpus_per_candidate CPUs, as the
          champsim_ipc policy runs them (run_order), using the LRU baseline's run times. Those
          ran about 20% slower than the pilot's candidates did, which roughly offsets the wait
          for a generation's slowest task, so a generation's estimate is close and its CPU-hours
          err high (checked against crc2-evolve-c1). Scales to a generation and to
          max_wall_hours, and warns when max_cpu_hours would end the campaign first.
"""

import argparse
import datetime
import heapq
import json
import math
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from controller import campaign as cp  # noqa: E402
from controller import runspec  # noqa: E402
from controller.executor import SlurmExecutor  # noqa: E402
from evaluator import evaluate_candidate as ec  # noqa: E402
from evaluator.manifest import Manifest  # noqa: E402
from evaluator.policies import policy_class  # noqa: E402
from evaluator.policies.champsim_ipc import run_order  # noqa: E402

# Measured in the pilot, crc2-evolve-c1 (docs/campaigns.md, section 8):
TASK_OVERHEAD_S = 15         # syntax check, tree copy and build before a candidate's first trace (median 12 s)
GENERATE_S = 180             # a generation job once it starts: vLLM up in 95-115 s, 24 replies in 12-36 s
COMPLETED_SHARE = 0.8        # candidates that run every trace (146 of 183); the rest fail within about a minute


def T(text):
    return datetime.datetime.fromisoformat(text)


def minutes(a, b):
    return round((b - a).total_seconds() / 60, 1)


def at_fraction(values, q):
    """The value q of the way through sorted values (q=0.5: the median task)."""
    return values[min(len(values) - 1, int(q * len(values)))]


def log_start(path):
    """When a job script started: the date= on its log's first line."""
    try:
        m = re.search(r"date=(\S+)", path.read_text(errors="replace").split("\n", 1)[0])
    except OSError:
        return None
    return T(m.group(1)) if m else None


def trace_records(evals, cand):
    """trace id -> the candidate's record, from evaluations/<experiment>/<policy>/<candidate>/."""
    found = {}
    for f in (evals / cand).glob("*.json"):
        if not f.name.startswith("summary"):
            r = cp.read_json(f)
            if r.get("trace_id"):
                found[r["trace_id"]] = r
    return found


# --- what a campaign did -------------------------------------------------------

def campaign_report(c):
    events = cp.read_events(c)
    cpus = c.meta["evaluator"]["cpus_per_candidate"]
    manifest = c.manifest()
    evals = ec.results_root() / "evaluations" / manifest.experiment_id / c.meta["evaluator"]["policy"]
    submitted = [e for e in events if e["event"] == "submitted"]
    generated = {e["generation"]: e for e in events if e["event"] == "generated"}
    aggregated = {e["generation"]: T(e["at"]) for e in events if e["event"] == "aggregated"}
    rows, per_trace = [], {}
    held = simulated = gpu_s = 0.0
    fresh_n = cached_n = 0
    for g in sorted(int(p.name) for p in (c.path / "generations").glob("*") if p.name.isdigit()):
        row = {"generation": g}
        gen = generated.get(g)
        if gen:
            row.update(llm_seconds=gen.get("llm_seconds"), gpu=gen.get("gpu"))
            start = log_start(c.path / "logs" / f"generate-g{g}-{gen.get('job')}.out")
            sub = next((e for e in submitted if e["job_id"] == gen.get("job")), None)
            if start:
                row["gpu_run_min"] = minutes(start, T(gen["at"]))
                gpu_s += (T(gen["at"]) - start).total_seconds()
                if sub:
                    row["gpu_wait_min"] = minutes(T(sub["at"]), start)
        first = next((T(e["at"]) for e in submitted if e["stage"] == "evaluate" and e["generation"] == g), None)
        ends, cached = [], 0
        for f in sorted((c.gen_dir(g) / "results").glob("*.json")):
            if ".infra-" in f.name:
                continue
            r = cp.read_json(f)
            s = r["summary"]
            start, end = T(s["started_at"]), T(s["finished_at"])
            if s.get("cached") or (first and start < first):
                cached += 1              # scored before, in this or another campaign: no CPU time here
                continue
            held += cpus * (end - start).total_seconds()
            simulated += (s.get("metrics") or {}).get("run_seconds") or 0.0
            ends.append(end)
            if r["status"] == "completed":
                for tid, rec in trace_records(evals, r["candidate_id"]).items():
                    if rec.get("run_seconds") is not None:
                        per_trace.setdefault(tid, []).append(rec["run_seconds"])
        fresh_n += len(ends)
        cached_n += cached
        row.update(evaluated=len(ends), cached=cached)
        if ends and first:
            ends.sort()
            row.update(half_min=minutes(first, at_fraction(ends, 0.5)), three_quarters_min=minutes(first, at_fraction(ends, 0.75)),
                       last_min=minutes(first, ends[-1]))
            row["tail_min"] = round(row["last_min"] - row["half_min"], 1)
            if g in aggregated:
                row["aggregation_min"] = minutes(ends[-1], aggregated[g])
        rows.append(row)
    st = c.status
    traces = sorted(({"trace": t, "median_s": round(statistics.median(v)), "min_s": round(min(v)), "max_s": round(max(v)),
                      "share": round(sum(v) / sum(sum(x) for x in per_trace.values()), 3), "n": len(v)}
                     for t, v in per_trace.items()), key=lambda r: -r["median_s"])
    tails = [r["tail_min"] for r in rows if "tail_min" in r and r["evaluated"] > 1]
    return {
        "campaign": c.id, "spec": c.meta["spec"]["file"], "state": st["state"], "reason": st.get("reason"),
        "experiment": manifest.experiment_id, "cpus_per_candidate": cpus,
        "candidates_per_generation": c.params["candidates_per_generation"],
        "wall_hours": round(cp.wall_hours(c, None if st["state"] == "running" else T(st["updated_at"])), 2),
        "generations": rows,
        "evaluations": fresh_n, "cached": cached_n,
        "cpu_hours_held": round(held / 3600, 1), "cpu_hours_simulating": round(simulated / 3600, 1),
        "efficiency": round(simulated / held, 3) if held else None,
        "cpu_hours_per_evaluation": round(held / 3600 / fresh_n, 2) if fresh_n else None,
        "gpu_job_hours": round(gpu_s / 3600, 2), "median_tail_min": statistics.median(tails) if tails else None,
        "traces": traces, "budget": cp.read_json(c.path / "budget.json"),
    }


def parse_cpu_time(text):
    """Slurm's TotalCPU ([D-][HH:]MM:SS[.mmm]) in seconds."""
    days, _, rest = text.rpartition("-")
    parts = [float(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_bytes(text):
    m = re.match(r"([\d.]+)([KMGT]?)", text or "")
    return float(m.group(1)) * 1024 ** "_KMGT".index(m.group(2) or "_") if m else 0.0


def sacct_report(c):
    """Slurm's view of the evaluation tasks: TotalCPU over CPU time held, and peak memory (g17 only)."""
    jobs = sorted({e["job_id"] for e in cp.read_events(c) if e["event"] == "submitted" and e["stage"] == "evaluate"})
    held = used = 0.0
    peak, tasks = 0.0, 0
    for i in range(0, len(jobs), 200):
        out = SlurmExecutor(c.path, 1)._run(["sacct", "-n", "-P", "-o", "JobID,ElapsedRaw,AllocCPUS,TotalCPU,MaxRSS",
                                             "-j", ",".join(jobs[i:i + 200])])
        if out.returncode != 0:
            raise cp.CampaignError(f"sacct failed ({out.returncode}): {out.stderr.strip()} (run --sacct on g17)")
        for line in out.stdout.splitlines():
            job, elapsed, alloc, total, rss = (line.split("|") + [""] * 5)[:5]
            if job.endswith(".batch") and elapsed.isdigit():
                tasks += 1
                held += int(elapsed) * int(alloc or 0)
                used += parse_cpu_time(total) if total else 0.0
                peak = max(peak, parse_bytes(rss))
    request = max(2, c.meta["evaluator"]["cpus_per_candidate"])
    return {"tasks": tasks, "efficiency": round(used / held, 3) if held else None,
            "max_rss_gb": round(peak / 2 ** 30, 2), "mem_request_gb": request}


def print_campaign(rep, sacct=None):
    print(f"campaign {rep['campaign']} ({rep['spec']}): {rep['state']}" + (f" ({rep['reason']})" if rep["reason"] else "")
          + f", {rep['wall_hours']} h; {rep['experiment']}, {rep['candidates_per_generation']} candidates x "
          f"{rep['cpus_per_candidate']} CPUs")
    print("  gen  GPU wait  GPU job   LLM s  evaluated  cached    50%    75%   last   tail  aggr   (minutes)")
    for r in rep["generations"]:
        def f(k, w=6):
            v = r.get(k)
            return f"{v:>{w}}" if v is not None else " " * (w - 1) + "-"
        print(f"  {r['generation']:>3}  {f('gpu_wait_min', 8)}  {f('gpu_run_min', 7)}  {f('llm_seconds')}  "
              f"{r['evaluated']:>9}  {r['cached']:>6} {f('half_min')} {f('three_quarters_min')} {f('last_min')} "
              f"{f('tail_min')} {f('aggregation_min', 5)}" + (f"  {r['gpu']}" if r.get("gpu") else ""))
    eff = f"{rep['efficiency']:.0%}" if rep["efficiency"] is not None else "-"
    print(f"  evaluation tasks: {rep['evaluations']} run, {rep['cached']} cached (free); {rep['cpu_hours_held']:,} CPU-hours "
          f"held, {rep['cpu_hours_simulating']:,} simulating: efficiency {eff}; "
          f"{rep['cpu_hours_per_evaluation']} CPU-hours per evaluation")
    print(f"  generation jobs: {rep['gpu_job_hours']} GPU-hours running; median tail after half the tasks: "
          f"{rep['median_tail_min']} min")
    if rep["budget"]:
        b = rep["budget"]
        print(f"  sacct, every job: {b['cpu_hours']} CPU-hours, {b['gpu_hours']} GPU-hours (as of {b['at']})")
    if sacct:
        print(f"  sacct, evaluation tasks: {sacct['tasks']} tasks; CPU efficiency (TotalCPU / held) "
              f"{sacct['efficiency']:.0%}; peak memory {sacct['max_rss_gb']} GB of {sacct['mem_request_gb']} GB requested")
    if rep["traces"]:
        print("  trace run times, completed candidates (s):   median     min     max   share")
        for t in rep["traces"]:
            print(f"    {t['trace']:<40} {t['median_s']:>7} {t['min_s']:>7} {t['max_s']:>7}   {t['share']:.1%}")


# --- what a spec will cost -----------------------------------------------------

def makespan(times, workers):
    """The time `workers` CPUs take for these run times, taken in order, each by the first free CPU."""
    loads = [0.0] * max(1, workers)
    for t in times:
        heapq.heappush(loads, heapq.heappop(loads) + t)
    return max(loads)


def estimate(spec_ref):
    path = runspec.spec_path(spec_ref)
    spec = runspec.load_yaml(path, "spec")
    if "campaign" not in spec or "policy" not in spec.get("evaluator", {}):
        raise cp.CampaignError(f"{path} has no campaign: section or no evaluator policy")
    params = {**runspec.CAMPAIGN_DEFAULTS, **spec["campaign"]}
    ev = spec["evaluator"]
    cpus = ev.get("cpus_per_candidate", 1)
    manifest = Manifest(runspec.CONFIGS / "experiments" / f"{ev['experiment']}.json")
    baselines = ec.load_baselines(manifest, ec.results_root() / "baselines" / manifest.experiment_id)
    times = {tid: r["run_seconds"] for tid, r in baselines.items()}
    ordered = [times[t["id"]] for t in run_order(manifest["traces"], baselines)]
    candidate_s = makespan(ordered, cpus) + TASK_OVERHEAD_S
    n, limit = params["candidates_per_generation"], params["max_concurrent_tasks"]
    waves = math.ceil(n / limit)
    generation_s = GENERATE_S + waves * candidate_s
    per_generation = n * cpus * candidate_s / 3600
    expected = per_generation * COMPLETED_SHARE + n * (1 - COMPLETED_SHARE) * cpus * 60 / 3600
    out = {"spec": runspec.rel(path), "experiment": manifest.experiment_id, "cpus_per_candidate": cpus,
           "candidates_per_generation": n, "max_concurrent_tasks": limit, "cpus_held": min(n, limit) * cpus,
           "slowest_trace_s": round(max(ordered)), "candidate_minutes": round(candidate_s / 60, 1),
           "efficiency": round(sum(ordered) / (cpus * candidate_s), 3),
           "task_time_limit": cp.hms(policy_class(ev["policy"])().time_limit_s(manifest, cpus) + cp.EVALUATE_MARGIN_S),
           "generation_minutes": round(generation_s / 60, 1), "cpu_hours_per_generation_max": round(per_generation, 1),
           "cpu_hours_per_generation": round(expected, 1), "warnings": []}
    gens = params["generations"]
    if params.get("max_wall_hours"):
        gens = min(gens, int(params["max_wall_hours"] * 3600 // generation_s))
    out.update(generations=gens, hours=round(gens * generation_s / 3600, 1), cpu_hours=round(gens * expected))
    if params.get("max_cpu_hours") and params["max_cpu_hours"] < out["cpu_hours"]:
        reached = int(params["max_cpu_hours"] // expected)
        out["warnings"].append(f"max_cpu_hours {params['max_cpu_hours']} would halt the campaign (budget) after about "
                               f"{reached} generation(s), {reached * generation_s / 3600:.0f} h, before "
                               + (f"max_wall_hours {params['max_wall_hours']}" if params.get("max_wall_hours")
                                  else f"its {params['generations']} generations"))
    return out


def print_estimate(e):
    print(f"estimate for {e['spec']} ({e['experiment']}), from the LRU baseline's run times:")
    print(f"  a candidate: {e['candidate_minutes']} min on {e['cpus_per_candidate']} CPUs (slowest trace "
          f"{e['slowest_trace_s']} s), {e['efficiency']:.0%} of its CPU time simulating; task limit {e['task_time_limit']}")
    print(f"  a generation: {e['candidates_per_generation']} candidates, {e['cpus_held']} CPUs held, about "
          f"{e['generation_minutes']} min without GPU waits; {e['cpu_hours_per_generation']} CPU-hours "
          f"({e['cpu_hours_per_generation_max']} if every candidate runs every trace)")
    print(f"  the campaign: {e['generations']} generation(s) in about {e['hours']} h, about {e['cpu_hours']:,} CPU-hours")
    for w in e["warnings"]:
        print(f"  warning: {w}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("campaign")
    p.add_argument("campaign")
    p.add_argument("--json", action="store_true")
    p.add_argument("--sacct", action="store_true", help="add Slurm's CPU time and memory (on g17)")
    p = sub.add_parser("estimate")
    p.add_argument("spec")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "campaign":
            c = cp.Campaign(args.campaign)
            rep = campaign_report(c)
            sacct = sacct_report(c) if args.sacct else None
            if args.json:
                print(json.dumps({**rep, "sacct": sacct}, indent=1))
            else:
                print_campaign(rep, sacct)
        else:
            e = estimate(args.spec)
            print(json.dumps(e, indent=1)) if args.json else print_estimate(e)
    except (cp.CampaignError, runspec.SpecError, ec.InfraError) as e:
        print(f"report: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
