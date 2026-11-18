"""Run a subprocess that can be killed completely on timeout. Standard library only."""

import os
import signal
import subprocess
import time

CANCEL_POLL_S = 1.0


def run_killable(cmd, cwd, timeout, stdout=subprocess.PIPE, env=None, cancel=None):
    """Run cmd in its own process group and kill the whole group on timeout.

    subprocess.run(timeout=) kills only the direct child (a compiler driver but
    not cc1plus, a shell but not what it started), and preexec_fn is unsafe
    in threads (OpenEvolve calls evaluate() in one), so this uses
    Popen(process_group=0) and os.killpg.

    stdout is a pipe by default, or an open file to write the output to.
    env replaces the environment when given (default: inherit).
    cancel, a threading.Event, kills the group early once set (checked every
    CANCEL_POLL_S); the result then looks like a timeout's, so callers that
    pass one check it.
    Returns (exit code, or None after a timeout or cancel; output text, ""
    when stdout is a file; seconds).
    """
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=stdout, stderr=subprocess.STDOUT, text=True,
                            errors="replace", process_group=0, env=env)
    deadline = None if timeout is None else start + timeout
    while True:
        wait = None if deadline is None else max(0.0, deadline - time.monotonic())
        if cancel is not None:
            wait = CANCEL_POLL_S if wait is None else min(wait, CANCEL_POLL_S)
        try:
            out, _ = proc.communicate(timeout=wait)
            return proc.returncode, out or "", time.monotonic() - start
        except subprocess.TimeoutExpired:
            if (deadline is not None and time.monotonic() >= deadline) or (cancel is not None and cancel.is_set()):
                os.killpg(proc.pid, signal.SIGKILL)
                out, _ = proc.communicate()
                return None, out or "", time.monotonic() - start
