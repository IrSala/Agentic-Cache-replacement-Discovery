"""Evaluate one candidate against one experiment manifest (M2). Standard library only.

    python -m evaluator.evaluate_candidate --manifest configs/experiments/smoke-v1.json --baseline
    python -m evaluator.evaluate_candidate --manifest ... --candidate seeds/evolved_repl.h
    python -m evaluator.evaluate_candidate --manifest ... --builtin srrip
    python -m evaluator.evaluate_candidate --manifest ... --builtin ship --unscored --trace mcf_46B
    python -m evaluator.evaluate_candidate --selftest

Writes one result record per trace plus summary.json (status, fitness,
per-trace speedups, build details) to --out, by default
    $AFAR_RESULTS/baselines/<experiment>/                    (--baseline: stock LRU)
    $AFAR_RESULTS/calibration/<experiment>/<candidate id>/   (--unscored)
    $AFAR_RESULTS/evaluations/<experiment>/<policy>/<candidate id>/   (otherwise)
Candidates are scored against those baselines, so run --baseline first.
--unscored writes records like a baseline's (no campaign, no fitness), for
trace calibration (M3). --trace runs one of the manifest's traces, as one
Slurm array task does; its summary is summary-<trace>.json, so tasks that
share a directory don't overwrite each other's.

Exit status: 0 for any candidate outcome (a candidate failure is a result),
1 for an infrastructure failure (contract §5). An evaluation already done for
the same candidate, manifest and policy is reused unless --force; one that
reused another behavior's scores through a duplicate screen is reused only by
its own campaign (evaluator/screen.py).
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from evaluator import champsim_build as cb
from evaluator import proc, results
from evaluator import screen as screen_mod
from evaluator.manifest import Manifest
from evaluator.policies import REGISTRY, Candidate, Context, InfraError, get_policy, policy_class
from evaluator.policies.compile_only import SEED

ROOT = cb.ROOT


def candidate_from_text(code):
    sha = results.sha256_bytes(code.encode())
    return Candidate(candidate_id=sha[:16], sha256=sha, code=code)


def builtin_candidate(name):
    if not (cb.CHAMPSIM / "replacement" / name).is_dir():
        raise ValueError(f"no built-in replacement module {name!r} in {cb.CHAMPSIM / 'replacement'}")
    return Candidate(candidate_id=f"builtin:{name}", sha256=cb.module_sha256(name), builtin=name)


def baseline_candidate(manifest):
    policy = manifest["baseline"]["policy"]
    return Candidate(candidate_id=policy, sha256=cb.module_sha256(policy), builtin=policy)


def results_root():
    value = os.environ.get("AFAR_RESULTS")
    if not value:
        raise InfraError("AFAR_RESULTS is not set; source env/site.sh first")
    return Path(value)


def load_baselines(manifest, baseline_dir):
    """trace id -> completed baseline record for this exact manifest; InfraError if any is missing."""
    found, problems = {}, []
    for trace in manifest["traces"]:
        path = Path(baseline_dir) / f"{trace['id']}.json"
        if not path.exists():
            problems.append(f"{trace['id']}: no baseline at {path}")
            continue
        record = json.loads(path.read_text())
        if results.validate(record) or record["status"] != "completed" or record["experiment_sha256"] != manifest.sha256:
            problems.append(f"{trace['id']}: {path} isn't a completed baseline for this manifest")
            continue
        found[trace["id"]] = record
    if problems:
        raise InfraError("baselines unusable (run evaluate_candidate --baseline): " + "; ".join(problems))
    return found


def evaluate(manifest, candidate, *, out_dir, baseline_dir=None, baseline=False, unscored=False, trace=None,
             policy="champsim_ipc", campaign_id="manual", generation=0, cxx="g++", jobs=1, keep=False, force=False,
             seed=None, screen=None):
    """Evaluate and return the summary dict (also written to out_dir/summary.json,
    or summary-<trace>.json for one trace). screen: a duplicate screen for policies that support
    one, {trace, index, audit, evaluations} (evaluator/screen.py)."""
    out_dir = Path(out_dir)
    if screen and (trace is not None or baseline or unscored or not policy_class(policy).supports_screen):
        raise ValueError(f"a duplicate screen needs a scored, full evaluation by a policy that supports it, not {policy}")
    if trace is not None:
        manifest = manifest.restricted(trace)
    summary_path = out_dir / (f"summary-{trace}.json" if trace is not None else "summary.json")
    seed_sha = results.sha256_bytes(Path(seed or SEED).read_bytes())
    if summary_path.exists() and not force:
        old = json.loads(summary_path.read_text())
        if (old.get("experiment_sha256") == manifest.sha256 and old.get("candidate_sha256") == candidate.sha256
                and old.get("policy") == policy and old.get("seed_sha256", seed_sha) == seed_sha
                and old.get("status") != "infra_error"
                and (not screened(old) or old.get("campaign_id") == campaign_id)):
            old["cached"] = True
            return old

    if baseline or unscored:
        campaign_id, generation, baselines = None, None, None
    elif policy_class(policy).needs_baselines:
        baselines = load_baselines(manifest, baseline_dir)
    else:
        baselines = None
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f"afar-eval-{candidate.candidate_id.replace(':', '-')}-",
                                      dir=os.environ.get("TMPDIR")))
    ctx = Context(manifest=manifest, out_dir=out_dir, workspace=workspace, cxx=cxx, jobs=jobs,
                  campaign_id=campaign_id, generation=generation, baselines=baselines, seed=seed, screen=screen)
    started = results.now()
    try:
        outcome = get_policy(policy).evaluate(candidate, ctx)
    finally:
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)
    summary = {
        "schema_version": results.SCHEMA_VERSION, "experiment_id": manifest.experiment_id,
        "experiment_sha256": manifest.sha256, "candidate_id": candidate.candidate_id,
        "candidate_sha256": candidate.sha256, "seed_sha256": seed_sha, "campaign_id": campaign_id,
        "generation": generation, "policy": policy, "status": outcome.status, "fitness": outcome.fitness, "reason": outcome.reason,
        "metrics": outcome.metrics,
        "speedups": {k[len("speedup_"):]: v for k, v in outcome.metrics.items() if k.startswith("speedup_")},
        "traces": {r["trace_id"]: r["status"] for r in outcome.records},
        "cycles": {r["trace_id"]: r["cycles"] for r in outcome.records},
        "build": outcome.artifacts.get("build", {}), "artifacts": outcome.artifacts,
        "started_at": started, "finished_at": results.now(),
    }
    if outcome.artifacts.get("screen"):
        summary["screen"] = outcome.artifacts["screen"]
    results.write_atomic(summary_path, summary)
    return summary


def screened(summary):
    """Whether a summary reused another behavior's scores through a duplicate screen."""
    s = summary.get("screen") or {}
    return bool(s.get("matched")) and not s.get("audited") and not s.get("conflict")


def describe(summary):
    fit = "n/a (unscored)" if summary["fitness"] is None else repr(summary["fitness"])
    build = summary["build"]
    parts = [f"{summary['candidate_id']}: {summary['status']}, fitness {fit}"]
    if build.get("build_seconds") is not None:
        parts.append(f"build {build['build_seconds']}s ({build.get('tree', '')}, recompiled {len(build.get('recompiled', []))})")
    parts.append("cycles " + ", ".join(f"{t}={c}" for t, c in summary["cycles"].items()))
    s = summary.get("screen")
    if s:
        on = screen_mod.label(s["trace"])
        if screened(summary):
            parts.append(f"screened on {on}: the scores of {s['matched']} (generation {s['matched_generation']})")
        elif s.get("conflict"):
            parts.append(f"screen matched {s['matched']} on {on} but {s['conflict']} differed: evaluated in full")
        elif s.get("audited") and s.get("matched"):
            parts.append(f"screen audit: matched {s['matched']} on {on}, evaluated in full")
    if summary.get("reason"):
        parts.append("reason: " + summary["reason"].splitlines()[0][:160])
    return "; ".join(parts) + ("  [cached]" if summary.get("cached") else "")


def selftest(cxx, jobs):
    """Every contract status on variants of the seed, on a 10K-instruction manifest, in a temporary results dir."""
    seed = (ROOT / "seeds" / "evolved_repl.h").read_text()
    body = "    // Find the way whose last use cycle is most distant\n"
    inject = lambda line: seed.replace(body, f"    {line}\n" + body)
    # replacement_cache_fill runs on every LLC fill; find_victim runs only when a
    # set is full, and the update hook on hits, neither of which this short run has.
    fill = "    last_used_cycles.at((std::size_t)(set * NUM_WAY + way)) = cycle++;\n  }\n\n  void update_replacement_state"
    assert seed.count(fill) == 1
    cases = [
        ("seed reproduces LRU exactly", seed, "completed", 1.0),
        ("built-in srrip", "builtin:srrip", "completed", None),
        ("forbidden call", inject('std::system("true");'), "invalid_candidate", None),
        ("edit outside the block", seed.replace("#include <deque>\n", "#include <list>\n"), "invalid_candidate", None),
        ("syntax error", seed.replace("uint64_t cycle = 0;", "uint64_t cycle = 0"), "compile_failed", None),
        ("wrong hook signature", seed.replace("access_type type, uint8_t hit)", "access_type type, uint8_t* hit)"), "compile_failed", None),
        ("declared but undefined (link error)", seed.replace("  uint64_t cycle = 0;\n", "  uint64_t cycle = 0;\n  long helper(long);\n")
         .replace(body, "    (void)helper(set);\n" + body), "compile_failed", None),
        ("out-of-range access (uncaught throw)", seed.replace(fill, fill.replace("+ way))", "+ way) + 1000000000)")),
         "runtime_failed", None),
        ("endless loop in a hook", seed.replace(fill, "    for (volatile uint64_t spin = 0;; spin = spin + 1) {}\n" + fill),
         "timeout", None),
    ]
    with tempfile.TemporaryDirectory(prefix="afar-evaluate-selftest-") as tmp:
        tmp = Path(tmp)
        data = json.loads((ROOT / "configs" / "experiments" / "smoke-tiny-v1.json").read_text())
        data.update(experiment_id="selftest-tiny")
        data["limits"]["run_timeout_s"] = 20
        manifest_path = tmp / "selftest-tiny.json"
        manifest_path.write_text(json.dumps(data, indent=2))
        manifest = Manifest(manifest_path)
        common = dict(cxx=cxx, jobs=jobs)

        failures = 0
        base = evaluate(manifest, baseline_candidate(manifest), out_dir=tmp / "baselines", baseline=True, **common)
        ok = base["status"] == "completed" and base["cycles"] == {"602.gcc_s-1850B": 43703}
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {'LRU baseline (43,703 cycles)':<38} {describe(base)}")
        for name, text, want, want_fitness in cases:
            cand = builtin_candidate(text.split(":", 1)[1]) if text.startswith("builtin:") else candidate_from_text(text)
            s = evaluate(manifest, cand, out_dir=tmp / "eval" / cand.candidate_id.replace(":", "-"),
                         baseline_dir=tmp / "baselines", **common)
            ok = s["status"] == want and (want_fitness is None or s["fitness"] == want_fitness)
            if want != "completed":
                ok = ok and s["fitness"] == 0.0 and all(v == want for v in s["traces"].values())
            failures += not ok
            print(f"  {'ok  ' if ok else 'FAIL'}  {name:<38} {describe(s)}")

        # One trace, unscored, as a calibration array task runs it.
        tid = manifest["traces"][0]["id"]
        s = evaluate(manifest, builtin_candidate("lru"), out_dir=tmp / "calibration", unscored=True, trace=tid, **common)
        record = json.loads((tmp / "calibration" / f"{tid}.json").read_text())
        ok = (s["status"] == "completed" and s["fitness"] is None and s["cycles"] == {tid: 43703}
              and (tmp / "calibration" / f"summary-{tid}.json").exists() and record["campaign_id"] is None
              and record["experiment_sha256"] == manifest.sha256)
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {'one trace, unscored (array task)':<38} {describe(s)}")

        # The contract every registered policy must meet, so a new policy is tested by being registered.
        contract = [("seed", seed, "completed"), ("forbidden call", inject('std::system("true");'), "invalid_candidate"),
                    ("edit outside the block", seed.replace("#include <deque>\n", "#include <list>\n"),
                     "invalid_candidate"),
                    ("syntax error", seed.replace("uint64_t cycle = 0;", "uint64_t cycle = 0"), "compile_failed")]
        for policy in sorted(REGISTRY):
            for name, text, want in contract:
                cand = candidate_from_text(text)
                s = evaluate(manifest, cand, out_dir=tmp / "contract" / policy / cand.candidate_id,
                             baseline_dir=tmp / "baselines", policy=policy, **common)
                ok = s["status"] == want and (s["fitness"] == 1.0 if name == "seed" else s["fitness"] == 0.0)
                failures += not ok
                print(f"  {'ok  ' if ok else 'FAIL'}  {f'{policy}: {name}':<38} {s['status']}, fitness {s['fitness']}")

        failures += screen_selftest(tmp, seed, body, common)
    print("evaluate selftest passed" if failures == 0 else f"evaluate selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


def screen_selftest(tmp, seed, body, common):
    """The duplicate screen (evaluator/screen.py) on three tiny traces. At 10K instructions no LLC set
    fills, so every policy behaves as LRU does, and a comment-only variant of the seed matches it."""
    data = json.loads((ROOT / "configs" / "experiments" / "crc2-tiny-v1.json").read_text())
    data.update(experiment_id="selftest-screen")
    data["traces"] = [t for t in data["traces"] if t["id"] in ("bzip2_183B", "mcf_46B", "astar_163B")]
    path = tmp / "selftest-screen.json"
    path.write_text(json.dumps(data, indent=2))
    manifest = Manifest(path)
    ids = [t["id"] for t in manifest["traces"]]
    base_dir, evals = tmp / "screen-baselines", tmp / "screen-evals"
    evaluate(manifest, baseline_candidate(manifest), out_dir=base_dir, baseline=True, **common)
    first = candidate_from_text(seed)
    s0 = evaluate(manifest, first, out_dir=evals / first.candidate_id, baseline_dir=base_dir,
                  campaign_id="selftest", generation=0, **common)
    rec = {t: json.loads((evals / first.candidate_id / f"{t}.json").read_text()) for t in ids}
    key = screen_mod.key(rec["bzip2_183B"])
    key2 = screen_mod.key([rec["bzip2_183B"], rec["astar_163B"]])
    entry = {"candidate": first.candidate_id, "generation": 0, "cycles": [s0["cycles"][t] for t in ids]}
    variant = candidate_from_text(seed.replace(body, "    // a comment-only change\n" + body))

    def run(case, index, audit=False, campaign="selftest", screen=True, trace="bzip2_183B"):
        scr = {"trace": trace, "index": index, "audit": audit, "evaluations": str(evals)} if screen else None
        return evaluate(manifest, variant, out_dir=tmp / "screen-cases" / case, baseline_dir=base_dir,
                        campaign_id=campaign, generation=1, screen=scr, **common)

    failures = 0

    def check(name, ok, s):
        nonlocal failures
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<38} {describe(s)[:150]}")

    m = run("match", {key: entry})
    check("screen: a match reuses its scores", screened(m) and m["fitness"] == s0["fitness"]
          and m["speedups"] == s0["speedups"] and "bzip2_183B" in m["traces"] and m["screen"]["matched"] == first.candidate_id
          and (tmp / "screen-cases" / "match" / "bzip2_183B.json").exists(), m)
    n = run("no-match", {"0:0:x:y": entry})
    check("screen: no match runs every trace", not screened(n) and sorted(n["traces"]) == sorted(ids)
          and n["screen"]["matched"] is None and n["fitness"] == s0["fitness"], n)
    a = run("ambiguous", {key: screen_mod.AMBIGUOUS})
    check("screen: an ambiguous key isn't screened", not screened(a) and sorted(a["traces"]) == sorted(ids), a)
    au = run("audit", {key: entry}, audit=True)
    check("screen: an audit runs every trace", not screened(au) and au["screen"]["audited"]
          and au["screen"]["matched"] == first.candidate_id and sorted(au["traces"]) == sorted(ids), au)
    again = run("match", {key: entry})
    other = run("match", {}, campaign="another", screen=False)
    check("screen: reused only by its own campaign", again.get("cached") and not other.get("cached")
          and not screened(other) and sorted(other["traces"]) == sorted(ids), other)
    pair = ["bzip2_183B", "astar_163B"]
    m2 = run("match-two", {key2: entry}, trace=pair)
    check("screen: two traces match on both", screened(m2) and m2["fitness"] == s0["fitness"]
          and set(pair) <= set(m2["traces"]) and m2["screen"]["trace"] == pair
          and key == screen_mod.key([rec["bzip2_183B"]]) and key2.startswith(key + "|"), m2)
    n2 = run("no-match-two", {key: entry}, trace=pair)
    check("screen: one trace's key doesn't match two", not screened(n2) and sorted(n2["traces"]) == sorted(ids), n2)

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    code, _, seconds = proc.run_killable(["sleep", "30"], tmp, 60, cancel=stop)
    ok = code is None and seconds < 5
    failures += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {'screen: a cancel stops a running trace':<38} exit {code} after {seconds:.1f}s")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--candidate", help="candidate header file")
    which.add_argument("--builtin", help="a built-in ChampSim replacement module, evaluated as a candidate")
    which.add_argument("--baseline", action="store_true", help="evaluate the manifest's baseline policy (LRU)")
    which.add_argument("--selftest", action="store_true")
    parser.add_argument("--unscored", action="store_true", help="records only, no fitness (trace calibration)")
    parser.add_argument("--trace", metavar="ID", help="run only this trace of the manifest (one array task)")
    parser.add_argument("--out", help="output directory (default under $AFAR_RESULTS)")
    parser.add_argument("--policy", default="champsim_ipc")
    parser.add_argument("--campaign", default="manual")
    parser.add_argument("--generation", type=int, default=0)
    parser.add_argument("--cxx", default=os.environ.get("AFAR_CXX", "g++"))
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", "1")))
    parser.add_argument("--keep", action="store_true", help="keep the workspace")
    parser.add_argument("--force", action="store_true", help="re-evaluate even if a summary exists")
    args = parser.parse_args()
    try:
        if args.selftest:
            return selftest(args.cxx, args.jobs)
        if not args.manifest or not (args.candidate or args.builtin or args.baseline):
            parser.error("--manifest with one of --candidate, --builtin, --baseline (or --selftest)")
        if args.unscored and args.baseline:
            parser.error("--baseline is already unscored; drop --unscored")
        manifest = Manifest(args.manifest)
        root = results_root()
        baseline_dir = root / "baselines" / manifest.experiment_id
        if args.baseline:
            cand, out = baseline_candidate(manifest), args.out or baseline_dir
        else:
            cand = builtin_candidate(args.builtin) if args.builtin else candidate_from_text(Path(args.candidate).read_text())
            cand_dir = cand.candidate_id.replace(":", "-")
            out = args.out or (root / "calibration" / manifest.experiment_id / cand_dir if args.unscored
                               else root / "evaluations" / manifest.experiment_id / args.policy / cand_dir)
        summary = evaluate(manifest, cand, out_dir=out, baseline_dir=baseline_dir, baseline=args.baseline,
                           unscored=args.unscored, trace=args.trace, policy=args.policy,
                           campaign_id=args.campaign, generation=args.generation,
                           cxx=args.cxx, jobs=args.jobs, keep=args.keep, force=args.force)
        print(describe(summary))
        print(f"  -> {Path(out) / ('summary-' + args.trace + '.json' if args.trace else 'summary.json')}")
        return 0
    except InfraError as e:
        print(f"infra_error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
