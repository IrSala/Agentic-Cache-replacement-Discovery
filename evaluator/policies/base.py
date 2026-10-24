"""The evaluator-policy interface. Standard library only.

A policy turns one candidate into an Outcome in four stages, which
Policy.evaluate() runs in order and stops at the first failure:

    static_check(candidate, ctx) -> reason or None        (invalid_candidate)
    build(candidate, ctx)        -> Build                  (compile_failed, timeout)
    run(candidate, build, ctx)   -> list of result records (runtime_failed, timeout, ...)
    score(candidate, build, records, ctx) -> Outcome

fail(status, reason, ...) turns an early failure into an Outcome; policies
that write result records write one per trace there (contract §3).
Infrastructure problems raise InfraError instead of scoring the candidate.
"""

from dataclasses import dataclass, field
from pathlib import Path


class InfraError(RuntimeError):
    """The environment is broken; not the candidate's fault (contract: infra_error)."""


@dataclass
class Candidate:
    candidate_id: str
    sha256: str
    code: str = None      # the candidate header's text, or None for a built-in module
    builtin: str = None   # a built-in ChampSim replacement module (baselines and comparisons)


@dataclass
class Context:
    manifest: object = None         # evaluator.manifest.Manifest
    out_dir: Path = None            # where records and raw/ logs go (None: write nothing)
    workspace: Path = None          # node-local scratch, deleted afterwards
    cxx: str = "g++"
    jobs: int = 1
    campaign_id: str = None         # both None for baselines
    generation: int = None
    baselines: dict = None          # trace id -> baseline record, for scoring


@dataclass
class Build:
    status: str                     # "completed", "compile_failed" or "timeout"
    reason: str = ""
    seconds: float = 0.0
    output: str = ""
    binary: Path = None
    details: dict = field(default_factory=dict)


@dataclass
class Outcome:
    status: str                     # the contract status for the candidate as a whole
    fitness: float = 0.0
    reason: str = ""
    metrics: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    records: list = field(default_factory=list)


class Policy:
    name = None

    def static_check(self, candidate, ctx):
        return None

    def build(self, candidate, ctx):
        raise NotImplementedError

    def run(self, candidate, build, ctx):
        return []

    def score(self, candidate, build, records, ctx):
        raise NotImplementedError

    def fail(self, status, reason, candidate, ctx, build=None):
        return Outcome(status=status, reason=reason, artifacts={"status": status, "reason": reason})

    def evaluate(self, candidate, ctx):
        problem = self.static_check(candidate, ctx)
        if problem:
            return self.fail("invalid_candidate", problem, candidate, ctx)
        built = self.build(candidate, ctx)
        if built.status != "completed":
            return self.fail(built.status, built.reason, candidate, ctx, build=built)
        records = self.run(candidate, built, ctx)
        return self.score(candidate, built, records, ctx)
