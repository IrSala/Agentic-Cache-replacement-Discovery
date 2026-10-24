"""Static checks on a candidate header, before anything is compiled. Standard library only.

A candidate is rejected as invalid_candidate (docs/experiment-contract.md §1) when:
  - it exceeds the size limit
  - it doesn't have exactly one EVOLVE-BLOCK, or its text outside the block
    differs from the seed's (trailing whitespace ignored)
  - the class head is missing
  - its EVOLVE-BLOCK uses a forbidden call or construct (below)

The forbidden list is defense in depth, not a sandbox: candidate code still
runs with the submitting user's permissions (docs/roadmap.md). It skips names
with ordinary standard-library uses (std::remove, std::bind, ...) to avoid
rejecting sound candidates.

    python -m evaluator.static_checks --selftest
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seeds" / "evolved_repl.h"
START, END = "// EVOLVE-BLOCK-START", "// EVOLVE-BLOCK-END"
CLASS_HEAD = "class evolved_repl : public champsim::modules::replacement"

# Calls a replacement policy never needs: processes, files, environment, raw memory, signals.
_CALLS = ("system", "popen", "pclose", "fork", "vfork", "execl", "execlp", "execle", "execv", "execvp",
          "execvpe", "execve", "posix_spawn", "posix_spawnp", "kill", "unlink", "rmdir", "chdir", "fopen",
          "freopen", "creat", "socket", "dlopen", "syscall", "getenv", "setenv", "putenv", "ptrace", "mmap",
          "signal", "sigaction")
FORBIDDEN = [
    (re.compile(r"(?<![\w.>])(?:std::|::)?(" + "|".join(_CALLS) + r")\s*\("), "a call to {0}()"),
    (re.compile(r"\b(asm|__asm__|__asm)\b"), "inline assembly ({0})"),
    (re.compile(r"\b__attribute__\b"), "__attribute__"),
    (re.compile(r"\bstd::(filesystem|ofstream|ifstream|fstream|thread|jthread|async)\b"), "std::{0}"),
    (re.compile(r"^\s*#", re.M), "a preprocessor directive inside the EVOLVE-BLOCK"),
]


def split_block(text):
    """Return (before, block, after) as line lists, split at the marker lines, or None."""
    if text.count("EVOLVE-BLOCK-START") != 1 or text.count("EVOLVE-BLOCK-END") != 1:
        return None
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == START]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None
    s, e = starts[0], ends[0]
    return lines[: s + 1], lines[s + 1 : e], lines[e:]


def fixed_text(lines):
    """The fixed text as compared: trailing whitespace on each line and at the end ignored."""
    return "\n".join(line.rstrip() for line in lines).rstrip()


def _strip_comments(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    return re.sub(r"//[^\n]*", "", code)


def forbidden_use(block_lines):
    """The first forbidden construct in the EVOLVE-BLOCK's code (comments ignored), or None."""
    code = _strip_comments("\n".join(block_lines))
    for pattern, message in FORBIDDEN:
        m = pattern.search(code)
        if m:
            return message.format(m.group(1) if m.groups() else m.group(0))
    return None


def static_problem(code, seed, max_bytes):
    """None if the candidate passes the static checks; otherwise the reason."""
    size = len(code.encode())
    if size > max_bytes:
        return f"file is {size} bytes; the limit is {max_bytes}"
    parts = split_block(code)
    if parts is None:
        return f"needs exactly one '{START}' line followed by one '{END}' line"
    before, block, after = parts
    seed_before, _, seed_after = split_block(seed)
    if fixed_text(before) != fixed_text(seed_before):
        return "text before the EVOLVE-BLOCK differs from the seed; only the block may change"
    if fixed_text(after) != fixed_text(seed_after):
        return "text after the EVOLVE-BLOCK differs from the seed; only the block may change"
    if CLASS_HEAD not in code:
        return f"'{CLASS_HEAD}' is missing"
    found = forbidden_use(block)
    if found:
        return f"the EVOLVE-BLOCK uses {found}, which a replacement policy may not use"
    return None


def selftest():
    seed = SEED.read_text()
    body = "    // Find the way whose last use cycle is most distant\n"
    inject = lambda line: seed.replace(body, f"    {line}\n" + body)
    cases = [
        ("seed", seed, None),
        ("std::remove and std::bind are allowed", inject("auto unused = std::bind(std::remove<int*, int>, nullptr, nullptr, 0); (void)unused;"), None),
        ("forbidden word only in a comment", inject("// never call system() here"), None),
        ("std::system call", inject('std::system("true");'), "system()"),
        ("plain fopen call", inject('auto f = fopen("x", "w"); (void)f;'), "fopen()"),
        ("std::ofstream", inject('std::ofstream out("x");'), "std::ofstream"),
        ("inline assembly", inject('asm("nop");'), "inline assembly"),
        ("__attribute__", inject("__attribute__((unused)) int z = 0;"), "__attribute__"),
        ("#include inside the block", inject("#include <cstdlib>"), "preprocessor"),
        ("member named execute() is allowed", inject("auto execute = [](){ return 0; }; (void)execute();"), None),
        ("changed include", seed.replace("#include <deque>\n", "#include <list>\n"), "before the EVOLVE-BLOCK"),
        ("END marker removed", seed.replace(END, ""), "exactly one"),
        ("over the size limit", seed.replace(body, "// " + "x" * 40000 + "\n"), "limit"),
    ]
    failures = 0
    for name, text, needle in cases:
        problem = static_problem(text, seed, 32768)
        ok = (problem is None) if needle is None else (problem is not None and needle in problem)
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<40} {problem or 'passes'}")
    print("static_checks selftest passed" if failures == 0 else f"static_checks selftest FAILED: {failures} case(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if not args.selftest:
        parser.error("use --selftest")
    sys.exit(selftest())
