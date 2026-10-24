"""M1: run the LRU baseline for every trace in a manifest and write result records.

    python -m evaluator.run_baseline --manifest configs/experiments/smoke-v1.json

For each trace it runs the baseline binary with the manifest's warmup and
simulation counts, under a process-group timeout, and writes
$AFAR_RESULTS/baselines/<experiment>/<trace>.json (the record) and
raw/<trace>.{log,champsim.json} beside it, so <experiment>/*.json are all records.

A baseline must succeed (contract §4: a missing baseline stops an
experiment), so any failure is recorded as infra_error and the exit code is 1.
A trace that already has a valid completed record for this exact manifest is
skipped, so a requeued job doesn't redo finished work. Standard library only.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from evaluator import manifest as manifest_mod
from evaluator import results
from evaluator.proc import run_killable

ROOT = results.ROOT
LRU_SOURCES = [results.CHAMPSIM / "replacement" / "lru" / name for name in ("lru.h", "lru.cc")]


def results_dir():
    value = os.environ.get("AFAR_RESULTS")
    if not value:
        raise RuntimeError("AFAR_RESULTS is not set; source env/site.sh first")
    return Path(value)


def pin_problems(manifest, base_config):
    """Problems that make a baseline meaningless: wrong ChampSim commit or LLC policy."""
    found = []
    actual = results.champsim_commit()
    if actual != manifest["champsim"]["commit"]:
        found.append(f"ChampSim is at {actual}, but the manifest pins {manifest['champsim']['commit']}")
    if manifest["baseline"]["policy"] != "lru":
        found.append(f"only an lru baseline is supported, not {manifest['baseline']['policy']!r}")
    llc_policy = json.loads(Path(base_config).read_text()).get("LLC", {}).get("replacement")
    if llc_policy != manifest["baseline"]["policy"]:
        found.append(f"{base_config} sets LLC replacement {llc_policy!r}, not {manifest['baseline']['policy']!r}")
    return found


def tail(path, chars=2000):
    try:
        return Path(path).read_text(errors="replace")[-chars:]
    except OSError:
        return ""


def run_trace(manifest, trace, binary, base_config, out_dir, env, candidate_sha256, force):
    """Run one trace and return its record (already written to disk)."""
    record_path = out_dir / f"{trace['id']}.json"
    if record_path.exists() and not force:
        old = json.loads(record_path.read_text())
        if not results.validate(old) and old["status"] == "completed" and old["experiment_sha256"] == manifest.sha256:
            print(f"  skip  {trace['id']}: completed record for this manifest already exists")
            return old

    raw = out_dir / "raw"
    raw.mkdir(exist_ok=True)
    log_path = raw / f"{trace['id']}.log"
    json_path = raw / f"{trace['id']}.champsim.json"
    partial = json_path.with_name(json_path.name + ".partial")
    partial.unlink(missing_ok=True)
    common = dict(manifest=manifest, candidate_id="lru", candidate_sha256=candidate_sha256,
                  trace_id=trace["id"], env=env,
                  logs={"run_log": str(log_path), "champsim_json": str(json_path)})
    started = results.now()

    trace_path = manifest.trace_path(trace)
    if not trace_path.is_file():
        return finish(record_path, results.make_record(
            status="infra_error", error_summary=f"trace not found: {trace_path}",
            started_at=started, finished_at=results.now(), **common))

    sim = manifest["simulation"]
    cmd = [str(binary), "--warmup-instructions", str(sim["warmup_instructions"]),
           "--simulation-instructions", str(sim["simulation_instructions"]),
           "--json", str(partial), str(trace_path)]
    with open(log_path, "w") as log:
        code, _, seconds = run_killable(cmd, out_dir, manifest["limits"]["run_timeout_s"], stdout=log)
    finished = results.now()

    if code is None:
        summary = f"baseline run exceeded run_timeout_s={manifest['limits']['run_timeout_s']} and was killed"
    elif code != 0:
        summary = f"champsim exited {code}: {tail(log_path, 500)}"
    elif not partial.exists():
        summary = "champsim exited 0 but wrote no --json output"
    else:
        os.replace(partial, json_path)
        try:
            metrics = results.champsim_metrics(json_path)
        except (KeyError, IndexError, ValueError, ZeroDivisionError) as e:
            summary = f"unparseable ChampSim JSON after exit 0: {e!r}"
        else:
            return finish(record_path, results.make_record(
                status="completed", metrics=metrics, exit_code=0, run_seconds=round(seconds, 2),
                started_at=started, finished_at=finished, **common))
    return finish(record_path, results.make_record(
        status="infra_error", error_summary=summary, exit_code=code, run_seconds=round(seconds, 2),
        started_at=started, finished_at=finished, **common))


def finish(path, record):
    found = results.validate(record)
    if found:
        raise RuntimeError(f"refusing to write an invalid record to {path}: {found}")
    results.write_atomic(path, record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--binary", default=str(ROOT / "build" / "baseline" / "champsim"))
    parser.add_argument("--base-config", default=str(ROOT / "configs" / "champsim" / "baseline.json"))
    parser.add_argument("--expect-cycles", type=int, help="fail unless every trace gives exactly this many cycles")
    parser.add_argument("--force", action="store_true", help="rerun traces that already have a completed record")
    args = parser.parse_args()

    manifest = manifest_mod.Manifest(args.manifest)
    found = pin_problems(manifest, args.base_config)
    if found:
        for p in found:
            print(f"  FAIL  {p}")
        return 1
    out_dir = results_dir() / "baselines" / manifest.experiment_id
    out_dir.mkdir(parents=True, exist_ok=True)
    env = results.environment(args.binary, args.base_config, args.base_config)
    candidate_sha256 = results.sha256_bytes(*(p.read_bytes() for p in LRU_SOURCES))

    print(f"{manifest.experiment_id} (manifest {manifest.sha256[:12]}…): {len(manifest['traces'])} trace(s) -> {out_dir}")
    print(f"  binary {args.binary} ({env['cxx_version']}), champsim {env['champsim_commit'][:7]}, project {env['project_git_commit']}")
    bad = 0
    for trace in manifest["traces"]:
        record = run_trace(manifest, trace, args.binary, args.base_config, out_dir, env, candidate_sha256, args.force)
        if record["status"] != "completed":
            bad += 1
            print(f"  FAIL  {trace['id']}: {record['status']}: {record['error_summary'][:300]}")
            continue
        line = (f"  ok    {trace['id']}: {record['cycles']} cycles, ipc {record['ipc']:.4f}, "
                f"llc demand mpki {record['llc_demand_mpki']:.2f}, hit rate {record['llc_hit_rate']:.3f}, "
                f"{record['run_seconds']}s")
        if args.expect_cycles is not None and record["cycles"] != args.expect_cycles:
            bad += 1
            line = line.replace("  ok  ", "  FAIL", 1) + f" (expected {args.expect_cycles} cycles)"
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
