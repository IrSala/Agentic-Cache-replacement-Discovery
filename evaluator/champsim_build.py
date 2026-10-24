"""Build and run ChampSim outside the shared source tree. Standard library only.

ChampSim's Makefile must run inside a ChampSim tree and includes
_configuration.mk from the current directory before any -I directory (GNU
make), and external/ChampSim already holds the baseline's. So every build
gets its own small copy of the tree (3.8 MB: no .git, vcpkg or build
outputs; vcpkg_installed is symlinked). Copies are safe to use concurrently
and never touch the shared tree (BUILD_PLAN 2.2).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from evaluator.proc import run_killable

ROOT = Path(__file__).resolve().parent.parent
CHAMPSIM = ROOT / "external" / "ChampSim"
BASE_CONFIG = ROOT / "configs" / "champsim" / "baseline.json"
VENV_BIN = ROOT / ".venv" / "evaluator" / "bin"   # config.sh and the Makefile's $(shell python3 …)
EXCLUDE = {".git", ".github", "vcpkg", "vcpkg_installed", ".csconfig", "bin", "_configuration.mk", "absolute.options"}
STATIC_LDFLAGS = "-static-libstdc++ -static-libgcc"


@dataclass
class StepResult:
    ok: bool
    seconds: float
    exit_code: int = None   # None after a timeout
    log: str = ""
    compiled: list = field(default_factory=list)   # objects make recompiled (builds only)


def _env():
    return dict(os.environ, PATH=f"{VENV_BIN}:{os.environ.get('PATH', '/usr/bin:/bin')}")


def prepare_tree(dest):
    """Copy the ChampSim source tree to dest (which must not exist) and return it."""
    dest = Path(dest)
    dest.mkdir(parents=True)
    for entry in CHAMPSIM.iterdir():
        if entry.name in EXCLUDE:
            continue
        if entry.is_dir():
            shutil.copytree(entry, dest / entry.name, symlinks=True,
                            ignore=shutil.ignore_patterns("*.o", "*.d", "__pycache__"))
        else:
            shutil.copy2(entry, dest / entry.name)
    (dest / "vcpkg_installed").symlink_to(CHAMPSIM / "vcpkg_installed")
    return dest


def copy_tree(src, dest):
    """Copy a built tree, keeping timestamps so make reuses its objects (like cp -a)."""
    shutil.copytree(src, dest, symlinks=True)
    return Path(dest)


def candidate_config(module, executable_name="champsim"):
    """The baseline configuration with the LLC replacement policy set to module."""
    cfg = json.loads(BASE_CONFIG.read_text())
    cfg["LLC"]["replacement"] = module
    cfg["executable_name"] = executable_name
    return cfg


def configure(tree, cfg, bindir, replacement_dir=None, timeout=300):
    """Run config.sh in tree; generated files stay inside tree (and bindir)."""
    tree = Path(tree)
    cfg_path = tree / "afar_config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2))
    cmd = [str(VENV_BIN / "python"), "config.sh", "--bindir", str(bindir)]
    if replacement_dir:
        cmd += ["--replacement-dir", str(replacement_dir)]
    cmd.append(str(cfg_path))
    code, out, seconds = run_killable(cmd, tree, timeout, env=_env())
    return StepResult(ok=code == 0, seconds=seconds, exit_code=code, log=out)


def has_static_libstdcxx(cxx):
    """True if cxx can link libstdc++ statically (g++ prints a full path when libstdc++.a exists)."""
    out = subprocess.run([cxx, "-print-file-name=libstdc++.a"], capture_output=True, text=True).stdout.strip()
    return os.path.isabs(out) and os.path.exists(out)


def build(tree, cxx, jobs, timeout, static=None):
    """make in tree with cxx. static=None links libstdc++ statically when the compiler can."""
    static = has_static_libstdcxx(cxx) if static is None else static
    cmd = ["make", f"-j{jobs}", f"CXX={cxx}"]
    if static:
        cmd.append(f"LDFLAGS={STATIC_LDFLAGS}")
    code, out, seconds = run_killable(cmd, tree, timeout, env=_env())
    compiled = sorted({Path(o).name for o in re.findall(r" -c -o (\S+\.o)\b", out)})
    return StepResult(ok=code == 0, seconds=seconds, exit_code=code, log=out, compiled=compiled)


def module_sha256(name):
    """Identity of a built-in replacement module: sha256 over its files, in name order."""
    h = hashlib.sha256()
    for path in sorted((CHAMPSIM / "replacement" / name).iterdir()):
        if path.is_file():
            h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def run(binary, trace, warmup, sim, json_out, log_path, timeout, cwd, limits=None, env=None):
    """Run ChampSim on one trace, optionally under prlimit (limits: as_bytes, fsize_bytes, cpu_s)."""
    cmd = [str(binary), "--warmup-instructions", str(warmup), "--simulation-instructions", str(sim),
           "--json", str(json_out), str(trace)]
    if limits and shutil.which("prlimit"):
        cmd = ["prlimit", f"--as={limits['as_bytes']}", f"--fsize={limits['fsize_bytes']}",
               f"--cpu={limits['cpu_s']}", "--"] + cmd
    with open(log_path, "w") as log:
        code, _, seconds = run_killable(cmd, cwd, timeout, stdout=log, env=env)
    return StepResult(ok=code == 0, seconds=seconds, exit_code=code, log=Path(log_path).read_text(errors="replace"))
