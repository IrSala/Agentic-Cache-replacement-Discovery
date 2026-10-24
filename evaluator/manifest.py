"""Experiment manifests (docs/experiment-contract.md §2). Standard library only.

A manifest is one hand-written JSON file, configs/experiments/<id>.json,
immutable once a campaign uses it. Trace paths are relative to $AFAR_ASSETS,
so one manifest works for every user. Each trace is pinned by sha256.

    python -m evaluator.manifest --check configs/experiments/smoke-v1.json [--verify-traces]
    python -m evaluator.manifest --selftest
"""

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# key -> expected type, for each section of a contract v0 manifest
SCHEMA = {
    "contract_version": int,
    "experiment_id": str,
    "purpose": str,
    "champsim": {"repo": str, "commit": str, "base_config": str, "target_cache": str, "module_name": str},
    "simulation": {"warmup_instructions": int, "simulation_instructions": int},
    "baseline": {"policy": str},
    "traces": list,
    "limits": {"build_timeout_s": int, "run_timeout_s": int, "max_source_bytes": int},
    "fitness": {"name": str, "version": int},
}
TRACE_KEYS = {"id": str, "path": str, "sha256": str}
SHA256 = re.compile(r"[0-9a-f]{64}")


def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def assets_dir():
    """$AFAR_ASSETS, which env/site.sh sets; trace paths are relative to it."""
    value = os.environ.get("AFAR_ASSETS")
    if not value:
        raise RuntimeError("AFAR_ASSETS is not set; source env/site.sh first")
    return Path(value)


def problems(manifest):
    """Return a list of problems with a manifest's structure (empty if valid)."""
    found = []

    def check(section, spec, data):
        for key, kind in spec.items():
            where = f"{section}.{key}" if section else key
            if key not in data:
                found.append(f"missing {where}")
            elif isinstance(kind, dict):
                if not isinstance(data[key], dict):
                    found.append(f"{where} must be an object")
                else:
                    check(where, kind, data[key])
            elif not isinstance(data[key], kind) or (kind is int and isinstance(data[key], bool)):
                found.append(f"{where} must be {kind.__name__}")
        extra = set(data) - set(spec)
        if extra:
            found.append(f"unknown key(s) in {section or 'manifest'}: {', '.join(sorted(extra))}")

    check("", SCHEMA, manifest)
    traces = manifest.get("traces")
    if isinstance(traces, list):
        if not traces:
            found.append("traces must not be empty")
        ids = [t.get("id") for t in traces if isinstance(t, dict)]
        if len(ids) != len(set(ids)):
            found.append("trace ids must be unique")
        for i, trace in enumerate(traces):
            if not isinstance(trace, dict):
                found.append(f"traces[{i}] must be an object")
                continue
            check(f"traces[{i}]", TRACE_KEYS, trace)
            if isinstance(trace.get("sha256"), str) and not SHA256.fullmatch(trace["sha256"]):
                found.append(f"traces[{i}].sha256 must be 64 lowercase hex characters")
            if isinstance(trace.get("path"), str) and os.path.isabs(trace["path"]):
                found.append(f"traces[{i}].path must be relative to $AFAR_ASSETS")
    sim = manifest.get("simulation")
    if isinstance(sim, dict) and any(isinstance(v, int) and v <= 0 for v in sim.values()):
        found.append("simulation instruction counts must be positive")
    return found


class Manifest:
    """A loaded, validated manifest. sha256 is of the file's exact bytes."""

    def __init__(self, path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.data = json.loads(raw)
        found = problems(self.data)
        if found:
            raise ValueError(f"{self.path}: " + "; ".join(found))

    def __getitem__(self, key):
        return self.data[key]

    @property
    def experiment_id(self):
        return self.data["experiment_id"]

    def trace_path(self, trace):
        return assets_dir() / trace["path"]

    def check_traces(self, verify_hashes=False):
        """Return problems with the traces on disk (missing, or a sha256 mismatch)."""
        found = []
        for trace in self.data["traces"]:
            path = self.trace_path(trace)
            if not path.is_file():
                found.append(f"trace {trace['id']}: {path} not found")
            elif verify_hashes and file_sha256(path) != trace["sha256"]:
                found.append(f"trace {trace['id']}: sha256 mismatch for {path}")
        return found


def selftest():
    good = json.loads((ROOT / "configs" / "experiments" / "smoke-tiny-v1.json").read_text())
    cases = [("smoke-tiny-v1 as written", good, None)]
    bad = copy.deepcopy(good); del bad["fitness"]
    cases.append(("missing section", bad, "missing fitness"))
    bad = copy.deepcopy(good); bad["simulation"]["warmup_instructions"] = "10000"
    cases.append(("wrong type", bad, "simulation.warmup_instructions must be int"))
    bad = copy.deepcopy(good); bad["traces"][0]["path"] = "/abs/trace.xz"
    cases.append(("absolute trace path", bad, "relative to $AFAR_ASSETS"))
    bad = copy.deepcopy(good); bad["traces"][0]["sha256"] = "abc"
    cases.append(("short sha256", bad, "64 lowercase hex"))
    bad = copy.deepcopy(good); bad["seed"] = 1
    cases.append(("unknown key", bad, "unknown key(s) in manifest: seed"))
    bad = copy.deepcopy(good); bad["traces"].append(copy.deepcopy(bad["traces"][0]))
    cases.append(("duplicate trace id", bad, "unique"))

    failures = 0
    for name, data, needle in cases:
        found = problems(data)
        ok = (not found) if needle is None else any(needle in p for p in found)
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<26} {found[0] if found else 'valid'}")

    # The sha256 is of the exact file bytes.
    with tempfile.TemporaryDirectory() as work:
        path = Path(work, "m.json")
        path.write_text(json.dumps(good))
        ok = Manifest(path).sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {'sha256 of the file bytes':<26}")

    print("manifest selftest passed" if failures == 0 else f"manifest selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", metavar="FILE", help="validate a manifest and check its traces exist")
    parser.add_argument("--verify-traces", action="store_true", help="also check each trace's sha256 (reads every trace)")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if not args.check:
        parser.error("use --check FILE or --selftest")
    manifest = Manifest(args.check)
    found = manifest.check_traces(verify_hashes=args.verify_traces)
    for p in found:
        print(f"  FAIL  {p}")
    if found:
        return 1
    hashed = " (sha256 verified)" if args.verify_traces else ""
    print(f"{manifest.experiment_id}: valid, {len(manifest['traces'])} trace(s) present{hashed}; manifest sha256 {manifest.sha256[:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
