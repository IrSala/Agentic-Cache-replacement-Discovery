"""Replay campaigns' recorded prompts against another model, and compare the replies (R8).

A staged campaign records every prompt it sent, with the reply and its token usage, in
generations/<g>/generation.json. Sending the same prompts to another model gives a paired
comparison: the same prompts, and only the model differs. Campaign records are only read.

    python -m controller.llm_replay run --source crc2-evolve-c1:10 --source crc2-evolve-c2-screen-test:2 \\
        --profile qwen3.6-27b-fp8-v1 --api-base URL [--out DIR] [--vllm-log FILE]
    python -m controller.llm_replay check DIR [--json] [--jobs N]
    python -m controller.llm_replay --selftest

run    Each source generation is one batch, sent at once as a generation job sends it
       (oe_adapter.generate), with the source campaign's temperature and max_tokens, which must
       equal the profile's. A request may take up to REPLAY_TIMEOUT_S with no retries, so its
       real latency (queue plus generation) is measured; check counts those over the profile's
       own timeout, which a campaign would have cancelled and resent. One file per batch, so a
       rerun with --out skips the batches already written. Output, unless --out:
       $AFAR_RESULTS/llm-replay/<profile>-<job>/ (run.json, batches/<campaign>-g<N>.json).
check  Puts each prompt's replayed reply and the source's recorded reply through the same
       funnel (FUNNEL). Its compile step is the compile_only policy: static checks and a
       syntax-only compile, champsim_ipc's first build stage. Then the paired counts with an
       exact McNemar test, the timing, and vLLM's own log. It checks itself against the source
       campaigns too: the recorded replies must get the campaigns' recorded rejections and
       compile verdicts. Writes check.json. Needs the compiler (make llm-replay-check).
"""

import argparse
import concurrent.futures
import difflib
import hashlib
import json
import math
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from controller import campaign as cp  # noqa: E402
from controller import oe_adapter as oe  # noqa: E402
from controller import runspec  # noqa: E402
from controller.diversity import evolve_block  # noqa: E402
from evaluator import results  # noqa: E402
from evaluator.policies import Candidate, Context  # noqa: E402
from evaluator.policies.compile_only import SEED, CompileOnly, syntax_check  # noqa: E402

REPLAY_VERSION = 1
REPLAY_TIMEOUT_S = 3600          # per request, with no retries: long enough to measure, not to hide
# The funnel's steps, in order; a reply is useful when it passes all of them.
FUNNEL = (
    ("answered", "the request returned a reply"),
    ("code found", "the rewrite parser found code in it"),
    ("not the parent", "not identical to the parent's code (the campaign rejects these)"),
    ("not cosmetic", "differs from the parent beyond comments and whitespace"),
    ("within length", "within max_code_length"),
    ("compiles", "passes the static checks and the syntax-only compile"),
)
COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
FENCE = re.compile(r"```([^\n`]*)\n")
STATS = re.compile(r"(\d\d-\d\d \d\d:\d\d:\d\d).*Avg prompt throughput: ([\d.]+) tokens/s, Avg generation "
                   r"throughput: ([\d.]+) tokens/s, Running: (\d+) reqs, Waiting: (\d+) reqs, "
                   r"GPU KV cache usage: ([\d.]+)%")
# Rejections a generation job records (controller/campaign.py generate), by the funnel step that makes them.
REJECTIONS = (("answered", "LLM generation failed"), ("code found", "no code found in the reply"),
              ("not the parent", "identical to the parent program"), ("within length", "longer than max_code_length"))
COMPILE_FAILURES = ("invalid_candidate", "compile_failed")
COMPILE_PASSES = ("completed", "runtime_failed")   # got past the build, so past the syntax check too


class ReplayError(Exception):
    pass


def replay_root():
    return cp.campaigns_root().parent / "llm-replay"


def parse_source(text):
    """'crc2-evolve-c1:10' or 'crc2-evolve-c1:1-9' -> [(campaign id, generation), ...]."""
    cid, sep, gens = text.strip().rpartition(":")
    first, dash, last = gens.partition("-")
    try:
        lo = int(first)
        hi = int(last) if dash else lo
    except ValueError:
        lo = hi = None
    if not sep or not cid or lo is None or lo < 1 or hi < lo:
        raise ReplayError(f"bad source {text!r}: use <campaign>:<generation> or <campaign>:<first>-<last>")
    return [(cid, g) for g in range(lo, hi + 1)]


def batch_name(cid, g):
    return f"{cid}-g{g}"


def load_profile(name):
    path = runspec.CONFIGS / "models" / f"{name}.env"
    if not path.exists():
        raise ReplayError(f"no model profile {path}")
    prof = runspec.read_env([runspec.VLLM_ENV, path], runspec.VLLM_KEYS + runspec.MODEL_KEYS)
    missing = [k for k in ("MODEL_ID", "MODEL_REVISION", "LLM_TEMPERATURE", "LLM_MAX_TOKENS", "LLM_TIMEOUT_S")
               if k not in prof]
    if missing:
        raise ReplayError(f"model profile {name} lacks {', '.join(missing)}")
    return prof


def load_generation(cid, g):
    c = cp.Campaign(cid)
    record = cp.read_json(c.gen_dir(g) / "generation.json")
    if record is None:
        raise ReplayError(f"{cid}: generation {g} has no generation.json")
    if not record["entries"] or any("prompt" not in e for e in record["entries"]):
        raise ReplayError(f"{cid}: generation {g} has entries without a recorded prompt")
    return c, record


def settings_problems(config, prof):
    problems = []
    for m in config.llm.models:
        if m.temperature != float(prof["LLM_TEMPERATURE"]):
            problems.append(f"temperature {m.temperature}, but the profile's is {prof['LLM_TEMPERATURE']}")
        if m.max_tokens != int(prof["LLM_MAX_TOKENS"]):
            problems.append(f"max_tokens {m.max_tokens}, but the profile's is {prof['LLM_MAX_TOKENS']}")
    return problems


def git_commit():
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    head = git("rev-parse", "--short", "HEAD")
    if head.returncode != 0:
        return None
    return head.stdout.strip() + ("" if git("diff", "--quiet", "HEAD").returncode == 0 else "-dirty")


def server_metrics(api_base):
    """vLLM's Prometheus counters (summed over labels), or None if the server has no /metrics."""
    url = api_base.rstrip("/").removesuffix("/v1") + "/metrics"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ.get('AFAR_LLM_API_KEY', '')}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read().decode(errors="replace")
    except Exception:  # the stand-in server and stopped servers have none; metrics are optional
        return None
    counters = {}
    for line in text.splitlines():
        if not line.startswith("vllm:"):
            continue
        name, _, value = line.rpartition(" ")
        name = name.split("{", 1)[0]
        if name.endswith("_total"):
            try:
                counters[name] = counters.get(name, 0.0) + float(value)
            except ValueError:
                pass
    return counters


# --- run ---------------------------------------------------------------------

def run(sources, profile, api_base, out=None, vllm_log=None, client=None):
    """Send every source batch not yet written; {batch name: "written" or "skipped"}."""
    prof = load_profile(profile)
    job = os.environ.get("SLURM_JOB_ID") or "local"
    out = Path(out) if out else replay_root() / f"{profile}-{job}"
    pairs = [pair for s in sources for part in s.split(",") if part.strip() for pair in parse_source(part)]
    if len(set(pairs)) != len(pairs):
        raise ReplayError("a source generation is listed twice")
    plans = []
    for cid, g in pairs:                      # check every source before sending anything
        c, record = load_generation(cid, g)
        config = oe.load_config(c.path / "resolved" / "config.yaml", api_base)
        problems = settings_problems(config, prof)
        if problems:
            raise ReplayError(f"{cid} generation {g} was generated with " + "; ".join(problems)
                              + ", so its replies don't compare")
        for m in config.llm.models:
            m.timeout, m.retries = REPLAY_TIMEOUT_S, 0
        plans.append((cid, g, record, config))
    fixed = {"replay_version": REPLAY_VERSION, "profile": profile, "server": prof.get("LLM_SERVER"),
             "model_id": prof["MODEL_ID"], "model_revision": prof["MODEL_REVISION"],
             "vllm_serve_args": prof.get("VLLM_SERVE_ARGS"), "vllm_image": prof.get("VLLM_IMAGE"),
             "settings": {"temperature": float(prof["LLM_TEMPERATURE"]), "max_tokens": int(prof["LLM_MAX_TOKENS"]),
                          "timeout_s": REPLAY_TIMEOUT_S, "retries": 0,
                          "profile_timeout_s": int(prof["LLM_TIMEOUT_S"])}}
    meta = cp.read_json(out / "run.json")
    if meta is not None:
        changed = [k for k in fixed if meta.get(k) != fixed[k]]
        if changed:
            raise ReplayError(f"{out} was replayed with a different {', '.join(changed)}; use a new --out")
    else:
        meta = dict(fixed, jobs=[])
    meta["jobs"].append({"job": job, "node": socket.gethostname(), "gpu": os.environ.get("AFAR_GPU_NAME"),
                         "commit": git_commit(), "vllm_log": str(Path(vllm_log).resolve()) if vllm_log else None,
                         "started_at": cp.now(), "sources": [batch_name(cid, g) for cid, g in pairs]})
    results.write_atomic(out / "run.json", meta)
    print(f"replay {profile} ({prof['MODEL_ID']}) -> {out}", flush=True)   # flushed: a job's log shows progress

    done = {}
    for cid, g, record, config in plans:
        name = batch_name(cid, g)
        path = out / "batches" / f"{name}.json"
        if path.exists():
            print(f"{name}: already replayed", flush=True)
            done[name] = "skipped"
            continue
        prompts = [e["prompt"] for e in record["entries"]]
        before = server_metrics(api_base)
        started, started_at = time.time(), cp.now()
        replies = oe.generate(config, prompts, client)
        finished = time.time()
        after = server_metrics(api_base)
        results.write_atomic(path, {
            "campaign": cid, "generation": g, "job": job, "requests": len(prompts),
            "seconds": round(finished - started, 2), "started_at": started_at, "finished_at": cp.now(),
            "started_epoch": round(started, 3), "finished_epoch": round(finished, 3),
            "source_llm_seconds": record.get("llm_seconds"), "source_host": record.get("host"),
            "source_gpu": record.get("gpu"), "metrics_before": before, "metrics_after": after,
            "replies": [{"index": e["index"], "response": r["response"], "token_usage": r["token_usage"],
                         "seconds": r["seconds"], "error": r["error"]} for e, r in zip(record["entries"], replies)]})
        failed = sum(r["error"] is not None for r in replies)
        print(f"{name}: {len(prompts)} replies in {finished - started:.1f}s (the source's: "
              f"{record.get('llm_seconds')}s), {failed} failed, slowest {max(r['seconds'] for r in replies):.1f}s",
              flush=True)
        done[name] = "written"
    return done


# --- check -------------------------------------------------------------------

def normalized(code):
    return re.sub(r"\s+", "", COMMENTS.sub("", code))


def fence(response):
    if not response:
        return "(no reply)"
    m = FENCE.search(response)
    return (m.group(1).strip() or "(bare)") if m else "(none)"


def changed_lines(code, parent):
    a, b = evolve_block(parent), evolve_block(code)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")


def assess(response, error, parent, max_length, language):
    """One reply's funnel flags, without the compile step (compile_status is filled in later)."""
    row = {"answered": error is None and response is not None, "error": error,
           "think": bool(response) and "<think>" in response, "fence": fence(response)}
    code = oe.parse_rewrite(response, language) if row["answered"] else None
    row["code_found"] = bool(code)
    if code:
        row.update(sha=hashlib.sha256(code.encode()).hexdigest(), bytes=len(code), identical=code == parent,
                   cosmetic=normalized(code) == normalized(parent), too_long=len(code) > max_length,
                   changed_lines=changed_lines(code, parent), parent_block_lines=len(evolve_block(parent)))
        row["_code"] = code
    return row


def stopped_at(row):
    """The funnel step a reply failed, or None if it passed them all."""
    if not row["answered"]:
        return "answered"
    if not row["code_found"]:
        return "code found"
    if row["identical"]:
        return "not the parent"
    if row["cosmetic"]:
        return "not cosmetic"
    if row["too_long"]:
        return "within length"
    if row.get("compile_status") != "completed":
        return "compiles"
    return None


def expected_rejection(row):
    """What a generation job would have recorded as `rejected` for this reply, or None."""
    for step, reason in REJECTIONS:
        if stopped_at(dict(row, cosmetic=False, compile_status="completed")) == step:
            return reason
    return None


def compile_all(codes, cxx, jobs):
    """{sha: (status, reason, error)} for each distinct code, through the compile_only policy. error is
    gcc's first error line, without the file and position, or None. The policy keeps only the output's
    tail, which a long template error can push the first error out of, so a failure is compiled again
    for its whole output."""
    policy, ctx = CompileOnly(), Context(cxx=cxx)

    def one(item):
        sha, code = item
        outcome = policy.evaluate(Candidate(candidate_id=sha[:16], sha256=sha, code=code), ctx)
        reason = (outcome.reason or "").strip().splitlines()
        output = syntax_check(code, cxx).output if outcome.status == "compile_failed" else ""
        errors = [line.split(" error: ", 1)[1] for line in output.splitlines() if " error: " in line]
        return sha, (outcome.status, reason[0][:200] if reason else "", errors[0][:300] if errors else None)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return dict(pool.map(one, codes.items()))


def recorded_result(c, cand):
    for f in sorted(c.path.glob(f"generations/*/results/{cand}.json")):
        r = cp.read_json(f)
        reason = (r.get("summary") or {}).get("reason") or ""
        return r["status"], reason.strip().splitlines()[0][:200] if reason.strip() else ""
    return None, None


def quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))] if xs else None


def mcnemar(b, c):
    """Exact two-sided McNemar p-value for b and c discordant pairs (a binomial test at 1/2)."""
    n, k = b + c, min(b, c)
    return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def log_stats(paths, batches):
    """Per batch name: peak running and waiting requests, peak KV cache use and generation throughput,
    from vLLM's 10-second stats lines within the batch's time window. The window ends 11 s after the
    batch (vLLM logs every 10 s) or when the next batch starts, whichever is first, so back-to-back
    batches don't share stats lines. (vLLM's V1 engine logs no preemptions; they come from /metrics.)"""
    lines = []
    for p in paths:
        if p and Path(p).exists():
            lines += Path(p).read_text(errors="replace").splitlines()
    starts = [b["started_epoch"] for b in batches]
    out = {}
    for b in batches:
        year = time.localtime(b["started_epoch"]).tm_year
        lo = b["started_epoch"] - 1
        hi = min([b["finished_epoch"] + 11] + [s for s in starts if s > b["started_epoch"]])

        def epoch(stamp):
            return time.mktime(time.strptime(f"{year}-{stamp}", "%Y-%m-%d %H:%M:%S"))
        stats = [m for m in map(STATS.search, lines) if m and lo <= epoch(m.group(1)) <= hi]
        out[batch_name(b["campaign"], b["generation"])] = None if not stats else {
            "stats_lines": len(stats), "peak_running": max(int(m.group(4)) for m in stats),
            "peak_waiting": max(int(m.group(5)) for m in stats),
            "peak_kv_cache_pct": max(float(m.group(6)) for m in stats),
            "peak_generation_tokens_s": max(float(m.group(3)) for m in stats)}
    return out


def metric_deltas(b):
    before, after = b.get("metrics_before"), b.get("metrics_after")
    if not before or not after:
        return None
    return {k.removeprefix("vllm:"): round(after[k] - before.get(k, 0.0), 1) for k in sorted(after)
            if after[k] != before.get(k, 0.0)}


def resolve_dir(ref):
    path = Path(ref)
    return path if path.exists() or "/" in str(ref) else replay_root() / str(ref)


def check(ref, cxx="g++", jobs=4):
    out = resolve_dir(ref)
    meta = cp.read_json(out / "run.json")
    if meta is None:
        raise ReplayError(f"{out} has no run.json")
    batches = sorted((cp.read_json(f) for f in (out / "batches").glob("*.json")),
                     key=lambda b: (b["campaign"], b["generation"]))
    if not batches:
        raise ReplayError(f"{out} has no replayed batches yet")
    seed_sha = hashlib.sha256(SEED.read_bytes()).hexdigest()
    campaigns, rows = {}, []
    for b in batches:
        cid, g = b["campaign"], b["generation"]
        if cid not in campaigns:
            c = cp.Campaign(cid)
            if c.meta["seed"]["sha256"] != seed_sha:
                raise ReplayError(f"{cid}'s seed isn't {SEED.relative_to(ROOT)}, which the static checks compare with")
            config = oe.load_config(c.path / "resolved" / "config.yaml")
            campaigns[cid] = (c, config.max_code_length, config.language)
        c, max_length, language = campaigns[cid]
        _, record = load_generation(cid, g)
        programs = c.checkpoint(g - 1) / "programs"
        for entry, reply in zip(record["entries"], b["replies"]):
            if entry["index"] != reply["index"]:
                raise ReplayError(f"{batch_name(cid, g)}: replies are out of order")
            parent = cp.read_json(programs / f"{entry['parent_id']}.json")
            if parent is None:
                raise ReplayError(f"{cid}: generation {g}'s parent {entry['parent_id']} isn't in {programs}")
            failed = entry.get("rejected", "")
            source = assess(entry["response"], failed if failed.startswith("LLM generation failed") else None,
                            parent["code"], max_length, language)
            replay = assess(reply["response"], reply["error"], parent["code"], max_length, language)
            source.update(seconds=entry.get("llm_seconds"), token_usage=entry.get("token_usage"),
                          recorded_rejection=entry.get("rejected"), candidate_id=entry.get("candidate_id"))
            replay.update(seconds=reply["seconds"], token_usage=reply["token_usage"])
            if entry.get("candidate_id"):
                source["recorded_status"], source["recorded_reason"] = recorded_result(c, entry["candidate_id"])
            rows.append({"batch": batch_name(cid, g), "index": entry["index"], "replay": replay, "source": source})

    codes = {r[side]["sha"]: r[side]["_code"] for r in rows for side in ("replay", "source")
             if r[side]["code_found"] and not r[side]["identical"] and not r[side]["too_long"]}
    t0 = time.monotonic()
    verdicts = compile_all(codes, cxx, jobs)
    compile_seconds = time.monotonic() - t0
    for r in rows:
        for side in ("replay", "source"):
            row = r[side]
            row.pop("_code", None)
            if row.get("sha") in verdicts:
                row["compile_status"], row["compile_reason"], row["compile_error"] = verdicts[row["sha"]]
            row["stopped_at"] = stopped_at(row)
            row["useful"] = row["stopped_at"] is None

    report = {"run": meta, "out": str(out), "compiler": cxx, "compiled": len(codes),
              "compile_seconds": round(compile_seconds, 1),
              "models": {"replay": meta["model_id"],
                         "source": sorted({campaigns[b["campaign"]][0].meta["model"]["model_id"] for b in batches})},
              "funnel": {}, "paired": {}, "batches": [], "agreement": {}, "entries": rows}
    for side in ("replay", "source"):
        side_rows = [r[side] for r in rows]
        steps, left = [("replies", len(side_rows))], len(side_rows)
        for step, _ in FUNNEL:
            left -= sum(row["stopped_at"] == step for row in side_rows)
            steps.append((step, left))
        unique = sum(len({r[side]["sha"] for r in rows if r["batch"] == name and r[side]["useful"]})
                     for name in dict.fromkeys(r["batch"] for r in rows))
        steps.append(("unique in their batch", unique))
        usage = [row["token_usage"] for row in side_rows if row.get("token_usage")]
        changed = [row["changed_lines"] / max(1, row["parent_block_lines"]) for row in side_rows if row["useful"]]
        report["funnel"][side] = {
            "steps": steps, "compile_failures": dict(sorted(
                {s: sum(row.get("compile_status") == s for row in side_rows)
                 for s in {row.get("compile_status") for row in side_rows} - {None, "completed"}}.items())),
            "fences": dict(sorted({f: sum(row["fence"] == f for row in side_rows)
                                   for f in {row["fence"] for row in side_rows}}.items(),
                                  key=lambda kv: str(kv[0]))),
            "think": sum(row["think"] for row in side_rows),
            "reply_tokens_median": statistics.median(u["completion_tokens"] for u in usage) if usage else None,
            "prompt_tokens_median": statistics.median(u["prompt_tokens"] for u in usage) if usage else None,
            "at_max_tokens": sum(u["completion_tokens"] >= meta["settings"]["max_tokens"] for u in usage),
            "changed_fraction_median": round(statistics.median(changed), 3) if changed else None}
    both = sum(r["replay"]["useful"] and r["source"]["useful"] for r in rows)
    only_replay = sum(r["replay"]["useful"] and not r["source"]["useful"] for r in rows)
    only_source = sum(r["source"]["useful"] and not r["replay"]["useful"] for r in rows)
    report["paired"] = {"both": both, "replay_only": only_replay, "source_only": only_source,
                        "neither": len(rows) - both - only_replay - only_source,
                        "mcnemar_p": float(f"{mcnemar(only_replay, only_source):.3g}")}

    logs = log_stats([j.get("vllm_log") for j in meta["jobs"]], batches)
    limit = meta["settings"]["profile_timeout_s"]
    for b in batches:
        name = batch_name(b["campaign"], b["generation"])
        lat = [r["seconds"] for r in b["replies"]]
        report["batches"].append({
            "batch": name, "requests": b["requests"], "seconds": b["seconds"],
            "source_seconds": b.get("source_llm_seconds"), "job": b.get("job"),
            "latency_p50": quantile(lat, 0.5), "latency_p90": quantile(lat, 0.9), "latency_max": max(lat),
            "over_profile_timeout": sum(s > limit for s in lat), "failed": sum(r["error"] is not None for r in b["replies"]),
            "vllm_log": logs.get(name), "vllm_metrics": metric_deltas(b)})

    rejections = [(r, r["source"]) for r in rows]
    bad_rejections = [{"batch": r["batch"], "index": r["index"], "recorded": s["recorded_rejection"],
                       "expected": expected_rejection(s)} for r, s in rejections
                      if (s["recorded_rejection"] or None) != expected_rejection(s)
                      and not (s["recorded_rejection"] or "").startswith(expected_rejection(s) or "\0")]
    verdict_rows = [(r, r["source"]) for r in rows if r["source"].get("recorded_status") in COMPILE_FAILURES + COMPILE_PASSES
                    and r["source"].get("compile_status")]
    bad_verdicts = [{"batch": r["batch"], "index": r["index"], "candidate_id": s["candidate_id"],
                     "recorded": s["recorded_status"], "recorded_reason": s["recorded_reason"],
                     "syntax_check": s["compile_status"], "reason": s.get("compile_reason")}
                    for r, s in verdict_rows
                    if (s["recorded_status"] if s["recorded_status"] in COMPILE_FAILURES else "completed")
                    != s["compile_status"]]
    report["agreement"] = {"rejections": [len(rejections) - len(bad_rejections), len(rejections)],
                           "rejection_mismatches": bad_rejections,
                           "compile_verdicts": [len(verdict_rows) - len(bad_verdicts), len(verdict_rows)],
                           "compile_mismatches": bad_verdicts}
    results.write_atomic(out / "check.json", report)
    return report


def pct(n, d):
    return f"{n} ({100 * n / d:.0f}%)" if d else str(n)


def print_check(rep):
    rows = rep["entries"]
    n = len(rows)
    print(f"replay: {rep['run']['profile']} ({rep['models']['replay']}); source: {', '.join(rep['models']['source'])}")
    print(f"{n} prompts in {len(rep['batches'])} batches, {rep['out']}")
    print(f"compile step: {rep['compiled']} distinct programs with {rep['compiler']} in {rep['compile_seconds']}s\n")
    fr, fs = rep["funnel"]["replay"], rep["funnel"]["source"]
    print(f"{'funnel (cumulative)':<26}{'replay':>14}{'source':>14}")
    for (step, a), (_, b) in zip(fr["steps"], fs["steps"]):
        print(f"{step:<26}{pct(a, n):>14}{pct(b, n):>14}")
    p = rep["paired"]
    print(f"\npaired, useful: both {p['both']}, replay only {p['replay_only']}, source only {p['source_only']}, "
          f"neither {p['neither']}; exact McNemar p = {p['mcnemar_p']}")
    for label, key in (("compile failures", "compile_failures"), ("code fences", "fences"),
                       ("reply tokens, median", "reply_tokens_median"), ("prompt tokens, median", "prompt_tokens_median"),
                       ("replies at max_tokens", "at_max_tokens"), ("replies with <think>", "think"),
                       ("block lines changed, median share (useful)", "changed_fraction_median")):
        print(f"{label + ':':<44} replay {fr[key]}; source {fs[key]}")
    limit = rep["run"]["settings"]["profile_timeout_s"]
    print(f"\n{'batch':<34}{'n':>4}{'s':>8}{'source s':>10}{'p50':>7}{'p90':>7}{'max':>7}{f'>{limit}s':>7}"
          f"{'fail':>5}{'run/wait':>10}{'KV %':>7}{'gen tok/s':>10}{'preempt':>8}")
    for b in rep["batches"]:
        lg = b["vllm_log"] or {}
        rw = f"{lg['peak_running']}/{lg['peak_waiting']}" if lg else "-"
        preempt = (b["vllm_metrics"] or {}).get("num_preemptions_total", 0 if b["vllm_metrics"] else "-")
        print(f"{b['batch']:<34}{b['requests']:>4}{b['seconds']:>8.1f}{b['source_seconds'] or 0:>10.1f}"
              f"{b['latency_p50']:>7.1f}{b['latency_p90']:>7.1f}{b['latency_max']:>7.1f}{b['over_profile_timeout']:>7}"
              f"{b['failed']:>5}{rw:>10}{lg.get('peak_kv_cache_pct', '-'):>7}"
              f"{lg.get('peak_generation_tokens_s', '-'):>10}{preempt:>8}")
        if b["vllm_metrics"]:
            print(f"    vLLM counters: {b['vllm_metrics']}")
    a = rep["agreement"]
    print(f"\nagreement with the source campaigns: rejections {a['rejections'][0]}/{a['rejections'][1]}, "
          f"compile verdicts {a['compile_verdicts'][0]}/{a['compile_verdicts'][1]}")
    for m in a["rejection_mismatches"]:
        print(f"  rejection {m['batch']}[{m['index']}]: recorded {m['recorded']!r}, expected {m['expected']!r}")
    for m in a["compile_mismatches"]:
        print(f"  verdict {m['batch']}[{m['index']}] {m['candidate_id']}: recorded {m['recorded']} "
              f"({m['recorded_reason']}), syntax check {m['syntax_check']} ({m['reason']})")


# --- selftest -----------------------------------------------------------------

def selftest():
    """A stand-in model on a made-up campaign: every funnel step, the paired counts, the agreement check,
    resume, and refused settings. Needs the compiler (AFAR_CXX)."""
    seed = SEED.read_text()
    body = "// Find the way whose last use cycle is most distant"
    variants = {
        "valid_a": seed.replace("std::min_element", "std::max_element"),
        "valid_b": seed.replace("EVOLVE-BLOCK-START\n", "EVOLVE-BLOCK-START\n  static constexpr int afar_selftest_b = 1;\n"),
        "cosmetic": seed.replace(body, "// reworded, and   spaced  differently"),
        "broken": seed.replace("uint64_t cycle = 0;", "uint64_t cycle = 0"),
    }
    assert all(v != seed for v in variants.values()), "a selftest variant didn't change the seed"
    code = {k: v.strip() for k, v in variants.items()}
    parent = seed.strip()

    def wrap(text):
        return f"Here is the improved program:\n```cpp\n{text}\n```\n"
    prose = "I would evict the most recently used line instead, but I can't write the code."
    error = RuntimeError("stand-in timeout")
    # replay reply, recorded reply, its recorded rejection, its recorded status,
    # and where each must stop in the funnel (None: useful)
    cases = [
        (wrap(code["valid_a"]), wrap(parent), "identical to the parent program", None, None, "not the parent"),
        (wrap(parent), wrap(code["valid_a"]), None, "completed", "not the parent", None),
        (wrap(code["cosmetic"]), wrap(code["cosmetic"]), None, "completed", "not cosmetic", "not cosmetic"),
        (wrap(code["broken"]), wrap(code["broken"]), None, "compile_failed", "compiles", "compiles"),
        (prose, prose, None, "invalid_candidate", "compiles", "compiles"),
        ("", "", "no code found in the reply", None, "code found", "code found"),
        (error, wrap(code["valid_b"]), None, "completed", "answered", None),
        (wrap(code["valid_a"]), wrap(code["valid_a"]), None, "completed", None, None),
    ]
    failures = []

    def expect(ok, what):
        print(f"  {'ok  ' if ok else 'FAIL'}  {what}")
        if not ok:
            failures.append(what)

    with tempfile.TemporaryDirectory(prefix="afar-replay-selftest-") as tmp:
        os.environ["AFAR_RESULTS"] = tmp
        root = cp.campaigns_root()
        config_yaml = ("language: cpp\nmax_code_length: 40960\nllm:\n  api_base: http://127.0.0.1:0/v1\n"
                       "  api_key: ${AFAR_LLM_API_KEY}\n  models:\n  - name: afar-llm\n    weight: 1.0\n"
                       "  temperature: TEMP\n  max_tokens: 4096\n  timeout: 300\n")
        seed_sha = hashlib.sha256(SEED.read_bytes()).hexdigest()
        for cid, temp in (("replay-selftest", "0.7"), ("replay-selftest-hot", "1.0")):
            c = root / cid
            (c / "resolved").mkdir(parents=True)
            (c / "resolved" / "config.yaml").write_text(config_yaml.replace("TEMP", temp))
            results.write_atomic(c / "campaign.json", {
                "campaign_id": cid, "campaign": {"candidates_per_generation": len(cases)},
                "seed": {"path": "seeds/evolved_repl.h", "sha256": seed_sha},
                "model": {"model_id": "stand-in/recorded", "profile": "fake-v1"}})
            results.write_atomic(c / "checkpoints" / "checkpoint_0" / "programs" / "p0.json", {"id": "p0", "code": parent})
            entries = []
            for i, (_, recorded, rejected, status, _, _) in enumerate(cases):
                e = {"index": i, "iteration": i + 1, "parent_id": "p0", "llm_seconds": 1.0 + i,
                     "prompt": {"system": "selftest", "user": f"case {i}"}, "response": recorded,
                     "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}}
                if rejected:
                    e["rejected"] = rejected
                else:
                    cand = hashlib.sha256(oe.parse_rewrite(recorded, "cpp").encode()).hexdigest()[:16]
                    e["candidate_id"] = cand
                    results.write_atomic(c / "generations" / "1" / "results" / f"{cand}.json",
                                         {"candidate_id": cand, "status": status, "summary": {"reason": ""}})
                entries.append(e)
            results.write_atomic(c / "generations" / "1" / "generation.json",
                                 {"generation": 1, "llm_seconds": 9.0, "host": "selftest", "entries": entries})

        sent = []

        async def client(prompt):
            i = int(prompt["user"].split()[-1])
            sent.append(i)
            reply = cases[i][0]
            if isinstance(reply, Exception):
                raise reply
            return reply, {"prompt_tokens": 120, "completion_tokens": 4096 if i == 7 else 60}

        out = Path(tmp) / "replay"
        api = "http://127.0.0.1:9/v1"        # nothing listens: /metrics is optional
        done = run(["replay-selftest:1"], "fake-v1", api, out=out, client=client)
        expect(done == {"replay-selftest-g1": "written"} and sorted(sent) == list(range(len(cases))),
               "run sends every recorded prompt once and writes the batch")
        batch = cp.read_json(out / "batches" / "replay-selftest-g1.json")
        expect(batch["replies"][6]["error"] and "stand-in timeout" in batch["replies"][6]["error"],
               "a failed request is recorded with its error, not dropped")
        sent.clear()
        done = run(["replay-selftest:1"], "fake-v1", api, out=out, client=client)
        expect(done == {"replay-selftest-g1": "skipped"} and not sent, "a rerun skips the written batch")
        expect(len(cp.read_json(out / "run.json")["jobs"]) == 2, "run.json lists both jobs")
        for args, needle, what in ((["replay-selftest-hot:1"], "temperature", "settings that differ are refused"),
                                   (["replay-selftest:1,replay-selftest:1"], "twice", "a repeated source is refused"),
                                   (["replay-selftest:0"], "bad source", "generation 0 is refused"),
                                   (["replay-selftest:2"], "no generation.json", "a missing generation is refused")):
            try:
                run(args, "fake-v1", api, out=Path(tmp) / "refused", client=client)
                expect(False, what)
            except ReplayError as e:
                expect(needle in str(e), f"{what} ({e})")

        rep = check(out, cxx=os.environ.get("AFAR_CXX", "g++"), jobs=2)
        for i, (*_, want_replay, want_source) in enumerate(cases):
            got = rep["entries"][i]
            expect(got["replay"]["stopped_at"] == want_replay and got["source"]["stopped_at"] == want_source,
                   f"case {i}: replay stops at {got['replay']['stopped_at']}, source at {got['source']['stopped_at']}")
        steps = {side: dict(rep["funnel"][side]["steps"]) for side in ("replay", "source")}
        expect(steps["replay"]["compiles"] == 2 and steps["replay"]["unique in their batch"] == 1
               and steps["source"]["compiles"] == 3 and steps["source"]["unique in their batch"] == 2,
               f"funnel: replay {steps['replay']['compiles']} useful, 1 unique; source 3 useful, 2 unique")
        expect(rep["paired"] == {"both": 1, "replay_only": 1, "source_only": 2, "neither": 4, "mcnemar_p": 1.0},
               f"paired counts {rep['paired']}")
        expect(rep["agreement"]["rejections"] == [8, 8] and rep["agreement"]["compile_verdicts"] == [6, 6],
               f"agreement with the recorded campaign: {rep['agreement']['rejections']}, "
               f"{rep['agreement']['compile_verdicts']}")
        expect(rep["funnel"]["replay"]["at_max_tokens"] == 1 and rep["funnel"]["replay"]["fences"].get("cpp") == 5,
               "a reply at max_tokens is counted, and the code fences too")
        broken = [e["replay"] for e in rep["entries"] if e["replay"].get("compile_status") == "compile_failed"]
        passed = [e["replay"] for e in rep["entries"] if e["replay"].get("compile_status") == "completed"]
        expect(broken and all(b.get("compile_error") for b in broken)
               and all(p["compile_error"] is None for p in passed),
               f"a compile failure keeps gcc's first error ({broken[0].get('compile_error') if broken else None!r})")
        expect(rep["batches"][0]["over_profile_timeout"] == 0 and rep["batches"][0]["vllm_log"] is None,
               "timing without a vLLM log")
        expect((out / "check.json").exists() and "_code" not in json.dumps(rep["entries"]),
               "check.json is written, without the programs' text")
        expect(mcnemar(10, 0) < 0.002 and mcnemar(3, 3) == 1.0, "the McNemar test's p-values")
        print_check(rep)
    print("selftest passed" if not failures else f"selftest FAILED: {len(failures)} check(s)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--selftest", action="store_true")
    sub = parser.add_subparsers(dest="command")
    r = sub.add_parser("run", help="send source generations' prompts to a served model")
    r.add_argument("--source", action="append", required=True,
                   help="<campaign>:<generation> or <campaign>:<first>-<last>; repeat or comma-separate")
    r.add_argument("--profile", required=True, help="the model profile being served (configs/models/<name>.env)")
    r.add_argument("--api-base", required=True)
    r.add_argument("--out", help="output directory (default $AFAR_RESULTS/llm-replay/<profile>-<job>)")
    r.add_argument("--vllm-log", help="the server's log, for check's queue and cache figures")
    k = sub.add_parser("check", help="compare the replayed replies with the recorded ones")
    k.add_argument("dir", help="a replay directory, or its name under $AFAR_RESULTS/llm-replay")
    k.add_argument("--json", action="store_true")
    k.add_argument("--jobs", type=int, default=4, help="compiles at once (default 4)")
    k.add_argument("--cxx", default=os.environ.get("AFAR_CXX", "g++"))
    args = parser.parse_args()
    try:
        if args.selftest:
            return selftest()
        if args.command == "run":
            run(args.source, args.profile, args.api_base, args.out, args.vllm_log)
            return 0
        if args.command == "check":
            rep = check(args.dir, args.cxx, args.jobs)
            print(json.dumps(rep, indent=2, sort_keys=True) if args.json else "", end="")
            if not args.json:
                print_check(rep)
            return 0
    except (ReplayError, cp.CampaignError) as e:
        print(f"llm_replay: {e}", file=sys.stderr)
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
