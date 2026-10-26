"""OpenEvolve's evaluator for any registered policy (M4): the file a run spec's jobs give openevolve-run.

OpenEvolve imports this file and calls evaluate(program_path). Which policy,
experiment, seed and CPUs to use come from the run's run.json, written by
controller/runspec.py, so nothing here assumes one setup:
    AFAR_RUN_JSON   <run>/run.json (required)
    AFAR_CXX        the compiler (default g++)
Results go to $AFAR_RESULTS/evaluations/<experiment>/<policy>/<candidate id>/,
reused when the same candidate comes back, and each evaluation adds one line to
<run>/evaluations.jsonl. Infrastructure failures raise, as the contract
requires (OpenEvolve then retries once), rather than scoring the candidate.

Before a run starts, slurm/openevolve.sbatch checks this host with:
    python evaluator/openevolve_adapter.py --preflight --policy P --experiment M --seed S --jobs N
"""

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# OpenEvolve imports this file by path, so make the evaluator package importable.
sys.path.insert(0, str(ROOT))
from evaluator import evaluate_candidate as ec  # noqa: E402
from evaluator import results  # noqa: E402
from evaluator.manifest import Manifest  # noqa: E402
from evaluator.policies import Context, InfraError, get_policy  # noqa: E402

ARTIFACT_CHARS = 4000
ARTIFACT_KEYS = ("status", "reason", "compiler_output", "compiler")


def load_run():
    value = os.environ.get("AFAR_RUN_JSON")
    if not value:
        raise InfraError("AFAR_RUN_JSON is not set (slurm/openevolve.sbatch sets it to <run>/run.json)")
    return Path(value), json.loads(Path(value).read_text())


def summary_to_result(summary):
    """(metrics, artifacts) for OpenEvolve from an evaluate_candidate summary; staged campaigns use it too."""
    metrics = dict(summary.get("metrics") or {})
    metrics["combined_score"] = float(summary["fitness"] or 0.0)
    metrics.update({f"speedup_{t}": s for t, s in summary["speedups"].items()})
    artifacts = {}
    for key in ARTIFACT_KEYS:
        value = summary["artifacts"].get(key) or (summary.get(key) if key in ("status", "reason") else None)
        if value:
            artifacts[key] = str(value)[-ARTIFACT_CHARS:]
    return metrics, artifacts


def evaluate(program_path):
    from openevolve.evaluation_result import EvaluationResult

    run_json, run = load_run()
    ev = run["evaluator"]
    manifest = Manifest(ROOT / ev["manifest"])
    candidate = ec.candidate_from_text(Path(program_path).read_text(errors="replace"))
    root = ec.results_root()
    started = time.monotonic()
    summary = ec.evaluate(
        manifest, candidate,
        out_dir=root / "evaluations" / manifest.experiment_id / ev["policy"] / candidate.candidate_id,
        baseline_dir=root / "baselines" / manifest.experiment_id,
        policy=ev["policy"], campaign_id=run["run_id"], generation=0,
        cxx=os.environ.get("AFAR_CXX", "g++"), jobs=ev["cpus_per_candidate"], seed=ROOT / run["seed"]["path"])

    metrics, artifacts = summary_to_result(summary)
    line = {"at": results.now(), "job_id": os.environ.get("SLURM_JOB_ID"), "candidate_id": candidate.candidate_id,
            "status": summary["status"], "fitness": summary["fitness"], "cached": bool(summary.get("cached")),
            "seconds": round(time.monotonic() - started, 2)}
    with open(run_json.parent / "evaluations.jsonl", "a") as f:
        f.write(json.dumps(line) + "\n")
    return EvaluationResult(metrics=metrics, artifacts=artifacts)


def preflight(policy, experiment, seed, jobs, cxx):
    """The policy's own seconds-long check that this host can evaluate candidates."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="afar-preflight-") as work:
        ctx = Context(manifest=Manifest(ROOT / experiment), workspace=Path(work), cxx=cxx, jobs=jobs,
                      seed=ROOT / seed)
        get_policy(policy).preflight(ctx)
    print(f"preflight: {policy} on {Path(experiment).stem} ready on {socket.gethostname()} with {cxx} "
          f"in {time.monotonic() - started:.0f}s")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preflight", action="store_true", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--experiment", required=True, help="manifest path, relative to the repo")
    parser.add_argument("--seed", required=True)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--cxx", default=os.environ.get("AFAR_CXX", "g++"))
    args = parser.parse_args()
    try:
        preflight(args.policy, args.experiment, args.seed, args.jobs, args.cxx)
    except InfraError as e:
        print(f"preflight FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
