"""Evaluator policies, found by name (docs/roadmap.md, "research levers").

    from evaluator.policies import get_policy
    policy = get_policy("champsim_ipc")

A new policy is a module with a Policy subclass (evaluator/policies/base.py),
added to REGISTRY. Each class declares what it needs from a run:
needs_baselines (it scores against the manifest's LRU baselines) and
needs_simulation (it runs the manifest's traces).
"""

import importlib

from evaluator.policies.base import Candidate, Context, InfraError, Outcome, Policy  # noqa: F401

# name -> "module:Class", imported only when the policy is used
REGISTRY = {
    "compile_only": "evaluator.policies.compile_only:CompileOnly",
    "champsim_ipc": "evaluator.policies.champsim_ipc:ChampsimIPC",
}


def policy_class(name):
    if name not in REGISTRY:
        raise ValueError(f"unknown evaluator policy {name!r} (known: {', '.join(sorted(REGISTRY))})")
    module, cls = REGISTRY[name].split(":")
    return getattr(importlib.import_module(module), cls)


def get_policy(name):
    return policy_class(name)()
