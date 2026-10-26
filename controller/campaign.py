"""Staged campaigns (M5, M6): a GPU job generates a generation of candidates, a CPU
array evaluates them, and a small job aggregates them into OpenEvolve's database
and starts the next generation (docs/campaigns.md).

    python -m controller.campaign start SPEC [--name ID] [--executor slurm|local] [--exclude NODES]
    python -m controller.campaign status CAMPAIGN
    python -m controller.campaign stop CAMPAIGN [--now]  halt after the current stage (writes STOP);
                                                         --now also cancels its jobs
    python -m controller.campaign resume CAMPAIGN     resubmit the current stage; stages are idempotent
    python -m controller.campaign generate|evaluate-task|aggregate --campaign C --generation G ...
                                                      (what the stage jobs run)
    python -m controller.campaign handle|heartbeat --campaign C ...
                                                      (the watchdog jobs, M6)
    python -m controller.campaign --selftest

The watchdog (M6). Every generation and aggregation job has a handler job that runs
only if it fails (afternotok), and restarts it a bounded number of times, or halts.
A heartbeat job per campaign, at most hourly, restarts a chain left with no live
job. Only the job that status.json names acts on a stage (stage_job, handler_job;
heartbeat.json's "next" for heartbeats), so a duplicate or orphaned job does nothing.
A halt mails AFAR_MAIL_USER, unless it was asked for (stopped). Only the heartbeat and
handlers write when no stage job is running, since status.json and events.jsonl are
rewritten and appended without locks (NFS).

SPEC is a run spec with a `campaign:` section (controller/runspec.py). CAMPAIGN
is a directory, or an id under $AFAR_RESULTS/campaigns/. Everything a campaign
knows is in its directory, written atomically, so any stage can be rerun:
    campaign.json       the run record: parts, input sha256s, commits, executor
    status.json         state (running | finished | halted), generation, stage, jobs, attempt,
                        stage_job, handler_job, stage_attempt
    events.jsonl        one line per transition, submission, retry, failure, halt and mail
    heartbeat.json      the heartbeat's last run and its successor's job
    budget.json         CPU- and GPU-hours used so far, from sacct
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
from controller.executor import ExecutorError, LocalExecutor, SlurmExecutor  # noqa: E402
from evaluator import evaluate_candidate as ec  # noqa: E402
from evaluator import results  # noqa: E402
from evaluator.manifest import Manifest  # noqa: E402
from evaluator.policies import InfraError, policy_class  # noqa: E402

EXECUTORS = ("slurm", "local")
LOCAL_EXECUTORS = {}             # campaign directory -> its LocalExecutor, shared by every stage in this process
GENERATE_RESOURCES = {"partition": "gpuqs", "gres": "gpu:a100:1", "cpus-per-task": 4, "mem": "24G", "time": "00:30:00"}
AGGREGATE_RESOURCES = {"partition": "cpuqs", "cpus-per-task": 1, "mem": "4G", "time": "00:20:00"}
HANDLE_RESOURCES = {"partition": "cpuqs", "cpus-per-task": 1, "mem": "1G", "time": "00:10:00"}
HEARTBEAT_RESOURCES = {"partition": "cpuqs", "cpus-per-task": 1, "mem": "1G", "time": "00:05:00"}
EVALUATE_MARGIN_S = 600          # above the policy's own worst case: queue-free start-up, copying, writing
STALL_GRACE_S = 600              # the heartbeat restarts a chain only once status.json is this old
NAMED_WAIT_S = 60                # a job can start before its submitter records it: how long it waits to be named
# FAULTS (tests only): faults.json = {"infra": [{"generation": g, "index": i, "attempts": [1] or "all"}],
#     "generate_fail": [{"generation": g, "attempts": [1]}], "aggregate_crash": [g, or {"generation": g, "attempts": [1]}],
#     "aggregate_crash_after_save": [g, ...]}. attempts count a stage's job restarts (stage_attempt).


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

    def param(self, key):
        """A campaign: setting, with the defaults for campaigns started before the setting existed."""
        return self.params.get(key, runspec.CAMPAIGN_DEFAULTS.get(key))

    def halt(self, reason, message):
        self.set_status(state="halted", reason=reason, message=message)
        self.event("halted", reason=reason, message=message)
        print(f"campaign {self.id} halted ({reason}): {message}")
        if reason != "stopped":          # a stop was asked for; every other halt needs someone
            notify(self, f"AFAR_campaign_{self.id}_halted_{reason}")

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
        resources = {"generate": GENERATE_RESOURCES, "aggregate": AGGREGATE_RESOURCES,
                     "handle": HANDLE_RESOURCES}.get(stage) or self.evaluate_resources()
        exclude = set(kwargs.pop("exclude", ()))
        if stage == "generate":
            exclude |= set(self.meta.get("exclude", []))
        job = self.executor().submit(stage, str(self.path), g, resources=resources, exclude=sorted(exclude), **kwargs)
        self.event("submitted", stage=stage, generation=g, job_id=job, exclude=sorted(exclude),
                   indices=kwargs.get("indices"), after=kwargs.get("after"))
        return job

    def submit_stage(self, stage, g, after=None, exclude=()):
        """A generate or aggregate job, plus its handler, which runs only if the job fails."""
        job = self.submit(stage, g, after=after, exclude=exclude)
        handler = self.submit("handle", g, after=job, after_type="afternotok", mail=True,
                              env={"AFAR_STAGE": stage, "AFAR_STAGE_JOB": job})
        return job, handler

    def submit_evaluation(self, g, indices, attempt, exclude=(), stage_attempt=1, running_array=None):
        """Evaluate tasks `indices` of generation g (or wait for running_array), then aggregate it."""
        jobs = []
        after = running_array
        if indices:
            after = self.submit("evaluate", g, indices=indices, exclude=exclude)
        if after:
            jobs.append(after)
        job, handler = self.submit_stage("aggregate", g, after=after)
        jobs.append(job)
        self.set_status(state="running", generation=g, stage="evaluating", jobs=jobs, attempt=attempt,
                        stage_job=job, handler_job=handler, stage_attempt=stage_attempt, stage_exclude=[])

    def submit_generation(self, g, stage_attempt=1, exclude=()):
        job, handler = self.submit_stage("generate", g, exclude=exclude)
        self.set_status(state="running", generation=g, stage="generating", jobs=[job], attempt=1, stage_job=job,
                        handler_job=handler, stage_attempt=stage_attempt, stage_exclude=sorted(set(exclude)))

    def submit_heartbeat(self):
        """The next heartbeat, heartbeat_minutes from now; singleton, so two never run at once.
        Not an event: a heartbeat writes events.jsonl only when no stage job is running."""
        job = self.executor().submit(
            "heartbeat", str(self.path), None, resources=HEARTBEAT_RESOURCES, singleton=True, mail=True,
            begin=f"now+{self.param('heartbeat_minutes')}minutes", name=f"afar-heartbeat-{self.id}")
        results.write_atomic(self.path / "heartbeat.json", {**read_json(self.path / "heartbeat.json", {}),
                                                            "next": job, "submitted_at": now()})
        return job

    def stage_jobs(self):
        """(the job whose end advances the current stage, its handler); M5 campaigns record only jobs."""
        st = self.status
        return st.get("stage_job") or (st.get("jobs") or [None])[-1], st.get("handler_job")

    def named(self, field, job):
        """Whether status.json names job as its stage_job or handler_job. A job can start before its
        submitter records it, so wait a little. Campaigns from M5 record neither: every job counts."""
        deadline = time.monotonic() + NAMED_WAIT_S
        while True:
            st = self.status
            if not job or field not in st or st[field] == job:
                return True
            if time.monotonic() >= deadline:
                print(f"job {job} is not this campaign's {field} ({st[field]}): a duplicate or an orphan; nothing to do")
                return False
            time.sleep(5)


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
                      seed_suffix=r["seed"].suffix, mail=mail_enabled())
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
    c.event("started", spec=runspec.rel(spec_file), executor=executor, campaign=c.params, mail=mail_enabled())
    c.submit_evaluation(0, [0], attempt=1)
    c.submit_heartbeat()
    print(f"campaign {cid}: {path}")
    warn_without_mail()
    return c


def generate(c, g, api_base=None, needs_llm=False, job=None):
    """Write generation g's candidates (on a GPU node, with the server up), then submit their evaluation."""
    gd = c.gen_dir(g)
    done = (gd / "generation.json").exists()
    st = c.status
    current = st["state"] == "running" and st["stage"] == "generating" and st["generation"] == g
    if current and not c.named("stage_job", job or os.environ.get("SLURM_JOB_ID")):
        return 1 if needs_llm else 0
    if current and not done and fault_hit(c, "generate_fail", g, st.get("stage_attempt", 1)):
        raise CampaignError(f"injected by faults.json: the generation job of generation {g} failed")
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


def aggregate(c, g, job=None):
    """Retry missing evaluations, or add generation g to the database, then start g+1 (or finish, or halt)."""
    st = c.status
    if st["state"] != "running" or st["generation"] != g or st["stage"] not in ("evaluating", "aggregating"):
        print(f"generation {g} is not being evaluated ({st['state']}, {st['stage']} {st['generation']}); nothing to do")
        return 0
    if not c.named("stage_job", job or os.environ.get("SLURM_JOB_ID")):
        return 0
    st = c.set_status(stage="aggregating")
    gd = c.gen_dir(g)
    tasks = read_json(gd / "tasks.json")
    missing = [cand for cand in tasks if not (gd / "results" / f"{cand}.json").exists()]
    if missing and c.stopped():
        c.halt("stopped", f"STOP file found with {len(missing)} evaluation(s) of generation {g} missing")
        return 0
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
        if fault_hit(c, "aggregate_crash", g, st.get("stage_attempt", 1)):
            raise CampaignError("injected by faults.json: aggregate crashed before saving")
        oe.save_checkpoint(db, c.path / "checkpoints", g * c.params["candidates_per_generation"],
                           c.meta["seed_suffix"])
        if g in c.faults().get("aggregate_crash_after_save", []):
            raise CampaignError("injected by faults.json: aggregate crashed after saving")
        c.event("aggregated", generation=g, programs_added=added)
    summary = summarize(c, g)
    results.write_atomic(gd / "summary.json", summary)

    end = None if c.stopped() else limit_reached(c, g, summary)
    if c.stopped():
        c.halt("stopped", f"STOP file found after generation {g}")
    elif g >= c.params["generations"]:
        finish(c, g, summary, None, f"{g} generation(s); best combined_score {summary['best_overall']}")
    elif end and end[0] == "finished":
        finish(c, g, summary, end[1], end[2])
    elif end:
        c.halt(end[1], end[2])
    else:
        c.submit_generation(g + 1)
    return 0


def finish(c, g, summary, reason, message):
    c.set_status(state="finished", stage="done", jobs=[], reason=reason, message=message)
    c.event("finished", generation=g, best=summary["best_overall"], reason=reason)
    print(f"campaign {c.id} finished after generation {g}: {message}")


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


def record_usage(c):
    """CPU- and GPU-hours of every job the campaign recorded (heartbeats excluded: about 6 CPU-minutes a day)."""
    jobs = [e["job_id"] for e in read_events(c) if e["event"] == "submitted" and e.get("job_id")]
    cpu, gpu = c.executor().usage(jobs)
    results.write_atomic(c.path / "budget.json", {"cpu_hours": round(cpu, 2), "gpu_hours": round(gpu, 2),
                                                  "jobs": len(jobs), "at": now()})
    return cpu, gpu


def limit_reached(c, g, summary):
    """("finished" or "halted", reason, message) if a stop condition holds after generation g, else None.
    Checked once per generation, so a limit can be overshot by up to one generation."""
    best = summary["best_overall"]
    target = c.param("target_fitness")
    if target is not None and best is not None and best >= target:
        return "finished", "target", f"best combined_score {best} reached target_fitness {target} after generation {g}"
    hours = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.datetime.fromisoformat(c.meta["created_at"])).total_seconds() / 3600
    if c.param("max_wall_hours") and hours >= c.param("max_wall_hours"):
        return "finished", "wall_time", (f"{hours:.1f} h, the max_wall_hours of {c.param('max_wall_hours')}, "
                                         f"after generation {g}; best combined_score {best}")
    try:
        cpu, gpu = record_usage(c)
    except Exception as e:               # accounting can lag or fail; the next generation checks again
        print(f"warning: could not read this campaign's CPU and GPU use: {e}", file=sys.stderr)
        cpu = gpu = 0.0
    for used, key, unit in ((cpu, "max_cpu_hours", "CPU"), (gpu, "max_gpu_hours", "GPU")):
        if c.param(key) and used >= c.param(key):
            return "halted", "budget", f"{used:.1f} {unit}-hours used, the {key} of {c.param(key)}, after generation {g}"
    failed = 0
    for h in range(g, 0, -1):
        s = summary if h == g else read_json(c.gen_dir(h) / "summary.json", {})
        if s.get("statuses", {}).get("completed", 0):
            break
        failed += 1
    if failed >= c.param("max_failed_generations"):
        return "halted", "no_progress", (f"{failed} generation(s) in a row with no completed candidate "
                                         f"(generations {g - failed + 1}-{g})")
    free = free_gb(c.path)
    if free < c.param("min_free_gb"):
        return "halted", "storage", f"{free:.0f} GB free on {c.path.parent}, below min_free_gb {c.param('min_free_gb')}"
    return None


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


def run_stage(stage, campaign, g, index, job=None, env=None):
    """How LocalExecutor runs a job; Slurm jobs run the same functions through the CLI."""
    c = Campaign(campaign)
    if stage == "generate":
        return generate(c, g, api_base=os.environ.get("AFAR_LLM_API_BASE"), job=job)
    if stage == "evaluate":
        return evaluate_task(c, g, index)
    if stage == "handle":
        return handle(c, g, env["AFAR_STAGE"], env["AFAR_STAGE_JOB"], job=job)
    if stage == "heartbeat":
        return heartbeat(c, job=job)
    return aggregate(c, g, job=job)


def read_events(c):
    """events.jsonl as a list, skipping any line that isn't whole JSON."""
    events = []
    for line in (c.path / "events.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events


def fault_hit(c, key, g, attempt):
    """Whether faults.json injects `key` into generation g at this stage attempt (tests only)."""
    for f in c.faults().get(key, []):
        if f == g or (isinstance(f, dict) and f.get("generation") == g
                      and (f.get("attempts") == "all" or attempt in f.get("attempts", []))):
            return True
    return False


# --- the watchdog (M6) --------------------------------------------------------

def restart_stage(c, exclude=(), operator=False):
    """Resubmit the current stage's job and its handler; None if that job is still queued or running.
    An evaluation array that is still running is kept: the new aggregation waits for it."""
    st = c.status
    stage_job, _ = c.stage_jobs()
    if c.executor().alive([stage_job]):
        return None
    g, stage = st["generation"], st["stage"]
    stage_attempt = 1 if operator else st.get("stage_attempt", 1) + 1
    if stage == "generating":
        c.submit_generation(g, stage_attempt=stage_attempt,
                            exclude=sorted(set(st.get("stage_exclude", [])) | set(exclude)))
        return c.status["jobs"]
    gd = c.gen_dir(g)
    tasks = read_json(gd / "tasks.json")
    missing = [i for i, cand in enumerate(tasks) if not (gd / "results" / f"{cand}.json").exists()]
    if operator and st["state"] == "halted" and missing:
        # After an infra halt, the missing evaluations get a fresh set of attempts.
        results.write_atomic(gd / "attempts.json", {**read_json(gd / "attempts.json", {}),
                                                    **{tasks[i]: 1 for i in missing}})
        c.submit_evaluation(g, missing, attempt=1)
    else:
        running = c.executor().alive([j for j in (st.get("jobs") or []) if j != stage_job])
        c.submit_evaluation(g, [], attempt=st.get("attempt", 1), stage_attempt=stage_attempt,
                            running_array=running[0] if running else None)
    return c.status["jobs"]


def handle(c, g, stage, failed_job, job=None):
    """A generate or aggregate job failed (afternotok): restart it, up to max_stage_retries times, or halt."""
    if not c.named("handler_job", job or os.environ.get("SLURM_JOB_ID")):
        return 0
    st = c.status
    if st["state"] != "running" or c.stage_jobs()[0] != failed_job:
        print(f"the campaign has moved on ({st['state']}, {st['stage']} {st['generation']}); nothing to do")
        return 0
    if c.stopped():
        c.halt("stopped", f"STOP file found after the {stage} job {failed_job} of generation {g} ended")
        return 0
    info = c.executor().job_info(failed_job)
    attempt = st.get("stage_attempt", 1)
    c.event("stage_failed", stage=stage, generation=g, failed_job=failed_job, attempt=attempt, **info)
    if attempt > c.param("max_stage_retries"):
        log = c.path / "logs" / f"{stage}-g{g}-{failed_job}.out"
        c.halt("stage_failed", f"the {stage} job of generation {g} failed {attempt} time(s); the last, job "
                               f"{failed_job}, ended {info['state']} (exit {info['exit_code']}) on "
                               f"{info['node'] or 'no node'}. Log: {log}")
        return 0
    # A generation job's failure is usually its GPU or node, so its restart avoids that node.
    restart_stage(c, exclude=[info["node"]] if stage == "generate" and info["node"] else ())
    print(f"restarted the {stage} job of generation {g} (attempt {attempt + 1})")
    return 0


def heartbeat(c, job=None):
    """The periodic check: restart a chain left with no live job (a handler that failed or never ran)."""
    job = job or os.environ.get("SLURM_JOB_ID")
    path = c.path / "heartbeat.json"
    st, hb = c.status, read_json(path, {})
    if st["state"] != "running":
        results.write_atomic(path, {**hb, "last_run": now(), "last_job": job, "last_action": f"ended ({st['state']})"})
        print(f"campaign {c.id} is {st['state']}: the heartbeat ends")
        return 0
    if hb.get("next") not in (None, job) and c.executor().alive([hb["next"]]):
        print(f"heartbeat {hb['next']} is already due, so this one ({job}) is a duplicate and ends")
        return 0
    c.submit_heartbeat()                 # first, so a check that fails can't end the chain
    stage_job, handler_job = c.stage_jobs()
    live = c.executor().alive([stage_job, handler_job])
    age = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.datetime.fromisoformat(st["updated_at"])).total_seconds()
    action = "ok"
    if not live and age >= STALL_GRACE_S:
        attempt = st.get("stage_attempt", 1)
        if c.stopped():
            c.halt("stopped", f"STOP file found with no live job for generation {st['generation']}")
            action = "halted"
        elif attempt > c.param("max_stage_retries"):
            c.halt("stalled", f"no live job for the {st['stage']} stage of generation {st['generation']} after "
                              f"{attempt} attempt(s), {age / 60:.0f} min since the last change (jobs {st.get('jobs')}, "
                              f"handler {handler_job}). Logs: {c.path / 'logs'}")
            action = "halted"
        else:
            c.event("stalled", generation=st["generation"], stage=st["stage"], stage_job=stage_job,
                    handler_job=handler_job, minutes=round(age / 60))
            restart_stage(c)
            action = "restarted"
    results.write_atomic(path, {**read_json(path, {}), "last_run": now(), "last_job": job, "last_action": action})
    print(f"heartbeat {job}: {action} ({st['stage']} {st['generation']}, live jobs {live})")
    return 0


def ensure_heartbeat(c):
    nxt = read_json(c.path / "heartbeat.json", {}).get("next")
    if not (nxt and c.executor().alive([nxt])):
        c.submit_heartbeat()


def mail_enabled():
    return bool(os.environ.get("AFAR_MAIL_USER"))


def warn_without_mail():
    if not mail_enabled():
        print("warning: AFAR_MAIL_USER is not set, so this campaign can't mail you if it halts; set it before "
              "make campaign-start or campaign-resume", file=sys.stderr)


def notify(c, name):
    """Mail AFAR_MAIL_USER a message whose subject carries `name`. The address comes from the environment
    only and is never recorded. A failure becomes an event, never an error, so a halt can't fail because
    of mail. Slurm sends it (executor.mail): compute nodes can't, so the details stay in campaign-status."""
    to = os.environ.get("AFAR_MAIL_USER")
    if not to:
        c.event("mail_skipped", name=name, reason="AFAR_MAIL_USER is not set")
        return False
    try:
        job = c.executor().mail(name, to)
    except Exception as e:
        c.event("mail_failed", name=name, error=str(e)[:300])
        return False
    c.event("mailed", name=name, mail_job=job)
    return True


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
    hb = read_json(c.path / "heartbeat.json")
    if hb:
        print(f"  heartbeat: every {c.param('heartbeat_minutes')} min; last ran {hb.get('last_run', 'not yet')}"
              f" ({hb.get('last_action', '-')}); next job {hb.get('next')}")
    end = datetime.datetime.now(datetime.timezone.utc) if st["state"] == "running" \
        else datetime.datetime.fromisoformat(st["updated_at"])
    hours = (end - datetime.datetime.fromisoformat(c.meta["created_at"])).total_seconds() / 3600
    used = [f"{hours:.1f} h" + (f" of max_wall_hours {c.param('max_wall_hours')}" if c.param("max_wall_hours") else "")]
    budget = read_json(c.path / "budget.json")
    if budget:
        for value, key, unit in ((budget["cpu_hours"], "max_cpu_hours", "CPU"), (budget["gpu_hours"], "max_gpu_hours", "GPU")):
            used.append(f"{value} {unit}-hours" + (f" of {key} {c.param(key)}" if c.param(key) else ""))
        used[-1] += f" (as of {budget['at']})"
    print(f"  used: {'; '.join(used)}; mail on halt: {'on' if c.meta.get('mail') else 'off'}")
    if c.stopped():
        print("  STOP file present: the campaign halts after the current stage")
    return 0


def stop(c, cancel=False):
    """Halt after the current stage (a STOP file); with cancel, cancel the stage's jobs and halt now."""
    (c.path / "STOP").write_text(f"requested {now()}\n")
    c.event("stop_requested", cancel=cancel)
    st = c.status
    if not cancel or st["state"] != "running":
        print(f"campaign {c.id}: STOP written; " + ("it halts after the current stage" if st["state"] == "running"
                                                     else f"it is already {st['state']}"))
        return 0
    cancelled = []
    for _ in range(3):                   # a stage that is ending may submit the next: cancel until none is left
        st = c.status
        live = c.executor().alive([j for j in (st.get("jobs") or []) + [st.get("handler_job")] if j])
        if not live:
            break
        c.executor().cancel(live)
        cancelled += live
        deadline = time.monotonic() + 90  # Slurm's SIGTERM, then SIGKILL after KillWait
        while c.executor().alive(live) and time.monotonic() < deadline:
            time.sleep(5)
    c.halt("stopped", f"stopped now (make campaign-stop NOW=1); cancelled jobs {cancelled}")
    return 0


def resume(c):
    st = c.status
    if st["state"] == "finished":
        print(f"campaign {c.id} already finished")
        return 0
    if st["state"] == "halted" and st.get("reason") == "budget":
        raise CampaignError(f"campaign {c.id} halted on its budget ({st['message']}). The limits are part of its "
                            f"spec, so to go further, start a new campaign from its last checkpoint")
    stage_job, _ = c.stage_jobs()
    if c.executor().alive([stage_job]):
        raise CampaignError(f"job {stage_job} of the current stage is still queued or running; wait, or scancel it first")
    if (c.path / "STOP").exists():
        (c.path / "STOP").unlink()
        c.event("stop_cleared")
    g, stage = st["generation"], st["stage"]
    c.event("resumed", generation=g, stage=stage, previous=st["state"], mail=mail_enabled())
    restart_stage(c, operator=True)
    c.set_status(reason=None, message=None)
    ensure_heartbeat(c)
    print(f"campaign {c.id}: resumed at generation {g}, {stage}")
    warn_without_mail()
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
    global NAMED_WAIT_S, STALL_GRACE_S, free_gb
    from controller import oe_adapter as oe
    import yaml
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<52} {detail}")

    saved = {k: os.environ.get(k) for k in ("AFAR_RESULTS", "AFAR_LLM_API_BASE", "AFAR_LLM_API_KEY", "AFAR_MAIL_USER",
                                            "PATH", "FAKE_SBATCH_FAIL", "FAKE_SBATCH_REJECT")}
    saved_globals = (NAMED_WAIT_S, STALL_GRACE_S, free_gb, SlurmExecutor.retry_delays)
    NAMED_WAIT_S = STALL_GRACE_S = 0     # local jobs start when named, and a local run can't wait 10 min
    tmp = Path(tempfile.mkdtemp(prefix="afar-campaign-selftest-"))
    server = server2 = None
    try:
        os.environ["AFAR_RESULTS"] = str(tmp / "results")
        # The local executor records mail instead of sending it.
        os.environ["AFAR_MAIL_USER"] = "selftest@invalid"

        def mailed(c, reason):
            return f"AFAR_campaign_{c.id}_halted_{reason}" in c.executor().mails
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
        # min_free_gb 0: this runs in /tmp, with far less free than a campaign's 200 GB floor.
        spec["campaign"].update(generations=3, candidates_per_generation=6, min_free_gb=0)
        spec["description"] = "campaign selftest: m5-dev-v1 on crc2-tiny-v1 with the fake LLM"
        spec_path = tmp / "campaign-selftest.yaml"
        spec_path.write_text(yaml.safe_dump(spec))

        def variant(name, **campaign):
            data = json.loads(json.dumps(spec))
            data["campaign"].update(campaign)
            path = tmp / f"campaign-{name}.yaml"
            path.write_text(yaml.safe_dump(data))
            return path

        # (a) three generations, unattended, with (b) one task failing once and (d) aggregate crashing,
        # on every attempt, so its handler restarts it twice and then halts (M6 b).
        c = start(spec_path, name="selftest-a", executor="local")
        (c.path / "faults.json").write_text(json.dumps({"infra": [{"generation": 1, "index": 1, "attempts": [1]}],
                                                        "aggregate_crash": [2]}))
        failed = drain_local(c)
        st = c.status
        check("(d) aggregate crash leaves the campaign resumable", st["stage"] == "aggregating" and st["generation"] == 2
              and any(f[1] == "aggregate" for f in failed), f"{st['state']} {st['stage']} {st['generation']}")
        crashes = [e for e in read_events(c) if e["event"] == "stage_failed"]
        check("(M6 b) a persistent crash: restarted twice, then halts", st["state"] == "halted"
              and st["reason"] == "stage_failed" and [e["attempt"] for e in crashes] == [1, 2, 3],
              f"{st['state']} ({st.get('reason')}), attempts {[e['attempt'] for e in crashes]}")
        check("(M6 b) the halt mails, without recording the address", mailed(c, "stage_failed")
              and "selftest@invalid" not in (c.path / "events.jsonl").read_text() + (c.path / "campaign.json").read_text())
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
        check("(c) and mails", mailed(c2, "infra"))

        # (e) a STOP file halts after the current stage; resume clears it.
        c3 = start(spec_path, name="selftest-e", executor="local")
        stop(c3)
        drain_local(c3)
        st = c3.status
        check("(e) STOP halts after the current stage", st["state"] == "halted" and st["reason"] == "stopped"
              and c3.checkpoint(0).exists() and not c3.gen_dir(1).exists(), st.get("message", ""))
        check("(e) a stop that was asked for doesn't mail", not mailed(c3, "stopped"))
        resume(c3)
        drain_local(c3)
        check("(e) resume after STOP continues to the end", c3.status["state"] == "finished")

        # --- M6: the watchdog -------------------------------------------------------------
        # (M6 a) a generation job that fails once is restarted by its handler, off the node it failed on.
        cg = start(variant("g", generations=1), name="selftest-g", executor="local")
        (cg.path / "faults.json").write_text(json.dumps({"generate_fail": [{"generation": 1, "attempts": [1]}]}))
        drain_local(cg)
        events = read_events(cg)
        fails = [e for e in events if e["event"] == "stage_failed"]
        jobs = [e for e in events if e["event"] == "submitted" and e["stage"] == "generate"]
        check("(M6 a) a failed generation job is restarted", cg.status["state"] == "finished" and len(fails) == 1
              and fails[0]["stage"] == "generate" and len(jobs) == 2 and jobs[1]["exclude"] == ["localhost"],
              f"{len(fails)} failure(s), {len(jobs)} generation job(s), exclude {jobs[-1]['exclude'] if jobs else '-'}")

        # (M6 c) the stage job and its handler vanish without failing: only the heartbeat can notice.
        cs = start(variant("s", generations=1), name="selftest-s", executor="local")
        st = cs.status
        cs.executor().drop(st["jobs"] + [st["handler_job"]])
        drain_local(cs)
        stalls = [e for e in read_events(cs) if e["event"] == "stalled"]
        check("(M6 c) the heartbeat restarts a stalled chain", cs.status["state"] == "finished" and len(stalls) == 1,
              f"{len(stalls)} stall(s); {cs.status['state']}")

        # (M6 d) the heartbeat's safeguards: one name, singleton, hourly; it ends with the campaign and
        # when another heartbeat is already due (the spec check of its floor is in runspec --selftest).
        ex = cs.executor()
        beats = [s for s in ex.submitted if s["stage"] == "heartbeat"]
        check("(M6 d) heartbeats: one name, singleton, hourly, mail on failure", len(beats) >= 2 and all(
            b["singleton"] and b["name"] == "afar-heartbeat-selftest-s" and b["begin"] == "now+60minutes" and b["mail"]
            for b in beats), f"{len(beats)} heartbeat(s)")
        n = len(ex.submitted)
        heartbeat(cs, job="after-the-end")
        check("(M6 d) a heartbeat of an ended campaign has no successor", len(ex.submitted) == n)
        cd = start(variant("d", generations=1), name="selftest-d", executor="local")
        n = len(cd.executor().submitted)
        heartbeat(cd, job="duplicate")
        check("(M6 d) a duplicate heartbeat ends without a successor", len(cd.executor().submitted) == n)

        # (M6 d) Slurm commands, through a stand-in sbatch that records them and times out twice first.
        fake = tmp / "bin"
        fake.mkdir()
        (fake / "sbatch").write_text(
            f'#!/bin/bash\nn=$(( $(cat "{fake}/count" 2>/dev/null || echo 0) + 1 )); echo $n > "{fake}/count"\n'
            f'printf "%s\\n" "$*" >> "{fake}/sbatch.log"\n'
            'if [[ -n "${FAKE_SBATCH_REJECT:-}" ]]; then echo "sbatch: error: invalid partition specified: x" >&2; exit 1; fi\n'
            'if (( n <= ${FAKE_SBATCH_FAIL:-0} )); then\n'
            '    echo "sbatch: error: Batch job submission failed: Socket timed out on send/recv operation" >&2; exit 1\nfi\n'
            'echo $(( 7000 + n ))\n')
        (fake / "squeue").write_text("#!/bin/bash\nexit 0\n")
        for f in ("sbatch", "squeue"):
            (fake / f).chmod(0o755)
        os.environ.update(PATH=f"{fake}:{saved['PATH']}", FAKE_SBATCH_FAIL="2")
        SlurmExecutor.retry_delays = (0, 0, 0)
        cz = start(variant("slurm"), name="selftest-slurm", executor="slurm")
        sent = (fake / "sbatch.log").read_text().splitlines()
        check("(M6 d) a timed-out sbatch is retried", len(sent) == 6 and sent[0] == sent[1] == sent[2]
              and cz.status["jobs"] == ["7003", "7004"], f"{len(sent)} sbatch calls, jobs {cz.status['jobs']}")
        handler = next((s for s in sent if "campaign_handle.sbatch" in s), "")
        beat = next((s for s in sent if "campaign_heartbeat.sbatch" in s), "")
        check("(M6 d) the handler waits on its job's failure", all(x in handler for x in (
            "--dependency=afternotok:7004", "--kill-on-invalid-dep=yes", "AFAR_STAGE=aggregate,AFAR_STAGE_JOB=7004",
            "--mail-type=FAIL", "--mail-user=selftest@invalid")), handler[:60])
        check("(M6 d) the heartbeat's Slurm flags", all(x in beat for x in (
            "--begin=now+60minutes", "--dependency=singleton", "--job-name=afar-heartbeat-selftest-slurm")), beat[:60])
        cz.halt("infra", "a halt to test mail")
        sent = (fake / "sbatch.log").read_text().splitlines()
        check("(M6 b) a halt mails through Slurm", len(sent) == 7 and all(x in sent[-1] for x in (
            "--job-name=AFAR_campaign_selftest-slurm_halted_infra", "--mail-type=END", "--mail-user=selftest@invalid",
            "--wrap=true")) and read_events(cz)[-1]["event"] == "mailed", sent[-1][:60])
        os.environ.update(FAKE_SBATCH_REJECT="1")
        try:
            SlurmExecutor(tmp / "x", 4).submit("aggregate", str(tmp / "x"), 0)
            rejected = False
        except ExecutorError:
            rejected = True
        check("(M6 d) a rejected sbatch is not retried", rejected and int((fake / "count").read_text()) == 8)
        for k in ("PATH", "FAKE_SBATCH_FAIL", "FAKE_SBATCH_REJECT"):
            os.environ.pop(k, None) if saved[k] is None else os.environ.__setitem__(k, saved[k])
        SlurmExecutor.retry_delays = saved_globals[3]

        # (M6 e) every stop condition, checked after generation 0 (the seed scores exactly 1.0).
        def ends(name, state, reason, setup=None, generation=0, **campaign):
            ce = start(variant(name, generations=2, **campaign), name=f"selftest-{name}", executor="local")
            if setup:
                setup(ce)
            drain_local(ce)
            st = ce.status
            check(f"(M6 e) {name}: {state} ({reason})", st["state"] == state and st.get("reason") == reason
                  and st["generation"] == generation, f"{st['state']} ({st.get('reason')}): {(st.get('message') or '')[:60]}")
            return ce
        ends("target", "finished", "target", target_fitness=1.0)
        ends("wall-time", "finished", "wall_time", max_wall_hours=1e-6)
        cb = ends("budget", "halted", "budget", setup=lambda ce: setattr(ce.executor(), "fake_usage", (20.0, 0.0)),
                  max_cpu_hours=10)
        try:
            resume(cb)
            refused = False
        except CampaignError as e:
            refused = "budget" in str(e)
        check("(M6 e) a budget halt can't be resumed, and mails", refused and mailed(cb, "budget"))
        free_gb = lambda path: 1.0
        ends("storage", "halted", "storage", min_free_gb=200)
        free_gb = saved_globals[2]
        # No completed candidate: a second fake server answers only with code that doesn't compile.
        (tmp / "replies-bad").mkdir()
        (tmp / "replies-bad" / "3-syntax").write_text(f"```cpp\n{replies['3-syntax']}```\n")
        server2 = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fake_llm_server.py"), "--port", str(port + 1),
                                    "--served-model-name", "afar-llm", "--reply-dir", str(tmp / "replies-bad")],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        os.environ["AFAR_LLM_API_BASE"] = f"http://127.0.0.1:{port + 1}/v1"
        ends("no-progress", "halted", "no_progress", generation=1, max_failed_generations=1)
        os.environ["AFAR_LLM_API_BASE"] = f"http://127.0.0.1:{port}/v1"

        # (M6 f) stop --now cancels the stage's jobs and halts at once: no retry, no mail.
        cn = start(variant("now", generations=1), name="selftest-now", executor="local")
        stop(cn, cancel=True)
        drain_local(cn)
        events = read_events(cn)
        after = events[[e["event"] for e in events].index("stop_requested"):]
        check("(M6 f) stop --now halts at once, no retry, no mail", cn.status["state"] == "halted"
              and cn.status["reason"] == "stopped" and not any(e["event"] in ("submitted", "stage_failed", "mailed")
                                                              for e in after), f"{[e['event'] for e in after]}")

        # The last checkpoint is a standard OpenEvolve checkpoint: openevolve-run continues from it.
        out = subprocess.run([str(ROOT / ".venv" / "controller" / "bin" / "openevolve-run"), str(ROOT / "seeds" / "evolved_repl.h"),
                              str(ROOT / "evaluator" / "compile_check.py"), "--config", str(c.path / "resolved" / "config.yaml"),
                              "--checkpoint", str(c.checkpoint(3)), "--iterations", "1", "--output", str(tmp / "oe-continue"),
                              "--api-base", os.environ["AFAR_LLM_API_BASE"]],
                             capture_output=True, text=True, timeout=600, env=dict(os.environ, AFAR_CXX=os.environ.get("AFAR_CXX", "g++")))
        check("openevolve-run continues from a campaign checkpoint", out.returncode == 0
              and "Loaded database with" in out.stdout + out.stderr, f"exit {out.returncode}")
    finally:
        for s in (server, server2):
            if s:
                s.terminate()
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        NAMED_WAIT_S, STALL_GRACE_S, free_gb, SlurmExecutor.retry_delays = saved_globals
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
        p = sub.add_parser(name)
        p.add_argument("campaign")
        if name == "stop":
            p.add_argument("--now", action="store_true", help="also cancel the current stage's jobs, and halt now")
    for name in ("generate", "evaluate-task", "aggregate", "handle", "heartbeat"):
        p = sub.add_parser(name)
        p.add_argument("--campaign", default=os.environ.get("AFAR_CAMPAIGN"))
        if name == "heartbeat":
            continue
        p.add_argument("--generation", type=int, default=os.environ.get("AFAR_GENERATION"))
        if name == "handle":
            p.add_argument("--stage", default=os.environ.get("AFAR_STAGE"), choices=("generate", "aggregate"))
            p.add_argument("--job", default=os.environ.get("AFAR_STAGE_JOB"), help="the stage job that failed")
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
            code = stop(c, args.now) if args.command == "stop" else \
                {"status": show_status, "resume": resume}[args.command](c)
            drain_local(c)
            return code
        c = Campaign(args.campaign)
        if args.command == "heartbeat":
            return heartbeat(c)
        g = int(args.generation)
        if args.command == "handle":
            return handle(c, g, args.stage, args.job)
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
