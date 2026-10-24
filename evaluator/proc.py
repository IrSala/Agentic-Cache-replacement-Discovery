"""Run a subprocess that can be killed completely on timeout. Standard library only."""

import os
import signal
import subprocess
import time


def run_killable(cmd, cwd, timeout, stdout=subprocess.PIPE):
    """Run cmd in its own process group and kill the whole group on timeout.

    subprocess.run(timeout=) kills only the direct child (a compiler driver but
    not cc1plus, a shell but not what it started), and preexec_fn is unsafe
    in threads (OpenEvolve calls evaluate() in one), so this uses
    Popen(process_group=0) and os.killpg.

    stdout is a pipe by default, or an open file to write the output to.
    Returns (exit code, or None after a timeout; output text, "" when stdout
    is a file; seconds).
    """
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=stdout, stderr=subprocess.STDOUT, text=True,
                            errors="replace", process_group=0)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", time.monotonic() - start
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out or "", time.monotonic() - start
