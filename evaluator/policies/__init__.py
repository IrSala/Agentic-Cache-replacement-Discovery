"""Evaluator policies, found by name (docs/roadmap.md, "research levers").

    from evaluator.policies import get_policy
    policy = get_policy("champsim_ipc")
"""

from evaluator.policies.base import Candidate, Context, InfraError, Outcome, Policy  # noqa: F401


def get_policy(name):
    # Imported here so each policy loads only when it's used.
    if name == "compile_only":
        from evaluator.policies.compile_only import CompileOnly
        return CompileOnly()
    if name == "champsim_ipc":
        from evaluator.policies.champsim_ipc import ChampsimIPC
        return ChampsimIPC()
    raise ValueError(f"unknown evaluator policy {name!r} (known: compile_only, champsim_ipc)")
