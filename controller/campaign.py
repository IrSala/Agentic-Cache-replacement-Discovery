"""Staged campaigns (M5): a GPU job generates a generation of candidates, a CPU
array evaluates them, and a small job aggregates them into OpenEvolve's database
and starts the next generation (docs/campaigns.md).

    python -m controller.campaign start SPEC [--name ID] [--executor slurm|local] [--exclude NODES]
    python -m controller.campaign status CAMPAIGN
    python -m controller.campaign stop CAMPAIGN       halt after the current stage (writes STOP)
    python -m controller.campaign resume CAMPAIGN     resubmit the current stage; stages are idempotent
    python -m controller.campaign generate|evaluate-task|aggregate --campaign C --generation G ...
                                                      (what the stage jobs run)
    python -m controller.campaign --selftest

SPEC is a run spec with a `campaign:` section (controller/runspec.py). CAMPAIGN
is a directory, or an id under $AFAR_RESULTS/campaigns/. Everything a campaign
knows is in its directory, written atomically, so any stage can be rerun:
    campaign.json       the run record: parts, input sha256s, commits, executor
    status.json         state (running | finished | halted), generation, stage, jobs, attempt
    events.jsonl        one line per transition, submission, retry and halt
    resolved/           the OpenEvolve config and prompt snapshot
    checkpoints/        standard OpenEvolve checkpoints, one per generation
    generations/<g>/    candidates/<id>.h, generation.json (parent, island, prompt, reply
                        or rejection per iteration), tasks.json (array index -> candidate),
                        results/<id>.json, attempts.json, summary.json
    faults.json         optional, for tests only: injected failures (see FAULTS below)
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from controller import runspec  # noqa: E402
from controller.executor import LocalExecutor, SlurmExecutor  # noqa: E402
from evaluator import evaluate_candidate as ec  # noqa: E402
from evaluator import results  # noqa: E402
from evaluator.manifest import Manifest  # noqa: E402
from evaluator.policies import InfraError, policy_class  # noqa: E402

EXECUTORS = ("slurm", "local")
LOCAL_EXECUTORS = {}             # campaign directory -> its LocalExecutor, shared by every stage in this process
GENERATE_RESOURCES = {"partition": "gpuqs", "gres": "gpu:a100:1", "cpus-per-task": 4, "mem": "24G", "time": "00:30:00"}
AGGREGATE_RESOURCES = {"partition": "cpuqs", "cpus-per-task": 1, "mem": "4G", "time": "00:20:00"}
EVALUATE_MARGIN_S = 600          # above the policy's own worst case: queue-free start-up, copying, writing
# FAULTS (tests only): faults.json = {"infra": [{"generation": g, "index": i, "attempts": [1] or "all"}],
#                                     "aggregate_crash": [g, ...], "aggregate_crash_after_save": [g, ...]}


class CampaignError(RuntimeError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def campaigns_root():
    value = os.environ.get("AFAR_RESULTS")
    if not value:
        raise CampaignError("AFAR_RESULTS is not set; source env/site.sh first")
    return Path(value) / "campaigns"


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def hms(seconds):
    seconds = int(math.ceil(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Campaign:
    def __init__(self, ref):
        path = Path(ref)
        self.path = path if (path.is_absolute() or "/" in str(ref)) else campaigns_root() / str(ref)
        if not (self.path / "campaign.json").exists():
            raise CampaignError(f"no campaign at {self.path}")
        self.meta = read_json(self.path / "campaign.json")
        self.id = self.meta["campaign_id"]
        self.params = self.meta["campaign"]
        self._executor = None

    # --- state ------------------------------------------------------------
    @property
    def status(self):
        return read_json(self.path / "status.json")

    def set_status(self, **changes):
        status = dict(self.status or {})
        status.update(changes, updated_at=now())
        results.write_atomic(self.path / "status.json", status)
        return status

    def event(self, kind, **fields):
        line = {"at": now(), "event": kind, "job": os.environ.get("SLURM_JOB_ID"), **fields}
        with open(self.path / "events.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")

    def halt(self, reason, message):
        self.set_status(state="halted", reason=reason, message=message)
        self.event("halted", reason=reason, message=message)
        print(f"campaign {self.id} halted ({reason}): {message}")

    def gen_dir(self, g):
        return self.path / "generations" / str(g)

    def checkpoint(self, g):
        return self.path / "checkpoints" / f"checkpoint_{g * self.params['candidates_per_generation']}"

    def stopped(self):
        return (self.path / "STOP").exists()

    def faults(self):
        return read_json(self.path / "faults.json", {})

    # --- executor and resources ------------------------------------------
    def executor(self):
        if self._executor is None:
            if self.meta["executor"] == "slurm":
                self._executor = SlurmExecutor(self.path, self.params["max_concurrent_tasks"])
            else:
                self._executor = LOCAL_EXECUTORS.setdefault(str(self.path), LocalExecutor(run_stage))
        return self._executor

    def manifest(self):
        return Manifest(ROOT / self.meta["evaluator"]["manifest"])

    def evaluate_resources(self):
        cpus = self.meta["evaluator"]["cpus_per_candidate"]
        limit = policy_class(self.meta["evaluator"]["policy"])().time_limit_s(self.manifest(), cpus)
        return {"partition": "cpuqs", "cpus-per-task": cpus, "mem": f"{max(2, cpus)}G",
                "time": hms(limit + EVALUATE_MARGIN_S)}

    def submit(self, stage, g, **kwargs):
        resources = {"generate": GENERATE_RESOURCES, "aggregate": AGGREGATE_RESOURCES}.get(stage) \
            or self.evaluate_resources()
        exclude = set(kwargs.pop("exclude", ()))
        if stage == "generate":
            exclude |= set(self.meta.get("exclude", []))
        job = self.executor().submit(stage, str(self.path), g, resources=resources, exclude=sorted(exclude), **kwargs)
        self.event("submitted", stage=stage, generation=g, job_id=job, exclude=sorted(exclude),
                   indices=kwargs.get("indices"), after=kwargs.get("after"))
        return job

    def submit_evaluation(self, g, indices, attempt, exclude=()):
        """Evaluate tasks `indices` of generation g, then aggregate it."""
        jobs = []
        after = None
        if indices:
            after = self.submit("evaluate", g, indices=indices, exclude=exclude)
            jobs.append(after)
        jobs.append(self.submit("aggregate", g, after=after))
        self.set_status(state="running", generation=g, stage="evaluating", jobs=jobs, attempt=attempt)


# --- stages -----------------------------------------------------------------

def start(spec_ref, name=None, executor="slurm", exclude=()):
    if executor not in EXECUTORS:
        raise CampaignError(f"executor must be one of {', '.join(EXECUTORS)}")
    spec_file = runspec.spec_path(spec_ref)
    cid = name or f"{spec_file.stem}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    path = campaigns_root() / cid
    if path.exists():
        raise CampaignError(f"{path} already exists; pick another --name or resume it")
    campaigns_root().mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{cid}-", dir=campaigns_root()))
    try:
        r = runspec.resolve(spec_ref, cpus=runspec.DEFAULT_JOB_CPUS, template_dir=path / "resolved" / "prompt")
        if not r["campaign"]:
            raise CampaignError(f"{runspec.rel(spec_file)} has no campaign: section")
        runspec.write(r, staging / "resolved")
        record = runspec.run_record(r, path, None)
        record.update(campaign_id=cid, executor=executor, exclude=sorted(exclude), created_at=now(),
                      seed_suffix=r["seed"].suffix)
        results.write_atomic(staging / "campaign.json", record)
        # Generation 0 is the seed alone, evaluated like any candidate.
        code = r["seed"].read_text()
        cand = hashlib.sha256(code.encode()).hexdigest()[:16]
        g0 = staging / "generations" / "0"
        (g0 / "candidates").mkdir(parents=True)
        (g0 / "candidates" / f"{cand}.h").write_text(code)
        results.write_atomic(g0 / "tasks.json", [cand])
        results.write_atomic(g0 / "generation.json", {"generation": 0, "entries": [
            {"index": 0, "iteration": 0, "candidate_id": cand, "seed": True, "island": 0}]})
        os.replace(staging, path)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    c = Campaign(path)
    c.set_status(state="running", generation=0, stage="evaluating", jobs=[], attempt=1, reason=None, message=None)
    c.event("started", spec=runspec.rel(spec_file), executor=executor, campaign=c.params)
    c.submit_evaluation(0, [0], attempt=1)
    print(f"campaign {cid}: {path}")
    return c


def generate(c, g, api_base=None, needs_llm=False):
    """Write generation g's candidates (on a GPU node, with the server up), then submit their evaluation."""
    gd = c.gen_dir(g)
    done = (gd / "generation.json").exists()
    st = c.status
    current = st["state"] == "running" and st["stage"] == "generating" and st["generation"] == g
    if needs_llm:                                  # does the job need to start the model server?
        return 0 if current and not done and not c.stopped() else 1
    if not current:
        print(f"generation {g} is not the current stage ({st['state']}, {st['stage']} {st['generation']}); nothing to do")
        return 0
    if c.stopped():
        c.halt("stopped", f"STOP file found before generating generation {g}")
        return 0
    if not done:
        if not api_base:
            raise CampaignError("generate needs --api-base (the model server's address)")
        from controller import oe_adapter as oe
        config = oe.load_config(c.path / "resolved" / "config.yaml", api_base)
        db = oe.load_database(config, c.checkpoint(g - 1), g)
        n = c.params["candidates_per_generation"]
        first = (g - 1) * n + 1
        sampler = oe.new_sampler(config)
        picks = []
        for i in range(n):
            island = i % oe.num_islands(db)
            parent, inspirations = oe.sample(db, config, island)
            prompt, parent_island = oe.build_prompt(sampler, db, config, parent, inspirations, first + i)
            picks.append((island, parent, inspirations, prompt, parent_island))
        started = time.monotonic()
        replies = oe.generate(config, [p[3] for p in picks])
        llm_seconds = round(time.monotonic() - started, 2)
        (gd / "candidates").mkdir(parents=True, exist_ok=True)
        entries, tasks = [], []
        for i, ((island, parent, inspirations, prompt, parent_island), reply) in enumerate(zip(picks, replies)):
            entry = {"index": i, "iteration": first + i, "island": island, "parent_island": parent_island,
                     "parent_id": parent.id, "parent_generation": parent.generation, "parent_metrics": parent.metrics,
                     "inspirations": [p.id for p in inspirations], "prompt": prompt, "response": reply["response"],
                     "token_usage": reply["token_usage"], "llm_seconds": reply["seconds"]}
            code = oe.parse_rewrite(reply["response"], config.language)
            reason = reply["error"] or (None if code else "no code found in the reply")
            if not reason and code == parent.code:
                reason = "identical to the parent program"
            if not reason and len(code) > config.max_code_length:
                reason = f"longer than max_code_length ({len(code)} > {config.max_code_length})"
            if reason:
                entry["rejected"] = reason
            else:
                cand = hashlib.sha256(code.encode()).hexdigest()[:16]
                (gd / "candidates" / f"{cand}.h").write_text(code)
                entry["candidate_id"] = cand
                if cand not in tasks:              # duplicates share one evaluation
                    tasks.append(cand)
            entries.append(entry)
        results.write_atomic(gd / "tasks.json", tasks)
        results.write_atomic(gd / "generation.json", {"generation": g, "llm_seconds": llm_seconds,
                                                      "host": socket.gethostname(), "entries": entries})
        rejected = sum("rejected" in e for e in entries)
        c.event("generated", generation=g, candidates=len(entries), unique=len(tasks), rejected=rejected,
                llm_seconds=llm_seconds)
        print(f"generation {g}: {len(entries)} replies in {llm_seconds}s, {len(tasks)} unique candidates, "
              f"{rejected} rejected")
    tasks = read_json(gd / "tasks.json")
    c.submit_evaluation(g, list(range(len(tasks))), attempt=1)
    return 0


def evaluate_task(c, g, index):
    """One candidate of generation g (one array task); 0 for any candidate outcome, 1 for an infra failure."""
    gd = c.gen_dir(g)
    cand = read_json(gd / "tasks.json")[index]
    result_path = gd / "results" / f"{cand}.json"
    if result_path.exists():
        print(f"{cand}: already evaluated")
        return 0
    attempt = read_json(gd / "attempts.json", {}).get(cand, 1)
    # Slurm's own name for this node (c11, not c11.hpc.coe), so a retry can --exclude it.
    node = os.environ.get("SLURMD_NODENAME") or socket.gethostname().split(".")[0]
    job = os.environ.get("SLURM_JOB_ID")
    infra_path = gd / "results" / f"{cand}.infra-{attempt}.json"
    (gd / "results").mkdir(exist_ok=True)

    def infra(message):
        results.write_atomic(infra_path, {"candidate_id": cand, "index": index, "attempt": attempt, "node": node,
                                          "job": job, "error": message, "at": now()})
        print(f"infra_error: {cand} (task {index}, attempt {attempt}, {node}): {message}", file=sys.stderr)
        return 1

    for f in c.faults().get("infra", []):
        if f.get("generation") == g and f.get("index") == index and (f.get("attempts") == "all"
                                                                     or attempt in f.get("attempts", [])):
            return infra(f"injected by faults.json (attempt {attempt})")
    ev = c.meta["evaluator"]
    manifest = c.manifest()
    root = ec.results_root()
    candidate = ec.candidate_from_text((gd / "candidates" / f"{cand}.h").read_text())
    try:
        summary = ec.evaluate(
            manifest, candidate, out_dir=root / "evaluations" / manifest.experiment_id / ev["policy"] / cand,
            baseline_dir=root / "baselines" / manifest.experiment_id, policy=ev["policy"], campaign_id=c.id,
            generation=g, cxx=os.environ.get("AFAR_CXX", "g++"), jobs=ev["cpus_per_candidate"],
            seed=ROOT / c.meta["seed"]["path"])
    except InfraError as e:
        return infra(str(e))
    if summary["status"] == "infra_error":
        return infra(summary.get("reason") or "infra_error")
    results.write_atomic(result_path, {"candidate_id": cand, "index": index, "attempt": attempt, "node": node,
                                       "job": job, "status": summary["status"], "fitness": summary["fitness"],
                                       "summary": summary, "at": now()})
    print(ec.describe(summary))
    return 0


def aggregate(c, g):
    """Retry missing evaluations, or add generation g to the database, then start g+1 (or finish, or halt)."""
    st = c.status
    if st["state"] != "running" or st["generation"] != g or st["stage"] not in ("evaluating", "aggregating"):
        print(f"generation {g} is not being evaluated ({st['state']}, {st['stage']} {st['generation']}); nothing to do")
        return 0
    c.set_status(stage="aggregating")
    gd = c.gen_dir(g)
    tasks = read_json(gd / "tasks.json")
    missing = [cand for cand in tasks if not (gd / "results" / f"{cand}.json").exists()]
    if missing:
        attempt = st.get("attempt", 1)
        failures = [read_json(f) for cand in missing for f in sorted((gd / "results").glob(f"{cand}.infra-*.json"))]
        if attempt > c.params["max_task_retries"]:
            last = {f["candidate_id"]: f for f in failures}
            detail = "; ".join(f"task {tasks.index(cand)} ({cand}): "
                               + (f"{last[cand]['error']} on {last[cand]['node']}" if cand in last
                                  else "no result and no error record (killed or lost)")
                               for cand in missing)
            c.halt("infra", f"{len(missing)} of {len(tasks)} evaluation(s) of generation {g} failed {attempt} "
                            f"time(s): {detail}. Logs: {c.path / 'logs'}")
            return 1
        attempts = read_json(gd / "attempts.json", {})
        for cand in missing:
            attempts[cand] = attempt + 1
        results.write_atomic(gd / "attempts.json", attempts)
        exclude = sorted({f["node"] for f in failures if f.get("node")})
        c.event("retry", generation=g, attempt=attempt + 1, tasks=[tasks.index(x) for x in missing], exclude=exclude)
        c.submit_evaluation(g, [tasks.index(x) for x in missing], attempt=attempt + 1, exclude=exclude)
        return 0

    if not c.checkpoint(g).exists():
        from controller import oe_adapter as oe
        from evaluator.openevolve_adapter import summary_to_result
        config = oe.load_config(c.path / "resolved" / "config.yaml")
        db = oe.new_database(config) if g == 0 else oe.load_database(config, c.checkpoint(g - 1), g)
        entries = read_json(gd / "generation.json")["entries"]
        added = 0
        for e in sorted(entries, key=lambda e: e["iteration"]):
            if "candidate_id" not in e:
                continue
            result = read_json(gd / "results" / f"{e['candidate_id']}.json")
            metrics, artifacts = summary_to_result(result["summary"])
            added += oe.add_program(
                db, config, code=(gd / "candidates" / f"{e['candidate_id']}.h").read_text(),
                parent_id=e.get("parent_id"), parent_generation=e.get("parent_generation", 0),
                parent_metrics=e.get("parent_metrics", {}), parent_island=e.get("parent_island", 0),
                target_island=e["island"], iteration=e["iteration"], metrics=metrics, artifacts=artifacts,
                prompt=e.get("prompt"), response=e.get("response"), token_usage=e.get("token_usage"),
                seed=bool(e.get("seed"))) is not None
        if g in c.faults().get("aggregate_crash", []):
            raise CampaignError("injected by faults.json: aggregate crashed before saving")
        oe.save_checkpoint(db, c.path / "checkpoints", g * c.params["candidates_per_generation"],
                           c.meta["seed_suffix"])
        if g in c.faults().get("aggregate_crash_after_save", []):
            raise CampaignError("injected by faults.json: aggregate crashed after saving")
        c.event("aggregated", generation=g, programs_added=added)
    summary = summarize(c, g)
    results.write_atomic(gd / "summary.json", summary)

    if c.stopped():
        c.halt("stopped", f"STOP file found after generation {g}")
    elif g >= c.params["generations"]:
        c.set_status(state="finished", stage="done", jobs=[], reason=None,
                     message=f"{g} generation(s); best combined_score {summary['best_overall']}")
        c.event("finished", generation=g, best=summary["best_overall"])
        print(f"campaign {c.id} finished after generation {g}; best combined_score {summary['best_overall']}")
    else:
        job = c.submit("generate", g + 1)
        c.set_status(state="running", generation=g + 1, stage="generating", jobs=[job], attempt=1)
    return 0


def summarize(c, g):
    gd = c.gen_dir(g)
    entries = read_json(gd / "generation.json")["entries"]
    statuses, fitness = {}, []
    for e in entries:
        if "candidate_id" in e:
            r = read_json(gd / "results" / f"{e['candidate_id']}.json")
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
            fitness.append(r["fitness"] or 0.0)
        else:
            key = "rejected: " + e["rejected"].split(" (")[0]
            statuses[key] = statuses.get(key, 0) + 1
    best = read_json(c.checkpoint(g) / "best_program_info.json", {})
    return {"generation": g, "iterations": [entries[0]["iteration"], entries[-1]["iteration"]],
            "statuses": statuses, "best_of_generation": max(fitness, default=None),
            "best_overall": best.get("metrics", {}).get("combined_score"), "best_id": best.get("id"),
            "unique_candidates": len(read_json(gd / "tasks.json")), "at": now()}


def run_stage(stage, campaign, g, index):
    """How LocalExecutor runs a stage; Slurm jobs run the same functions through the CLI."""
    c = Campaign(campaign)
    if stage == "generate":
        return generate(c, g, api_base=os.environ.get("AFAR_LLM_API_BASE"))
    if stage == "evaluate":
        return evaluate_task(c, g, index)
    return aggregate(c, g)


# --- operator commands ------------------------------------------------------

def show_status(c):
    st = c.status
    print(f"campaign {c.id} ({c.meta['spec']['file']}, executor {c.meta['executor']}): {st['state']}"
          + (f" ({st['reason']}): {st['message']}" if st.get("reason") else (f": {st['message']}" if st.get("message") else "")))
    print(f"  now: generation {st['generation']} of {c.params['generations']}, {st['stage']}, attempt "
          f"{st.get('attempt', 1)}, jobs {st.get('jobs')}; updated {st['updated_at']}")
    for gd in sorted(c.path.glob("generations/*"), key=lambda p: int(p.name)):
        s = read_json(gd / "summary.json")
        if s:
            print(f"  generation {s['generation']}: {s['statuses']}; best of generation {s['best_of_generation']}, "
                  f"best so far {s['best_overall']}")
    if c.stopped():
        print("  STOP file present: the campaign halts after the current stage")
    return 0


def stop(c):
    (c.path / "STOP").write_text(f"requested {now()}\n")
    c.event("stop_requested")
    print(f"campaign {c.id}: STOP written; it halts after the current stage")
    return 0


def resume(c):
    st = c.status
    if st["state"] == "finished":
        print(f"campaign {c.id} already finished")
        return 0
    live = c.executor().alive(st.get("jobs") or [])
    if live:
        raise CampaignError(f"jobs {live} of the current stage are still queued or running; wait, or scancel them first")
    if (c.path / "STOP").exists():
        (c.path / "STOP").unlink()
        c.event("stop_cleared")
    g, stage = st["generation"], st["stage"]
    c.set_status(state="running", reason=None, message=None)
    c.event("resumed", generation=g, stage=stage, previous=st["state"])
    if stage == "generating":
        job = c.submit("generate", g)
        c.set_status(jobs=[job], attempt=1)
    else:
        gd = c.gen_dir(g)
        tasks = read_json(gd / "tasks.json")
        missing = [i for i, cand in enumerate(tasks) if not (gd / "results" / f"{cand}.json").exists()]
        attempt = 1 if st["state"] == "halted" else st.get("attempt", 1)
        if missing and st["state"] == "halted":
            results.write_atomic(gd / "attempts.json", {**read_json(gd / "attempts.json", {}),
                                                        **{tasks[i]: 1 for i in missing}})
        c.submit_evaluation(g, missing, attempt=attempt)
    print(f"campaign {c.id}: resumed at generation {g}, {stage}")
    return 0


def drain_local(c):
    """For --executor local: run every queued stage, in order, until the queue is empty."""
    ex = c.executor()
    if isinstance(ex, LocalExecutor):
        ex.drain()
        return ex.failed
    return []


# --- selftest ---------------------------------------------------------------

def selftest(keep=False):
    """Gate A on this host: a local campaign through every stage, with the fake LLM server and injected faults."""
    from controller import oe_adapter as oe
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<52} {detail}")

    saved = {k: os.environ.get(k) for k in ("AFAR_RESULTS", "AFAR_LLM_API_BASE", "AFAR_LLM_API_KEY")}
    tmp = Path(tempfile.mkdtemp(prefix="afar-campaign-selftest-"))
    server = None
    try:
        os.environ["AFAR_RESULTS"] = str(tmp / "results")
        # Baselines for the tiny manifest, built here (seconds).
        manifest = Manifest(ROOT / "configs" / "experiments" / "crc2-tiny-v1.json")
        base = ec.evaluate(manifest, ec.baseline_candidate(manifest), out_dir=tmp / "results" / "baselines" / manifest.experiment_id,
                           baseline=True, cxx=os.environ.get("AFAR_CXX", "g++"), jobs=4)
        check("tiny baselines", base["status"] == "completed", f"{len(base['traces'])} traces")

        # The fake server answers in turn: the seed, an MRU variant, a compile error, a forbidden call.
        seed = (ROOT / "seeds" / "evolved_repl.h").read_text()
        replies = {"1-seed": seed, "2-mru": seed.replace("std::min_element(begin, end)", "std::max_element(begin, end)"),
                   "3-syntax": seed.replace("uint64_t cycle = 0;", "uint64_t cycle = 0"),
                   "4-forbidden": seed.replace("    // Find the way whose last use cycle is most distant\n",
                                               '    std::system("true");\n    // Find the way whose last use cycle is most distant\n')}
        (tmp / "replies").mkdir()
        for name, code in replies.items():
            (tmp / "replies" / name).write_text(f"```cpp\n{code}```\n")
        port = 39000 + os.getpid() % 1000
        os.environ.update(AFAR_LLM_API_KEY="selftest-key", AFAR_LLM_API_BASE=f"http://127.0.0.1:{port}/v1")
        env = dict(os.environ, VLLM_API_KEY="selftest-key")
        server = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fake_llm_server.py"), "--port", str(port),
                                   "--served-model-name", "afar-llm", "--reply-dir", str(tmp / "replies")],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)

        spec = runspec.load_yaml(runspec.spec_path("m5-dev-v1"), "spec")
        spec["evaluator"]["experiment"] = "crc2-tiny-v1"
        spec["model"] = "fake-v1"
        spec["campaign"].update(generations=3, candidates_per_generation=6)
        spec["description"] = "campaign selftest: m5-dev-v1 on crc2-tiny-v1 with the fake LLM"
        spec_path = tmp / "campaign-selftest.yaml"
        import yaml
        spec_path.write_text(yaml.safe_dump(spec))

        # (a) three generations, unattended, with (b) one task failing once and (d) aggregate crashing once.
        c = start(spec_path, name="selftest-a", executor="local")
        (c.path / "faults.json").write_text(json.dumps({"infra": [{"generation": 1, "index": 1, "attempts": [1]}],
                                                        "aggregate_crash": [2]}))
        failed = drain_local(c)
        st = c.status
        check("(d) aggregate crash leaves the campaign resumable", st["stage"] == "aggregating" and st["generation"] == 2
              and any(f[1] == "aggregate" for f in failed), f"{st['state']} {st['stage']} {st['generation']}")
        (c.path / "faults.json").write_text(json.dumps({}))
        resume(c)
        drain_local(c)
        st = c.status
        check("(a) three generations finish", st["state"] == "finished", st.get("message", ""))
        events = [json.loads(l) for l in (c.path / "events.jsonl").read_text().splitlines()]
        retries = [e for e in events if e["event"] == "retry"]
        check("(b) an injected failure is retried, then succeeds", len(retries) == 1 and retries[0]["generation"] == 1
              and any((c.gen_dir(1) / "results").glob("*.infra-1.json")) and st["state"] == "finished",
              f"{len(retries)} retry")
        ckpts = sorted(int(p.name.split("_")[1]) for p in (c.path / "checkpoints").glob("checkpoint_*"))
        check("(a) checkpoints for every generation", ckpts == [0, 6, 12, 18], str(ckpts))
        config = oe.load_config(c.path / "resolved" / "config.yaml")
        db = oe.load_database(config, c.checkpoint(3), 3)
        iterations = sorted(p.iteration_found for p in db.programs.values())
        ids_by_code = {}
        for p in db.programs.values():
            ids_by_code.setdefault((p.code, p.iteration_found), []).append(p.id)
        check("(d) no program added twice after the resume", all(len(v) == 1 for v in ids_by_code.values()),
              f"{len(db.programs)} programs, iterations {iterations[:3]}..{iterations[-1:]}")
        gen1 = read_json(c.gen_dir(1) / "generation.json")["entries"]
        uniq = read_json(c.gen_dir(1) / "tasks.json")
        cands = [e["candidate_id"] for e in gen1 if "candidate_id" in e]
        check("(f) duplicates are evaluated once", len(uniq) == len(set(cands)) < len(cands),
              f"{len(cands)} candidates, {len(uniq)} evaluations")
        s = read_json(c.gen_dir(3) / "summary.json")
        check("statuses vary across candidates", len(s["statuses"]) >= 2, str(s["statuses"]))

        # (c) a persistent failure halts with a clear status.
        c2 = start(spec_path, name="selftest-c", executor="local")
        (c2.path / "faults.json").write_text(json.dumps({"infra": [{"generation": 0, "index": 0, "attempts": "all"}]}))
        drain_local(c2)
        st = c2.status
        check("(c) a persistent infra failure halts", st["state"] == "halted" and st["reason"] == "infra"
              and "task 0" in st["message"] and "injected" in st["message"], st.get("message", "")[:90])

        # (e) a STOP file halts after the current stage; resume clears it.
        c3 = start(spec_path, name="selftest-e", executor="local")
        stop(c3)
        drain_local(c3)
        st = c3.status
        check("(e) STOP halts after the current stage", st["state"] == "halted" and st["reason"] == "stopped"
              and c3.checkpoint(0).exists() and not c3.gen_dir(1).exists(), st.get("message", ""))
        resume(c3)
        drain_local(c3)
        check("(e) resume after STOP continues to the end", c3.status["state"] == "finished")

        # The last checkpoint is a standard OpenEvolve checkpoint: openevolve-run continues from it.
        out = subprocess.run([str(ROOT / ".venv" / "controller" / "bin" / "openevolve-run"), str(ROOT / "seeds" / "evolved_repl.h"),
                              str(ROOT / "evaluator" / "compile_check.py"), "--config", str(c.path / "resolved" / "config.yaml"),
                              "--checkpoint", str(c.checkpoint(3)), "--iterations", "1", "--output", str(tmp / "oe-continue"),
                              "--api-base", os.environ["AFAR_LLM_API_BASE"]],
                             capture_output=True, text=True, timeout=600, env=dict(os.environ, AFAR_CXX=os.environ.get("AFAR_CXX", "g++")))
        check("openevolve-run continues from a campaign checkpoint", out.returncode == 0
              and "Loaded database with" in out.stdout + out.stderr, f"exit {out.returncode}")
    finally:
        if server:
            server.terminate()
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        if keep:
            print(f"  kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print("campaign selftest passed" if failures == 0 else f"campaign selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--keep", action="store_true", help="with --selftest: keep the temporary campaigns")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("start")
    p.add_argument("spec")
    p.add_argument("--name")
    p.add_argument("--executor", default="slurm", choices=EXECUTORS)
    p.add_argument("--exclude", default="", help="nodes to keep generation jobs off, e.g. cs002")
    for name in ("status", "stop", "resume"):
        sub.add_parser(name).add_argument("campaign")
    for name in ("generate", "evaluate-task", "aggregate"):
        p = sub.add_parser(name)
        p.add_argument("--campaign", default=os.environ.get("AFAR_CAMPAIGN"))
        p.add_argument("--generation", type=int, default=os.environ.get("AFAR_GENERATION"))
        if name == "generate":
            p.add_argument("--api-base")
            p.add_argument("--needs-llm", action="store_true", help="exit 0 if this generation still needs the model")
            p.add_argument("--model-profile", action="store_true", help="print the campaign's model profile path")
        if name == "evaluate-task":
            p.add_argument("--index", type=int, default=os.environ.get("SLURM_ARRAY_TASK_ID"))
    args = parser.parse_args()
    try:
        if args.selftest:
            return selftest(args.keep)
        if args.command == "start":
            c = start(args.spec, args.name, args.executor, [n for n in args.exclude.split(",") if n])
            return 1 if drain_local(c) and c.status["state"] != "finished" else 0
        if args.command is None:
            parser.error("a command, or --selftest")
        if args.command in ("status", "stop", "resume"):
            c = Campaign(args.campaign)
            code = {"status": show_status, "stop": stop, "resume": resume}[args.command](c)
            drain_local(c)
            return code
        c = Campaign(args.campaign)
        g = int(args.generation)
        if args.command == "generate":
            if args.model_profile:
                print(ROOT / c.meta["model"]["file"])
                return 0
            return generate(c, g, args.api_base, args.needs_llm)
        if args.command == "evaluate-task":
            return evaluate_task(c, g, int(args.index))
        return aggregate(c, g)
    except (CampaignError, runspec.SpecError) as e:
        print(f"campaign: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
