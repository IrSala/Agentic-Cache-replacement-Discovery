"""The champsim_ipc policy (M2): build the candidate into ChampSim, run the manifest's traces, score IPC against LRU.

Stages (evaluator/policies/base.py):
  static_check  EVOLVE-BLOCK rules and forbidden calls (evaluator/static_checks.py)
  build         1. compile_only's syntax and hook check: seconds, and it catches a
                   hook ChampSim would silently never call
                2. copy the prebuilt base tree (or, without one, a fresh tree),
                   add the candidate through --replacement-dir, and make
  run           every trace, up to ctx.jobs at a time, under prlimit and a
                process-group timeout, each in its own directory used as HOME.
                With a duplicate screen (ctx.screen, evaluator/screen.py), the
                screening trace starts first; when its result matches a behavior
                in the index, the other traces are stopped or skipped and the
                candidate gets that behavior's scores (unless it's an audit,
                which runs every trace anyway)
  score         the manifest's fitness function, against LRU baselines built
                the same way (and by the same compiler)

One result record per trace (contract §3), written atomically to ctx.out_dir,
with ChampSim's log and JSON in out_dir/raw/. A built-in module (candidate.builtin)
skips the static checks and the syntax check; that's how baselines are built.
"""

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evaluator import champsim_build as cb
from evaluator import fitness as fitness_mod
from evaluator import prebuilt, results, static_checks
from evaluator import screen as screen_mod
from evaluator.policies.base import Build, InfraError, Outcome, Policy
from evaluator.policies.compile_only import (COMPILE_TIMEOUT_S, check_infra, compiler_version, preflight_seed,
                                             syntax_check)

RUN_LIMITS = {"as_bytes": 8 << 30, "fsize_bytes": 1 << 30}   # plus cpu_s = run timeout + 60
SUMMARY_CHARS = 2000


def _tail(text, chars=SUMMARY_CHARS):
    text = (text or "").strip()
    return text if len(text) <= chars else "…" + text[-chars:]


def run_order(traces, baselines):
    """Traces longest first, by their LRU baseline's run time, so ctx.jobs workers pack them as
    LPT scheduling does: a candidate on fewer CPUs than traces still takes about as long as its
    slowest trace (8 CPUs for crc2-evolve-v1's 14; docs/campaigns.md section 8). Without baselines
    (baseline and calibration runs), the manifest's order."""
    if not baselines:
        return list(traces)
    return sorted(traces, key=lambda t: -((baselines.get(t["id"]) or {}).get("run_seconds") or 0.0))


class ChampsimIPC(Policy):
    name = "champsim_ipc"
    needs_baselines = True
    needs_simulation = True
    supports_screen = True

    def time_limit_s(self, manifest, cpus):
        """The syntax check, then the build, then the traces in waves of `cpus`."""
        limits, traces = manifest["limits"], len(manifest["traces"])
        return COMPILE_TIMEOUT_S + limits["build_timeout_s"] + math.ceil(traces / max(1, cpus)) * limits["run_timeout_s"]

    def preflight(self, ctx):
        preflight_seed(ctx)
        missing = ctx.manifest.check_traces()
        if missing:
            raise InfraError("; ".join(missing))
        prebuilt.ensure(ctx.cxx, ctx.jobs)

    # --- stages -------------------------------------------------------------

    def static_check(self, candidate, ctx):
        if candidate.builtin:
            return None
        seed = check_infra(ctx.cxx, ctx.seed)
        return static_checks.static_problem(candidate.code, seed, ctx.manifest["limits"]["max_source_bytes"])

    def build(self, candidate, ctx):
        details, check_seconds = {}, 0.0
        if not candidate.builtin:
            check = syntax_check(candidate.code, ctx.cxx)
            check_seconds = check.seconds
            details["syntax_check_seconds"] = round(check.seconds, 2)
            if check.status != "completed":
                check.details = details
                return check

        ws = ctx.workspace
        base = prebuilt.ensure(ctx.cxx, ctx.jobs)
        if base is not None:
            tree = cb.copy_tree(base, ws / "tree")
            details["tree"] = f"prebuilt {base.parent.name}"
        else:
            tree = cb.prepare_tree(ws / "tree")
            details["tree"] = "full build (no prebuilt tree, and AFAR_ASSETS isn't writable)"

        module, repl_dir = candidate.builtin or "evolved_repl", None
        if not candidate.builtin:
            repl_dir = ws / "repl"
            (repl_dir / "evolved_repl").mkdir(parents=True)
            (repl_dir / "evolved_repl" / "evolved_repl.h").write_text(candidate.code)
        cfg = cb.configure(tree, cb.candidate_config(module), ws / "bin", replacement_dir=repl_dir)
        if not cfg.ok:
            raise InfraError(f"config.sh failed:\n{_tail(cfg.log)}")
        self._config_path = tree / "afar_config.json"

        timeout = ctx.manifest["limits"]["build_timeout_s"]
        made = cb.build(tree, ctx.cxx, ctx.jobs, timeout)
        details.update(build_seconds=round(made.seconds, 2), recompiled=made.compiled)
        if made.exit_code is None:
            status, reason = "timeout", f"the build was still running after {timeout}s and was killed"
        elif not made.ok:
            status, reason = "compile_failed", "the ChampSim build failed (for example a link error)"
        else:
            status, reason = "completed", ""
        return Build(status, reason, check_seconds + made.seconds, made.log, ws / "bin" / "champsim", details)

    def run(self, candidate, build, ctx):
        """Every trace, longest first; the records come back in the manifest's order, so the
        outcome (including which failure is reported first) doesn't depend on the order run.
        With a screen, its trace starts first, and a match stops the rest (see score)."""
        env = results.environment(build.binary, cb.BASE_CONFIG, self._config_path)
        traces = ctx.manifest["traces"]
        ordered = run_order(traces, ctx.baselines)
        self._screen, cancel = None, None
        if ctx.screen:
            first = ctx.screen["trace"]
            ordered = [t for t in ordered if t["id"] == first] + [t for t in ordered if t["id"] != first]
            self._screen = {"trace": first, "matched": None, "matched_generation": None,
                            "audited": bool(ctx.screen.get("audit")), "stopped_seconds": 0.0}
            self._screen_lock = threading.Lock()
            cancel = None if ctx.screen.get("audit") else threading.Event()
        workers = max(1, min(ctx.jobs, len(traces)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = dict(zip((t["id"] for t in ordered),
                            pool.map(lambda t: self._run_trace(candidate, build, ctx, t, env, cancel), ordered)))
            conflict = self._screen_conflict(done)
            if conflict:
                # A trace that ended before it could be stopped disagrees with the matched behavior: the
                # match was wrong. Run what was stopped or skipped, and score this candidate in full.
                self._screen["conflict"] = conflict
                del self._screen["summary"]
                missing = [t for t in ordered if done[t["id"]] is None]
                done.update(zip((t["id"] for t in missing),
                                pool.map(lambda t: self._run_trace(candidate, build, ctx, t, env), missing)))
        return [done[t["id"]] for t in traces if done[t["id"]] is not None]

    def _screen_conflict(self, done):
        """The first trace, other than the screening one, that finished and disagrees with the matched
        behavior's cycles (or failed), when a screen matched and wasn't an audit; else None."""
        screen = self._screen
        if not screen or not screen.get("summary") or screen["audited"]:
            return None
        want = screen["summary"].get("cycles") or {}
        for tid, r in done.items():
            if r is not None and tid != screen["trace"] and (r["status"] != "completed" or r["cycles"] != want.get(tid)):
                return tid
        return None

    def score(self, candidate, build, records, ctx):
        screen = getattr(self, "_screen", None)
        if screen and screen.get("summary") and not screen["audited"]:
            return self._screened(build, records)
        by_trace = {r["trace_id"]: r for r in records}
        failed = [r for r in records if r["status"] != "completed"]
        if any(r["status"] == "infra_error" for r in failed):
            bad = next(r for r in failed if r["status"] == "infra_error")
            raise InfraError(f"trace {bad['trace_id']}: {bad['error_summary']}")
        status = failed[0]["status"] if failed else "completed"
        reason = failed[0]["error_summary"] if failed else ""
        speedups, fitness = {}, None
        if ctx.baselines is not None:
            self._check_toolchain(records, ctx.baselines)
            fitness, extra = fitness_mod.get(ctx.manifest["fitness"]["name"], ctx.manifest["fitness"]["version"])(
                by_trace, ctx.baselines)
            speedups = extra.get("speedups", {})
        metrics = {"combined_score": fitness if fitness is not None else 0.0,
                   "build_seconds": round(build.seconds, 2),
                   "run_seconds": round(sum(r["run_seconds"] or 0 for r in records), 2)}
        metrics.update({f"speedup_{t}": s for t, s in speedups.items()})
        artifacts = {"status": status, "build": build.details}
        if reason:
            artifacts["reason"] = reason
        if screen:
            artifacts["screen"] = self._screen_info()
        return Outcome(status=status, fitness=fitness, reason=reason, metrics=metrics,
                       artifacts=artifacts, records=records)

    def _screened(self, build, records):
        """A match: the matched behavior's scores, with this candidate's own build time. The records
        are the ones that finished (the screening trace's, and any that ended before the match)."""
        matched = self._screen["summary"]
        metrics = dict(matched["metrics"])
        metrics["build_seconds"] = round(build.seconds, 2)
        return Outcome(status="completed", fitness=matched["fitness"], metrics=metrics, records=records,
                       artifacts={"status": "completed", "build": build.details, "screen": self._screen_info()})

    def _screen_info(self):
        info = {k: v for k, v in self._screen.items() if k != "summary"}
        info["stopped_seconds"] = round(info["stopped_seconds"], 2)
        return info

    def _check_screen(self, ctx, record, cancel):
        """After the screening trace: look its result up, and on a match stop the other traces."""
        entry = screen_mod.lookup(ctx.screen["index"], record)
        if not entry:
            return
        try:
            summary = json.loads((Path(ctx.screen["evaluations"]) / entry["candidate"] / "summary.json").read_text())
        except (OSError, ValueError):
            return                      # can't reuse it: evaluate in full
        if summary.get("status") != "completed" or summary.get("screen", {}).get("matched"):
            return
        self._screen.update(matched=entry["candidate"], matched_generation=entry.get("generation"), summary=summary)
        if cancel is not None:
            cancel.set()

    def fail(self, status, reason, candidate, ctx, build=None):
        """An early failure: one record per trace with this status (contract §3)."""
        summary = reason + (f"\n{_tail(build.output)}" if build and build.output.strip() else "")
        env = results.environment(None, cb.BASE_CONFIG, cb.BASE_CONFIG, cxx_version=compiler_version(ctx.cxx))
        now = results.now()
        records = [self._write(ctx, results.make_record(
            manifest=ctx.manifest, candidate_id=candidate.candidate_id, candidate_sha256=candidate.sha256,
            trace_id=t["id"], status=status, env=env, logs={}, started_at=now, finished_at=now,
            campaign_id=ctx.campaign_id, generation=ctx.generation, error_summary=summary,
            build_seconds=round(build.seconds, 2) if build else None)) for t in ctx.manifest["traces"]]
        artifacts = {"status": status, "reason": reason}
        if build:
            artifacts["build"] = build.details
            if build.output.strip():
                artifacts["compiler_output"] = _tail(build.output, 4000)
        return Outcome(status=status, fitness=0.0, reason=reason, records=records, artifacts=artifacts,
                       metrics={"combined_score": 0.0})

    # --- helpers ------------------------------------------------------------

    def _write(self, ctx, record):
        found = results.validate(record)
        if found:
            raise InfraError(f"refusing to write an invalid record: {found}")
        if ctx.out_dir:
            results.write_atomic(Path(ctx.out_dir) / f"{record['trace_id']}.json", record)
        return record

    def _run_trace(self, candidate, build, ctx, trace, env, cancel=None):
        """One trace's record, or None if a screen's match stopped or skipped it."""
        tid = trace["id"]
        if cancel is not None and cancel.is_set():
            return None
        raw = Path(ctx.out_dir or ctx.workspace) / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        log_path, json_path = raw / f"{tid}.log", raw / f"{tid}.champsim.json"
        partial = json_path.with_name(json_path.name + ".partial")
        partial.unlink(missing_ok=True)
        common = dict(manifest=ctx.manifest, candidate_id=candidate.candidate_id, candidate_sha256=candidate.sha256,
                      trace_id=tid, env=env, campaign_id=ctx.campaign_id, generation=ctx.generation,
                      build_seconds=round(build.seconds, 2),
                      logs={"run_log": str(log_path), "champsim_json": str(json_path)})
        started = results.now()

        trace_path = ctx.manifest.trace_path(trace)
        if not trace_path.is_file():
            return self._write(ctx, results.make_record(status="infra_error", error_summary=f"trace not found: {trace_path}",
                                                        started_at=started, finished_at=results.now(), **common))
        run_dir = Path(ctx.workspace) / f"run-{tid}"
        run_dir.mkdir(parents=True, exist_ok=True)
        timeout = ctx.manifest["limits"]["run_timeout_s"]
        sim = ctx.manifest["simulation"]
        r = cb.run(build.binary, trace_path, sim["warmup_instructions"], sim["simulation_instructions"],
                   partial, log_path, timeout, run_dir, limits=dict(RUN_LIMITS, cpu_s=timeout + 60),
                   env=dict(os.environ, HOME=str(run_dir)), cancel=cancel)
        finished, seconds = results.now(), round(r.seconds, 2)
        if r.exit_code is None and cancel is not None and cancel.is_set():
            partial.unlink(missing_ok=True)
            log_path.unlink(missing_ok=True)
            with self._screen_lock:
                self._screen["stopped_seconds"] += r.seconds
            return None

        if r.exit_code is None:
            status, summary = "timeout", f"the run exceeded run_timeout_s={timeout} and its process group was killed"
        elif r.exit_code != 0:
            how = f"was killed by signal {-r.exit_code}" if r.exit_code < 0 else f"exited {r.exit_code}"
            status, summary = "runtime_failed", f"champsim {how}:\n{_tail(r.log)}"
        elif not partial.exists():
            status, summary = "infra_error", "champsim exited 0 but wrote no --json output"
        else:
            os.replace(partial, json_path)
            try:
                metrics = results.champsim_metrics(json_path)
            except (KeyError, IndexError, ValueError, ZeroDivisionError) as e:
                status, summary = "infra_error", f"unparseable ChampSim JSON after exit 0: {e!r}"
            else:
                record = self._write(ctx, results.make_record(status="completed", metrics=metrics, exit_code=0,
                                                              run_seconds=seconds, started_at=started,
                                                              finished_at=finished, **common))
                if self._screen is not None and tid == self._screen["trace"]:
                    self._check_screen(ctx, record, cancel)
                return record
        return self._write(ctx, results.make_record(status=status, error_summary=summary, exit_code=r.exit_code,
                                                    run_seconds=seconds, started_at=started, finished_at=finished,
                                                    **common))

    @staticmethod
    def _check_toolchain(records, baselines):
        """Scores compare like with like: same ChampSim commit and compiler as the baseline."""
        for r in records:
            b = baselines.get(r["trace_id"])
            if b and r["status"] == "completed":
                for key in ("champsim_commit", "cxx_version"):
                    if r["env"][key] != b["env"][key]:
                        raise InfraError(f"baseline for {r['trace_id']} has {key} {b['env'][key]!r}, the candidate "
                                         f"{r['env'][key]!r}; rebuild the baselines with evaluate_candidate --baseline")
