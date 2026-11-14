"""The duplicate screen (docs/levers/evaluation/duplicate-screen.md). Standard library only.

A campaign's run spec names one screening trace or several (evaluator.duplicate_screen.trace: a
trace id or a list of them). They run first; when their results match a behavior the campaign has
already measured on every trace, the candidate gets that behavior's scores and its other traces are
stopped or skipped.

trace_list(trace)   the setting as a list of trace ids
label(trace)        the setting for messages: "bzip2_183B + soplex_66B"
key(records)        the key of a completed screening record, or of a list of them in the setting's
                    order: each one's cycles and LLC misses, and the ChampSim commit and compiler
                    that produced them. One record's key is the same as a one-record list's.
lookup(index, recs) the behavior an index entry names for these records, or None (no entry, or an
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


def trace_list(trace):
    return [trace] if isinstance(trace, str) else list(trace)


def label(trace):
    return " + ".join(trace_list(trace))


def _key(record):
    if record.get("status") != "completed" or record.get("cycles") is None:
        return None
    misses = llc_misses((record.get("logs") or {}).get("champsim_json"))
    if misses is None:
        return None
    env = record.get("env") or {}
    return f"{record['cycles']}:{misses}:{env.get('champsim_commit')}:{env.get('cxx_version')}"


def key(records):
    """The screening key of a result record or a list of them, or None unless every one completed
    with readable output."""
    parts = [_key(r) for r in ([records] if isinstance(records, dict) else records)]
    return "|".join(parts) if parts and None not in parts else None


def lookup(index, records):
    k = key(records)
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
