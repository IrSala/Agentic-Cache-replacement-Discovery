"""Where a campaign's stages run (M5): Slurm jobs on HPC3, or in order on this host for tests.

Both take the same requests, so a test exercises exactly the stages a real
campaign runs:
    submit(stage, campaign, generation, indices=None, after=None, exclude=(), resources={}) -> job id
stage is "generate", "evaluate" (one task per index) or "aggregate".

SlurmExecutor  sbatch --parsable with the stage's script in slurm/campaign_<stage>.sbatch.
               Evaluation is an array (--array=<indices>%K); aggregation waits for
               it with afterany, since afterok on an array never fires if one task
               fails; --exclude keeps retries off nodes that failed.
LocalExecutor  queues the stages and runs them in order with drain(); a stage that
               raises is logged and treated like a failed job.
"""

import collections
import os
import subprocess
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {"generate": "slurm/campaign_generate.sbatch", "evaluate": "slurm/campaign_evaluate.sbatch",
           "aggregate": "slurm/campaign_aggregate.sbatch"}


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


class SlurmExecutor:
    name = "slurm"

    def __init__(self, campaign_dir, max_concurrent_tasks):
        self.campaign_dir = Path(campaign_dir)
        self.limit = max_concurrent_tasks

    def submit(self, stage, campaign, generation, *, indices=None, after=None, exclude=(), resources=None):
        logs = self.campaign_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        name = f"afar-{stage}-{self.campaign_dir.name}-g{generation}"
        output = logs / (f"{stage}-g{generation}-%A_%a.out" if indices is not None else f"{stage}-g{generation}-%j.out")
        cmd = ["sbatch", "--parsable", f"--job-name={name}", f"--chdir={ROOT}", f"--output={output}",
               f"--export=ALL,AFAR_REPO={ROOT},AFAR_CAMPAIGN={self.campaign_dir},AFAR_GENERATION={generation}"]
        for key, value in (resources or {}).items():
            cmd.append(f"--{key}={value}")
        if indices is not None:
            cmd.append(f"--array={compact(indices)}%{self.limit}")
        if after:
            cmd += [f"--dependency=afterany:{after}", "--kill-on-invalid-dep=yes"]
        if exclude:
            cmd.append(f"--exclude={','.join(sorted(set(exclude)))}")
        cmd.append(str(ROOT / SCRIPTS[stage]))
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0:
            raise ExecutorError(f"sbatch for {stage} failed ({out.returncode}): {out.stderr.strip()}")
        return out.stdout.strip().split(";")[0]

    def alive(self, job_ids):
        """The jobs (or array tasks) among job_ids still pending or running."""
        ids = [j for j in job_ids if j]
        if not ids:
            return []
        out = subprocess.run(["squeue", "-h", "-o", "%i", "-j", ",".join(ids)], capture_output=True, text=True)
        live = set(out.stdout.split())
        return [j for j in ids if any(x == j or x.startswith(f"{j}_") for x in live)]


class LocalExecutor:
    name = "local"

    def __init__(self, runner):
        self.runner = runner                 # runner(stage, campaign, generation, index) runs one stage
        self.queue = collections.deque()
        self.count = 0
        self.failed = []                     # (job id, stage, generation, index, error) of stages that raised

    def submit(self, stage, campaign, generation, *, indices=None, after=None, exclude=(), resources=None):
        self.count += 1
        job = f"local-{self.count}"
        for index in (indices if indices is not None else [None]):
            self.queue.append((job, stage, campaign, generation, index))
        return job

    def alive(self, job_ids):
        return []

    def drain(self):
        while self.queue:
            job, stage, campaign, generation, index = self.queue.popleft()
            try:
                code = self.runner(stage, campaign, generation, index)
            except Exception:  # a crashed stage, like a job that failed
                code = traceback.format_exc()
            if code not in (0, None):
                self.failed.append((job, stage, generation, index, code))
