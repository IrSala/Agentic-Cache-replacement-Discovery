"""The prebuilt base tree: ChampSim built once per (commit, compiler, base config). Standard library only.

A candidate changes only 2 of ChampSim's 41 objects (docs/champsim-evaluator.md,
M2 part A), so each candidate build copies this tree, timestamps kept, adds
its module and rebuilds just those 2: about 10 s instead of 53 s on HPC3.

It lives at $AFAR_ASSETS/champsim-prebuilt/<commit>-<compiler>-<hash>/tree, and
must stay at that path, unchanged, because copies' absolute.options point at
its inc/ directory. It's built in place under a lock (mkdir of its directory)
and becomes usable only once PREBUILT.json is written, so a crashed or
concurrent build is never used half-done.

    python -m evaluator.prebuilt --build [--cxx /usr/bin/g++] [--jobs 4]
    python -m evaluator.prebuilt --show
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path

from evaluator import champsim_build as cb
from evaluator import results
from evaluator.policies.base import InfraError
from evaluator.policies.compile_only import compiler_version

MARKER = "PREBUILT.json"


def key(cxx):
    """<champsim commit>-<compiler>-<hash of everything that affects the base objects>."""
    ver = compiler_version(cxx)
    m = re.search(r"(\d+\.\d+\.\d+)", ver)
    tag = f"gcc{m.group(1)}" if m else "cxx"
    h = hashlib.sha256()
    for part in (ver.encode(), cb.BASE_CONFIG.read_bytes(), (cb.CHAMPSIM / "global.options").read_bytes(),
                 (cb.CHAMPSIM / "module.options").read_bytes(), str(cb.has_static_libstdcxx(cxx)).encode()):
        h.update(part)
        h.update(b"\0")
    return f"{results.champsim_commit()[:7]}-{tag}-{h.hexdigest()[:8]}"


def location(cxx):
    assets = os.environ.get("AFAR_ASSETS")
    if not assets:
        raise InfraError("AFAR_ASSETS is not set; source env/site.sh first")
    return Path(assets) / "champsim-prebuilt" / key(cxx)


def find(cxx):
    """The prebuilt tree for cxx, or None if there's no complete one."""
    loc = location(cxx)
    return loc / "tree" if (loc / MARKER).exists() else None


def ensure(cxx, jobs, timeout=1800, wait_s=1800):
    """The prebuilt tree for cxx, building it first if needed.

    Returns None if it doesn't exist and can't be built here (AFAR_ASSETS not
    writable, e.g. a teammate's shared copy): callers then do a full build.
    """
    loc = location(cxx)
    if (loc / MARKER).exists():
        return loc / "tree"
    try:
        loc.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(loc)                        # the lock: whoever creates it builds
    except FileExistsError:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:   # someone else is building it
            if (loc / MARKER).exists():
                return loc / "tree"
            time.sleep(10)
        raise InfraError(f"{loc} has no {MARKER} after {wait_s}s: a build crashed or is stuck; remove the directory")
    except PermissionError:
        return None

    try:
        tree = cb.prepare_tree(loc / "tree")
        cfg = cb.configure(tree, cb.candidate_config("lru"), tree / "bin-prebuilt")
        if not cfg.ok:
            raise InfraError(f"config.sh failed for the prebuilt tree:\n{cfg.log[-2000:]}")
        built = cb.build(tree, cxx, jobs, timeout)
        if not built.ok:
            raise InfraError(f"the prebuilt tree didn't build with {cxx}:\n{built.log[-3000:]}")
        info = {"key": loc.name, "compiler": compiler_version(cxx), "cxx": cxx,
                "champsim_commit": results.champsim_commit(), "static_libstdcxx": cb.has_static_libstdcxx(cxx),
                "objects": len(built.compiled), "build_seconds": round(built.seconds, 2),
                "host": socket.gethostname(), "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
                "built_at": results.now()}
        results.write_atomic(loc / MARKER, info)
        return tree
    except BaseException:
        shutil.rmtree(loc, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cxx", default=os.environ.get("AFAR_CXX", "g++"))
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", "2")))
    parser.add_argument("--build", action="store_true", help="build the prebuilt tree if it doesn't exist")
    parser.add_argument("--force", action="store_true", help="with --build: remove an existing tree first")
    parser.add_argument("--show", action="store_true", help="print the key and whether the tree exists")
    args = parser.parse_args()
    loc = location(args.cxx)
    if args.build:
        if args.force and loc.exists():
            shutil.rmtree(loc)
        start = time.monotonic()
        tree = ensure(args.cxx, args.jobs)
        if tree is None:
            print(f"cannot build {loc}: {loc.parent} is not writable")
            return 1
        print(f"prebuilt tree ready in {time.monotonic() - start:.0f}s: {tree}")
    if args.show or not args.build:
        marker = loc / MARKER
        print(f"key: {loc.name}\nlocation: {loc}\n" +
              (json.dumps(json.loads(marker.read_text()), indent=2) if marker.exists() else "status: not built"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
