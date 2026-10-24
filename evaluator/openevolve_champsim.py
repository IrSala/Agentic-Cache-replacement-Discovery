"""OpenEvolve's evaluator for the champsim_ipc policy (M2): real builds, real runs, IPC fitness.

OpenEvolve imports this file and calls evaluate(program_path). Settings come
from the environment, so the same file serves any experiment:
    AFAR_EXPERIMENT   manifest path, or an id under configs/experiments/ (required)
    AFAR_CAMPAIGN     campaign id for the records (default "openevolve")
    AFAR_GENERATION   generation number for the records (default 0)
    AFAR_EVAL_JOBS    CPUs for the build and parallel traces (default 1)
    AFAR_CXX          the compiler (default g++)
Baselines come from $AFAR_RESULTS/baselines/<experiment>/ (evaluate_candidate --baseline).
Infrastructure failures raise, as the contract requires, rather than scoring the candidate.
"""

import os
import sys
from pathlib import Path

from openevolve.evaluation_result import EvaluationResult

ROOT = Path(__file__).resolve().parent.parent
# OpenEvolve imports this file by path, so make the evaluator package importable.
sys.path.insert(0, str(ROOT))
from evaluator import evaluate_candidate as ec  # noqa: E402
from evaluator.manifest import Manifest  # noqa: E402
from evaluator.policies import InfraError  # noqa: E402

ARTIFACT_CHARS = 4000


def manifest_path():
    value = os.environ.get("AFAR_EXPERIMENT")
    if not value:
        raise InfraError("AFAR_EXPERIMENT is not set (a manifest path or id)")
    path = Path(value)
    return path if path.suffix == ".json" else ROOT / "configs" / "experiments" / f"{value}.json"


def evaluate(program_path):
    manifest = Manifest(manifest_path())
    candidate = ec.candidate_from_text(Path(program_path).read_text(errors="replace"))
    root = ec.results_root()
    summary = ec.evaluate(
        manifest, candidate,
        out_dir=root / "evaluations" / manifest.experiment_id / candidate.candidate_id,
        baseline_dir=root / "baselines" / manifest.experiment_id,
        campaign_id=os.environ.get("AFAR_CAMPAIGN", "openevolve"),
        generation=int(os.environ.get("AFAR_GENERATION", "0")),
        cxx=os.environ.get("AFAR_CXX", "g++"), jobs=int(os.environ.get("AFAR_EVAL_JOBS", "1")))
    metrics = {"combined_score": float(summary["fitness"] or 0.0)}
    metrics.update({f"speedup_{t}": s for t, s in summary["speedups"].items()})
    artifacts = {"status": summary["status"]}
    for key in ("reason", "compiler_output"):
        value = summary["artifacts"].get(key) or (summary.get(key) if key == "reason" else None)
        if value:
            artifacts[key] = str(value)[-ARTIFACT_CHARS:]
    return EvaluationResult(metrics=metrics, artifacts=artifacts)
