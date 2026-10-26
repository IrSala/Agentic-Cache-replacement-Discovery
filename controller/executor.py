"""Where a campaign's jobs run (M5, M6): Slurm jobs on HPC3, or in order on this host for tests.

Both take the same requests, so a test exercises exactly the jobs a real campaign submits:
    submit(stage, campaign, generation, indices=None, after=None, after_type="afterany", exclude=(),
           resources={}, begin=None, singleton=False, name=None, mail=False, env={}) -> job id
stage is "generate", "evaluate" (one task per index), "aggregate", "handle" (a stage job's
failure handler: after_type="afternotok") or "heartbeat" (begin= a later start, singleton=True).
    mail(name, to) -> job id      a message whose subject is `name`
plus alive(), job_info(), usage() and cancel() for the jobs they submitted.

SlurmExecutor  sbatch --parsable with the stage's script in slurm/campaign_<stage>.sbatch.
               Evaluation is an array (--array=<indices>%K); aggregation waits for
               it with afterany, since afterok on an array never fires if one task
               fails; --exclude keeps retries off nodes that failed. Every Slurm command
               is retried on errors a busy or restarting controller gives. A retried
               sbatch may leave a duplicate job; the campaign ignores any job that
               status.json doesn't name (controller/campaign.py).
LocalExecutor  queues the jobs and runs them in order with drain(); a job that raises
               is logged and treated like a failed job. An afternotok job runs only if
               the job it waits on failed, and is dropped otherwise, as Slurm does with
               --kill-on-invalid-dep. A begin= job waits until nothing else is queued.
"""

import collections
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {"generate": "slurm/campaign_generate.sbatch", "evaluate": "slurm/campaign_evaluate.sbatch",
           "aggregate": "slurm/campaign_aggregate.sbatch", "handle": "slurm/campaign_handle.sbatch",
           "heartbeat": "slurm/campaign_heartbeat.sbatch"}
# What a busy or restarting slurmctld answers: worth retrying, unlike a request it rejects.
TRANSIENT = re.compile(r"Socket timed out|Unable to contact slurm controller|temporarily unable|"
                       r"Resource temporarily unavailable|Connection (refused|reset)|Zero Bytes were transmitted|"
                       r"Communication connection failure", re.IGNORECASE)


class ExecutorError(RuntimeError):
    pass


def compact(indices):
    """[0, 1, 2, 5, 7, 8] -> "0-2,5,7-8", Slurm's array syntax."""
    indices, parts = sorted(set(indices)), []
    start = prev = None
    for i in indices + [None]:
        if start is None:
            start = prev = i
        elif i is not None and i == prev + 1:
            prev = i
        else:
            parts.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = i
    return ",".join(parts)


def gpus_in(tres):
    """GPUs in an AllocTRES string such as "cpu=4,gres/gpu=1,gres/gpu:a100=1" (counted once)."""
    generic = re.search(r"gres/gpu=(\d+)", tres)
    return int(generic.group(1)) if generic else sum(int(n) for n in re.findall(r"gres/gpu:[^=,]+=(\d+)", tres))


class SlurmExecutor:
    name = "slurm"
    retry_delays = (10, 30, 60)          # seconds before each retry of a transient error

    def __init__(self, campaign_dir, max_concurrent_tasks):
        self.campaign_dir = Path(campaign_dir)
        self.limit = max_concurrent_tasks

    def _run(self, cmd, accept=lambda out: out.returncode == 0):
        for delay in (*self.retry_delays, None):
            out = subprocess.run(cmd, capture_output=True, text=True)
            if accept(out) or delay is None or not TRANSIENT.search(out.stderr):
                return out
            print(f"{cmd[0]}: transient Slurm error, retrying in {delay}s: {out.stderr.strip()}", file=sys.stderr)
            time.sleep(delay)

    def submit(self, stage, campaign, generation, *, indices=None, after=None, after_type="afterany", exclude=(),
               resources=None, begin=None, singleton=False, name=None, mail=False, env=None):
        logs = self.campaign_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        name = name or f"afar-{stage}-{self.campaign_dir.name}-g{generation}"
        if indices is not None:
            output = logs / f"{stage}-g{generation}-%A_%a.out"
        else:
            output = logs / (f"{stage}-%j.out" if generation is None else f"{stage}-g{generation}-%j.out")
        exports = {"AFAR_REPO": ROOT, "AFAR_CAMPAIGN": self.campaign_dir, **(env or {})}
        if generation is not None:
            exports["AFAR_GENERATION"] = generation
        cmd = ["sbatch", "--parsable", f"--job-name={name}", f"--chdir={ROOT}", f"--output={output}",
               "--export=ALL," + ",".join(f"{k}={v}" for k, v in exports.items())]
        for key, value in (resources or {}).items():
            cmd.append(f"--{key}={value}")
        if indices is not None:
            cmd.append(f"--array={compact(indices)}%{self.limit}")
        dependencies = ([f"{after_type}:{after}"] if after else []) + (["singleton"] if singleton else [])
        if dependencies:
            cmd.append(f"--dependency={','.join(dependencies)}")
        if after:
            cmd.append("--kill-on-invalid-dep=yes")
        if begin:
            cmd.append(f"--begin={begin}")
        if exclude:
            cmd.append(f"--exclude={','.join(sorted(set(exclude)))}")
        if mail and os.environ.get("AFAR_MAIL_USER"):
            cmd += ["--mail-type=FAIL", f"--mail-user={os.environ['AFAR_MAIL_USER']}"]
        cmd.append(str(ROOT / SCRIPTS[stage]))
        out = self._run(cmd)
        if out.returncode != 0:
            raise ExecutorError(f"sbatch for {stage} failed ({out.returncode}): {out.stderr.strip()}")
        return out.stdout.strip().split(";")[0]

    def alive(self, job_ids):
        """The jobs (or array tasks) among job_ids still pending or running."""
        ids = [j for j in job_ids if j]
        if not ids:
            return []
        # squeue answers "Invalid job id" when none of the jobs is known any more: none is alive.
        out = self._run(["squeue", "-h", "-o", "%i", "-j", ",".join(ids)],
                        accept=lambda o: o.returncode == 0 or "Invalid job id" in o.stderr)
        if out.returncode != 0 and "Invalid job id" not in out.stderr:
            raise ExecutorError(f"squeue failed ({out.returncode}): {out.stderr.strip()}")
        live = set(out.stdout.split())
        return [j for j in ids if any(x == j or x.startswith(f"{j}_") for x in live)]

    def job_info(self, job):
        """How a finished job ended: {state, exit_code, node}, from Slurm's accounting."""
        out = self._run(["sacct", "-X", "-n", "-P", "-o", "State,ExitCode,NodeList", "-j", str(job)])
        line = next((l for l in out.stdout.splitlines() if l.strip()), "") if out.returncode == 0 else ""
        state, code, node = (line.split("|") + ["", "", ""])[:3]
        return {"state": state.split()[0] if state else "UNKNOWN", "exit_code": code or None,
                "node": node if node and node != "None assigned" else None}

    def usage(self, job_ids):
        """(CPU-hours, GPU-hours) used by job_ids, array tasks included; a running job counts its time so far."""
        ids, cpu, gpu = sorted({str(j) for j in job_ids if j}), 0.0, 0.0
        for i in range(0, len(ids), 200):
            out = self._run(["sacct", "-X", "-n", "-P", "-o", "JobIDRaw,ElapsedRaw,AllocCPUS,AllocTRES",
                             "-j", ",".join(ids[i:i + 200])])
            if out.returncode != 0:
                raise ExecutorError(f"sacct failed ({out.returncode}): {out.stderr.strip()}")
            for line in out.stdout.splitlines():
                parts = line.split("|")
                if len(parts) >= 4 and parts[1].isdigit():
                    hours = int(parts[1]) / 3600
                    cpu += hours * int(parts[2] or 0)
                    gpu += hours * gpus_in(parts[3])
        return cpu, gpu

    def cancel(self, job_ids):
        ids = [j for j in job_ids if j]
        if ids:
            out = self._run(["scancel", *ids])
            if out.returncode != 0:
                raise ExecutorError(f"scancel failed ({out.returncode}): {out.stderr.strip()}")

    def mail(self, name, to):
        """Have Slurm's controller mail `to`: a 1-second job called `name`, with --mail-type=END, so the
        subject carries the name. HPC3's compute nodes can't send mail themselves (homacs1 has no mail
        program; make mail-test, job 96270), but the controller's mail arrived."""
        logs = self.campaign_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        out = self._run(["sbatch", "--parsable", f"--job-name={name}", f"--chdir={ROOT}",
                         f"--output={logs / 'mail-%j.out'}", "--partition=cpuqs", "--cpus-per-task=1", "--mem=100M",
                         "--time=00:01:00", "--mail-type=END", f"--mail-user={to}", "--wrap=true"])
        if out.returncode != 0:
            raise ExecutorError(f"sbatch for mail failed ({out.returncode}): {out.stderr.strip()}")
        return out.stdout.strip().split(";")[0]


class LocalExecutor:
    name = "local"

    def __init__(self, runner):
        self.runner = runner                 # runner(stage, campaign, generation, index, job, env) runs one job
        self.queue = collections.deque()
        self.count = 0
        self.failed = []                     # (job id, stage, generation, index, error) of jobs that failed
        self.outcome = {}                    # job id -> "ok" or "failed"
        self.submitted = []                  # every request, for tests
        self.running = None
        self.fake_usage = (0.0, 0.0)         # what usage() reports, for tests
        self.mails = []                      # the names mail() was asked to send, for tests

    def submit(self, stage, campaign, generation, *, indices=None, after=None, after_type="afterany", exclude=(),
               resources=None, begin=None, singleton=False, name=None, mail=False, env=None):
        self.count += 1
        job = f"local-{self.count}"
        self.submitted.append({"job": job, "stage": stage, "generation": generation, "after": after,
                               "after_type": after_type, "exclude": sorted(set(exclude)), "begin": begin,
                               "singleton": singleton, "name": name, "mail": mail})
        for index in (indices if indices is not None else [None]):
            self.queue.append((job, stage, campaign, generation, index, after, after_type, dict(env or {}), bool(begin)))
        return job

    def alive(self, job_ids):
        return [j for j in job_ids if j and (j == self.running or any(e[0] == j for e in self.queue))]

    def job_info(self, job):
        return {"state": "FAILED" if self.outcome.get(job) == "failed" else "COMPLETED", "exit_code": "1:0",
                "node": "localhost"}

    def usage(self, job_ids):
        return self.fake_usage

    def cancel(self, job_ids):
        """Like scancel: the jobs never run, and count as failed for afternotok."""
        for job in job_ids:
            if any(e[0] == job for e in self.queue):
                self.queue = collections.deque(e for e in self.queue if e[0] != job)
                self.outcome[job] = "failed"

    def mail(self, name, to):
        self.mails.append(name)
        return f"local-mail-{len(self.mails)}"

    def drop(self, job_ids):
        """Tests only: the jobs vanish without failing, so no handler fires (a stalled chain)."""
        self.queue = collections.deque(e for e in self.queue if e[0] not in job_ids)

    def drain(self):
        idle_beats = 0
        while self.queue:
            entry = self.queue.popleft()
            job, stage, campaign, generation, index, after, after_type, env, begin = entry
            if begin and any(not e[-1] for e in self.queue):
                self.queue.append(entry)     # not due while other work is queued
                continue
            if begin:
                idle_beats += 1
                if idle_beats > 1:           # a local run can't wait for the next hour
                    self.queue.clear()
                    break
            else:
                idle_beats = 0
            if after_type == "afternotok" and self.outcome.get(after) != "failed":
                continue                     # its job succeeded: dropped, as with --kill-on-invalid-dep
            self.running = job
            try:
                code = self.runner(stage, campaign, generation, index, job, env)
            except Exception:  # a crashed job, like a job that failed
                code = traceback.format_exc()
            finally:
                self.running = None
            if code not in (0, None):
                self.failed.append((job, stage, generation, index, code))
                self.outcome[job] = "failed"
            else:
                self.outcome.setdefault(job, "ok")
