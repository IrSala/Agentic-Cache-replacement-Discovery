"""Result records (docs/experiment-contract.md §3). Standard library only.

One JSON record per (candidate, trace). Baselines (stock LRU) use the same
schema, with campaign_id and generation both null.

Metric definitions v1, from the ROI section of ChampSim's --json output:
    ipc              instructions / cycles (core 0)
    llc_demand_mpki  1000 * (LLC LOAD + RFO misses) / instructions
    llc_hit_rate     (LLC LOAD + RFO hits) / (LOAD + RFO hits + misses); 0.0 with no accesses

    python -m evaluator.results --validate FILE...
    python -m evaluator.results --selftest
"""

import argparse
import copy
import datetime
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAMPSIM = ROOT / "external" / "ChampSim"
SCHEMA_VERSION = 1

# status -> failure class (contract §5)
STATUSES = {
    "completed": "none",
    "invalid_candidate": "candidate",
    "compile_failed": "candidate",
    "runtime_failed": "candidate",
    "timeout": "candidate",
    "infra_error": "infra",
}
METRICS = {"ipc": float, "instructions": int, "cycles": int, "llc_demand_mpki": float, "llc_hit_rate": float}
IDENTITY = ("schema_version", "experiment_id", "experiment_sha256", "campaign_id", "generation",
            "candidate_id", "candidate_sha256", "trace_id")
OUTCOME = ("status", "failure_class", "error_summary", "exit_code", "build_seconds", "run_seconds")
PROVENANCE = ("logs", "env", "started_at", "finished_at")
LOG_KEYS = ("build_log", "run_log", "champsim_json")
ENV_KEYS = ("champsim_commit", "base_config_sha256", "config_sha256", "project_git_commit",
            "hostname", "cxx_version", "slurm_job_id", "slurm_array_task_id")
SHA256 = re.compile(r"[0-9a-f]{64}")
DEMAND = ("LOAD", "RFO")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(*chunks):
    h = hashlib.sha256()
    for chunk in chunks:
        h.update(chunk)
    return h.hexdigest()


def champsim_metrics(json_path):
    """Metrics v1 from the ROI section of a ChampSim --json file."""
    roi = json.loads(Path(json_path).read_text())[-1]["roi"]
    core = roi["cores"][0]
    instructions, cycles = int(core["instructions"]), int(core["cycles"])
    llc = roi["LLC"]
    hits = sum(sum(llc[t]["hit"]) for t in DEMAND)
    misses = sum(sum(llc[t]["miss"]) for t in DEMAND)
    return {
        "ipc": instructions / cycles,
        "instructions": instructions,
        "cycles": cycles,
        "llc_demand_mpki": 1000.0 * misses / instructions,
        "llc_hit_rate": hits / (hits + misses) if hits + misses else 0.0,
    }


def _git(args, cwd):
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def project_commit():
    """Short commit of this repo, with -dirty if tracked files have changed."""
    commit = _git(["rev-parse", "--short", "HEAD"], ROOT) or "unknown"
    dirty = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT, capture_output=True).returncode != 0
    return commit + ("-dirty" if dirty else "")


def champsim_commit():
    return _git(["rev-parse", "HEAD"], CHAMPSIM) or "unknown"


def compiler_of(binary):
    """The compiler recorded in a binary's ELF .comment section, e.g. 'GCC: (Spack GCC) 12.2.0'."""
    data = Path(binary).read_bytes()
    found = sorted({m.decode(errors="replace") for m in re.findall(rb"GCC: \([^\x00]*", data)})
    return "; ".join(found) or "unknown"


def environment(binary, base_config, config, cxx_version=None):
    """Provenance for a record. With no binary (a failed build), pass cxx_version instead."""
    return {
        "champsim_commit": champsim_commit(),
        "base_config_sha256": sha256_bytes(Path(base_config).read_bytes()),
        "config_sha256": sha256_bytes(Path(config).read_bytes()),
        "project_git_commit": project_commit(),
        "hostname": socket.gethostname(),
        "cxx_version": compiler_of(binary) if binary and Path(binary).exists() else (cxx_version or "unknown"),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
    }


def make_record(*, manifest, candidate_id, candidate_sha256, trace_id, status, env, logs,
                started_at, finished_at, metrics=None, campaign_id=None, generation=None,
                error_summary=None, exit_code=None, build_seconds=None, run_seconds=None):
    """Build a record; metrics are given only for status == 'completed'."""
    record = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": manifest.experiment_id,
        "experiment_sha256": manifest.sha256,
        "campaign_id": campaign_id,
        "generation": generation,
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha256,
        "trace_id": trace_id,
        "status": status,
        "failure_class": STATUSES.get(status, "unknown"),
        "error_summary": error_summary,
        "exit_code": exit_code,
        "build_seconds": build_seconds,
        "run_seconds": run_seconds,
        "logs": {key: logs.get(key) for key in LOG_KEYS},
        "env": env,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    record.update({key: (metrics or {}).get(key) for key in METRICS})
    return record


def validate(record):
    """Return a list of problems with a record (empty if valid)."""
    if not isinstance(record, dict):
        return ["not a result record (expected a JSON object)"]
    found = []
    expected = set(IDENTITY) | set(OUTCOME) | set(METRICS) | set(PROVENANCE)
    missing, extra = expected - set(record), set(record) - expected
    if missing:
        found.append(f"missing key(s): {', '.join(sorted(missing))}")
    if extra:
        found.append(f"unknown key(s): {', '.join(sorted(extra))}")
    if missing:
        return found

    if record["schema_version"] != SCHEMA_VERSION:
        found.append(f"schema_version must be {SCHEMA_VERSION}")
    status = record["status"]
    if status not in STATUSES:
        found.append(f"status {status!r} is not one of {', '.join(STATUSES)}")
    elif record["failure_class"] != STATUSES[status]:
        found.append(f"failure_class must be {STATUSES[status]!r} for status {status!r}")

    for key in ("experiment_sha256", "candidate_sha256"):
        if not (isinstance(record[key], str) and SHA256.fullmatch(record[key])):
            found.append(f"{key} must be 64 lowercase hex characters")
    for key in ("experiment_id", "candidate_id", "trace_id"):
        if not (isinstance(record[key], str) and record[key]):
            found.append(f"{key} must be a non-empty string")

    baseline = record["campaign_id"] is None and record["generation"] is None
    candidate = (isinstance(record["campaign_id"], str) and isinstance(record["generation"], int)
                 and not isinstance(record["generation"], bool) and record["generation"] >= 0)
    if not (baseline or candidate):
        found.append("campaign_id and generation must both be null (baseline) or both set (str, int >= 0)")

    if status == "completed":
        for key, kind in METRICS.items():
            value = record[key]
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) if kind is float else \
                isinstance(value, int) and not isinstance(value, bool)
            if not ok:
                found.append(f"{key} must be a {kind.__name__} when status is completed")
        if record["exit_code"] != 0:
            found.append("exit_code must be 0 when status is completed")
    else:
        set_metrics = [key for key in METRICS if record[key] is not None]
        if set_metrics:
            found.append(f"metrics must be null unless completed: {', '.join(set_metrics)}")
        if not record["error_summary"]:
            found.append("error_summary is required unless completed")

    for key in ("exit_code",):
        if record[key] is not None and (not isinstance(record[key], int) or isinstance(record[key], bool)):
            found.append(f"{key} must be an int or null")
    for key in ("build_seconds", "run_seconds"):
        if record[key] is not None and not isinstance(record[key], (int, float)):
            found.append(f"{key} must be a number or null")

    if not isinstance(record["logs"], dict) or set(record["logs"]) != set(LOG_KEYS):
        found.append(f"logs must have exactly: {', '.join(LOG_KEYS)}")
    env = record["env"]
    if not isinstance(env, dict) or set(env) != set(ENV_KEYS):
        found.append(f"env must have exactly: {', '.join(ENV_KEYS)}")
    else:
        for key in ("base_config_sha256", "config_sha256"):
            if not (isinstance(env[key], str) and SHA256.fullmatch(env[key])):
                found.append(f"env.{key} must be 64 lowercase hex characters")

    for key in ("started_at", "finished_at"):
        try:
            datetime.datetime.fromisoformat(record[key])
        except (TypeError, ValueError):
            found.append(f"{key} must be an ISO 8601 timestamp")
    return found


def write_atomic(path, data):
    """Write JSON so readers never see a partial file: temporary file, fsync, rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def selftest():
    class FakeManifest:
        experiment_id, sha256 = "selftest", "0" * 64

    env = {key: None for key in ENV_KEYS}
    env.update(champsim_commit="c" * 40, base_config_sha256="b" * 64, config_sha256="b" * 64,
               project_git_commit="abc1234", hostname="host", cxx_version="GCC: (GCC) 11.5.0")
    metrics = {"ipc": 0.25, "instructions": 10004, "cycles": 43703, "llc_demand_mpki": 21.99, "llc_hit_rate": 0.0}
    common = dict(manifest=FakeManifest, candidate_id="lru", candidate_sha256="a" * 64, trace_id="t",
                  env=env, logs={"run_log": "x.log"}, started_at=now(), finished_at=now())
    good = make_record(status="completed", metrics=metrics, exit_code=0, run_seconds=1.0, **common)
    failed = make_record(status="compile_failed", error_summary="error: x", exit_code=1,
                         campaign_id="c1", generation=0, **common)

    def broken(base, change):
        record = copy.deepcopy(base)
        change(record)
        return record

    cases = [
        ("completed baseline", good, None),
        ("compile_failed candidate", failed, None),
        ("unknown status", broken(good, lambda r: r.update(status="crashed")), "is not one of"),
        ("wrong failure class", broken(failed, lambda r: r.update(failure_class="infra")), "failure_class"),
        ("completed without ipc", broken(good, lambda r: r.update(ipc=None)), "ipc must be"),
        ("failed with metrics", broken(failed, lambda r: r.update(cycles=5)), "metrics must be null"),
        ("failed without summary", broken(failed, lambda r: r.update(error_summary=None)), "error_summary"),
        ("short sha256", broken(good, lambda r: r.update(candidate_sha256="abc")), "candidate_sha256"),
        ("campaign without generation", broken(good, lambda r: r.update(campaign_id="c1")), "must both be null"),
        ("bad timestamp", broken(good, lambda r: r.update(finished_at="yesterday")), "ISO 8601"),
        ("unknown key", broken(good, lambda r: r.update(score=1)), "unknown key(s): score"),
        ("not a record (a list)", [good], "not a result record"),
    ]
    failures = 0
    for name, record, needle in cases:
        found = validate(record)
        ok = (not found) if needle is None else any(needle in p for p in found)
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<28} {found[0] if found else 'valid'}")

    with tempfile.TemporaryDirectory() as work:
        path = Path(work, "sub", "r.json")
        write_atomic(path, good)
        ok = json.loads(path.read_text()) == good and [p.name for p in path.parent.iterdir()] == ["r.json"]
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {'atomic write, no temp file left':<28}")

    smoke = ROOT / "build" / "smoke.json"
    if smoke.exists():
        m = champsim_metrics(smoke)
        ok = m["instructions"] == 10004 and m["cycles"] == 43703
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {'metrics from build/smoke.json':<28} cycles={m['cycles']} "
              f"ipc={m['ipc']:.4f} mpki={m['llc_demand_mpki']:.2f}")
    else:
        print("  skip  metrics from build/smoke.json (run make smoke to include)")

    print("results selftest passed" if failures == 0 else f"results selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--validate", nargs="+", metavar="FILE")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if not args.validate:
        parser.error("use --validate FILE... or --selftest")
    bad = 0
    for name in args.validate:
        found = validate(json.loads(Path(name).read_text()))
        bad += bool(found)
        print(f"  {'ok  ' if not found else 'FAIL'}  {name}" + "".join(f"\n        {p}" for p in found))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
