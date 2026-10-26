"""R6: summarize an OpenEvolve run directory, one row per iteration.

Usage (or `make oe-summary RUN=<run dir>`):
    .venv/controller/bin/python scripts/oe_run_summary.py runs/toy-<jobid>

Reads <run>/logs/openevolve_*.log, the only place OpenEvolve records iterations
that failed (checkpoints hold only the programs that were kept). The patterns
match OpenEvolve 0.4.0's log lines (openevolve/process_parallel.py); the script
exits 1 if it finds no iterations, so a change in the log format fails loudly.
Standard library only.
"""

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

STAMP = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) - "
TOKENS = r"(?: \| tokens: \d+ \(prompt: (\d+), completion: (\d+)\))?$"
START = re.compile(STAMP + r".*Starting process-based evolution from iteration (\d+) for (\d+) iterations")
APPLIED = re.compile(STAMP + r".*Iteration (\d+): Program (\S+) \(parent: \S+\) completed in ([\d.]+)s" + TOKENS)
METRICS = re.compile(r"openevolve\.process_parallel - INFO - Metrics: (.*)")
EVALUATED = re.compile(r"openevolve\.evaluator - INFO - Evaluated program (\S+) in ([\d.]+)s")
ERROR = re.compile(STAMP + r".*Iteration (\d+) error: (.*?)" + TOKENS)
TIMEOUT = re.compile(STAMP + r".*Iteration (\d+) timed out after")
# "Shutdown requested" here means OpenEvolve got SIGTERM/SIGINT. (Its "Received
# signal 15" line is not a guide: forked workers log it at every normal exit.)
FINISHED = re.compile(r"Evolution completed - (.*)")


def parse_time(stamp):
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f")


def evaluation_outcome(metrics):
    """Read an applied candidate's Metrics line: scored, or why its evaluation failed."""
    m = dict(re.findall(r"(\w+)=([^,]*)", metrics))
    if m.get("timeout") in ("1.0000", "True"):
        return None, "evaluation timed out"
    error = m.get("error", "")
    if error and not re.fullmatch(r"[-\d.]+", error):
        return float(m.get("combined_score", 0)), f"evaluation error: {error}"
    if "combined_score" not in m:
        return None, "evaluation returned no combined_score"
    return float(m["combined_score"]), "scored"


def best_score(run):
    """combined_score of the saved best program: best/, else the newest checkpoint."""
    candidates = [run / "best" / "best_program_info.json"]
    checkpoints = sorted((run / "checkpoints").glob("checkpoint_*"), key=lambda p: int(p.name.split("_")[-1]))
    candidates += [p / "best_program_info.json" for p in reversed(checkpoints)]
    for path in candidates:
        if path.exists():
            info = json.loads(path.read_text())
            return info.get("metrics", {}).get("combined_score"), path
    return None, None


ARTIFACT_STATUS = re.compile(r"Program (\S+) returned artifacts: \{'status': '(\w+)'")


def contract_statuses(run, logs):
    """Program id -> the evaluator's `status` artifact (docs/experiment-contract.md).

    From the DEBUG log lines the jobs keep, which cover every program; the newest
    checkpoint holds only the programs still in the population, so it's a fallback."""
    statuses = {}
    for log in logs:
        for match in ARTIFACT_STATUS.finditer(log.read_text(errors="replace")):
            statuses[match.group(1)] = match.group(2)
    checkpoints = sorted((run / "checkpoints").glob("checkpoint_*"), key=lambda p: int(p.name.split("_")[-1]))
    if checkpoints and not statuses:
        for path in (checkpoints[-1] / "programs").glob("*.json"):
            program = json.loads(path.read_text())
            artifacts = json.loads(program.get("artifacts_json") or "{}")
            if "status" in artifacts:
                statuses[program["id"]] = artifacts["status"]
    return statuses


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", type=Path, help="OpenEvolve output directory (--output)")
    args = parser.parse_args()

    logs = sorted((args.run / "logs").glob("openevolve_*.log"))
    if not logs:
        sys.exit(f"oe_run_summary: no logs/openevolve_*.log under {args.run}")

    rows = []        # [iteration, outcome, score, worker seconds, completion tokens, program id]
    eval_secs = {}   # program id -> seconds the evaluator took
    start = last = None
    finished = []
    pending = None   # the applied row still waiting for its Metrics line
    for log in logs:
        for line in log.read_text(errors="replace").splitlines():
            if m := START.match(line):
                start = start or parse_time(m[1])
            elif m := APPLIED.match(line):
                pending = [int(m[2]), "applied", None, float(m[4]), m[6], m[3]]
                rows.append(pending)
                last = parse_time(m[1])
            elif pending and (m := METRICS.search(line)):
                pending[2], outcome = evaluation_outcome(m[1])
                pending[1] = f"applied; {outcome}"
                pending = None
            elif m := EVALUATED.search(line):
                eval_secs[m[1]] = float(m[2])
            elif m := ERROR.match(line):
                rows.append([int(m[2]), m[3], None, None, m[5], None])
                last = parse_time(m[1])
            elif m := TIMEOUT.match(line):
                rows.append([int(m[2]), "timed out", None, None, None, None])
                last = parse_time(m[1])
            elif m := FINISHED.search(line):
                finished.append(m[1])

    if not rows:
        sys.exit(f"oe_run_summary: no iteration lines in {', '.join(map(str, logs))} "
                 "(not an OpenEvolve 0.4.0 log?)")

    # An evaluator that follows the contract reports a status; prefer it to the metrics.
    statuses = contract_statuses(args.run, logs)
    for row in rows:
        if row[5] in statuses:
            row[1] = f"applied; {statuses[row[5]]}"
    success = ("applied; scored", "applied; completed")

    print(f"run: {args.run}  logs: {', '.join(p.name for p in logs)}")
    # Runs from slurm/openevolve.sbatch record the parts they were assembled from.
    run_json = args.run / "run.json"
    if run_json.exists():
        run = json.loads(run_json.read_text())
        ev = run["evaluator"]
        print(f"parts: model {run['model']['profile']}, prompt {run['prompt']['profile']}, search {run['search']['profile']}, "
              f"evaluator {ev.get('policy') or ev.get('openevolve_file')}"
              f"{' on ' + ev['experiment'] if 'experiment' in ev else ''}; jobs {[j['job_id'] for j in run['jobs']]}")
    # secs = the worker's whole iteration; eval = the evaluator's part of it; the
    # rest is almost all waiting for the LLM.
    fmt = lambda x, spec: "" if x is None else format(x, spec)
    print(f"{'iter':>5}  {'score':>7}  {'secs':>6}  {'eval':>6}  {'tokens':>6}  outcome")
    for it, outcome, score, secs, tokens, pid in rows:
        print(f"{it:>5}  {fmt(score, '.4f'):>7}  {fmt(secs, '.1f'):>6}  "
              f"{fmt(eval_secs.get(pid), '.1f'):>6}  {tokens or '':>6}  {outcome}")

    applied = [r for r in rows if r[1].startswith("applied")]
    scored = [r for r in applied if r[1] in success]
    print(f"\niterations: {len(rows)}  applied: {len(applied)} (scored: {len(scored)}, "
          f"evaluation failed: {len(applied) - len(scored)})  rejected: {len(rows) - len(applied)}")
    for message, count in Counter(r[1] for r in rows if r[1] not in success).most_common():
        print(f"  {count:>3} x {message[:100]}")
    if start and last and rows:
        print(f"wall seconds per iteration: {(last - start).total_seconds() / len(rows):.1f}")
    timed = [(r[3], eval_secs[r[5]]) for r in applied if r[5] in eval_secs]
    if timed:
        print(f"per applied iteration: LLM about {statistics.mean(w - e for w, e in timed):.1f} s, "
              f"evaluation {statistics.mean(e for _, e in timed):.1f} s (means)")
    tokens = [int(r[4]) for r in rows if r[4]]
    if tokens:
        print(f"completion tokens per LLM call: mean {statistics.mean(tokens):.0f}, max {max(tokens)}")
    score, path = best_score(args.run)
    print(f"best combined_score: {'none saved' if score is None else f'{score:.4f} ({path.relative_to(args.run)})'}")
    print(f"finished: {finished[-1] if finished else 'no completion line (killed?)'}")


if __name__ == "__main__":
    main()
