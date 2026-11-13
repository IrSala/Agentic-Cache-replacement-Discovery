"""The duplicate screen (docs/levers/evaluation/duplicate-screen.md). Standard library only.

A campaign's run spec may name one screening trace (evaluator.duplicate_screen). That trace runs
first; when its result matches a behavior the campaign has already measured on every trace, the
candidate gets that behavior's scores and its other traces are stopped or skipped.

key(record)        a completed screening-trace record's key: its cycles and LLC misses, and the
                   ChampSim commit and compiler that produced them
lookup(index, rec) the behavior an index entry names for this record, or None (no entry, or an
                   ambiguous one)
merge(index, found) add one generation's fully evaluated behaviors to an index. A key two
                   different behaviors share becomes ambiguous and is never screened again.

The index maps key -> {"candidate", "generation", "cycles": [per trace, manifest order]} or
AMBIGUOUS. The controller builds it after each generation (controller/campaign.py).
"""

import json
from pathlib import Path

AMBIGUOUS = "ambiguous"


def llc_misses(json_path):
    """Every LLC miss in the measured region of a ChampSim --json file (None if it can't be read)."""
    try:
        llc = json.loads(Path(json_path).read_text())[0]["roi"]["LLC"]
    except (OSError, TypeError, ValueError, KeyError, IndexError):
        return None
    return sum(sum(v["miss"]) for v in llc.values() if isinstance(v, dict) and "miss" in v)


def key(record):
    """The screening key of a result record, or None unless it completed with readable output."""
    if record.get("status") != "completed" or record.get("cycles") is None:
        return None
    misses = llc_misses((record.get("logs") or {}).get("champsim_json"))
    if misses is None:
        return None
    env = record.get("env") or {}
    return f"{record['cycles']}:{misses}:{env.get('champsim_commit')}:{env.get('cxx_version')}"


def lookup(index, record):
    k = key(record)
    entry = index.get(k) if k else None
    return None if entry == AMBIGUOUS else entry


def merge(index, found):
    """found: key -> {tuple of cycles: entry} for one generation's behaviors. Updates index in place."""
    for k, vectors in found.items():
        known = index.get(k)
        if known == AMBIGUOUS or len(vectors) > 1 or (known is not None and tuple(known["cycles"]) not in vectors):
            index[k] = AMBIGUOUS
        elif known is None:
            index[k] = next(iter(vectors.values()))
    return index
