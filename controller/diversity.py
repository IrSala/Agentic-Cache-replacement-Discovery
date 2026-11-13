"""Behavioral diversity of a staged campaign (docs/campaigns.md, section 9): how many different
policies a campaign found, judged by what they did on its traces rather than how their code reads.
It reads files only, so it works from either cluster.

    python -m controller.diversity CAMPAIGN [--json] [--thresholds 0.001,0.01]
    python -m controller.diversity CAMPAIGN --screen [TRACE] [--cpus N] [--json]

ChampSim is deterministic, so a candidate's cycle counts on the campaign's traces fingerprint its
behavior there: candidates with identical cycles on every trace made the same replacement
decisions. Their LLC miss counts are a cross-check and must group candidates the same way. Only
candidates with every trace completed count; rejected replies and failures have no behavior.

diversity    richness (distinct behaviors); Hill numbers of order 1 and 2 over the behaviors'
             candidate counts (the effective number of behaviors, as ecology counts species);
             richness after merging behaviors whose cycles differ by less than a threshold on every
             trace (complete linkage on the largest per-trace |log| cycle ratio); the Vendi score
             (a Gaussian kernel on log cycles, its scale the first threshold).
generations  per generation: replies, exact copies of the parent (rejected), candidates scored,
             new behaviors, candidates repeating an earlier behavior, replies whose changed code
             behaved exactly like the parent, behaviors so far, and the distinct behaviors in the
             population OpenEvolve kept (the generation's checkpoint).
islands      replies, candidates scored and behaviors per island, and behaviors no other island found.
good         behaviors above LRU (fitness > 1) and within 1% of the campaign's best.
traces       the behaviors each trace separates alone; over subsets of k traces (all of them, or
             MAX_SUBSETS sampled), the fewest, median and most separated; and a greedy smallest
             set of traces that separates every behavior, preferring short traces.
code         code distance for pairs of candidates with the same behavior and with different
             ones: 1 - difflib's line ratio over the EVOLVE-BLOCK, and OpenEvolve 0.4.0's
             _fast_code_diversity; and their Spearman correlation with behavior distance.
groups       the largest behaviors: candidates, fitness, first generation, islands, members.

--screen replays the duplicate screen on the campaign's records instead
(docs/levers/evaluation/duplicate-screen.md): each new candidate, in order, is screened on one
trace against the behaviors earlier generations completed. A match would reuse that behavior's
scores; a wrong match is a candidate whose screening result matched but whose other traces
differed or failed. Every trace is tried as the screen; the detail is for TRACE (default: the
trace with no wrong matches that saves the most). CPU time is modeled at --cpus per candidate
(default: the campaign's), from the records' run times, in the evaluator's longest-first order
with the screening trace started first.
"""

import argparse
import difflib
import hashlib
import itertools
import json
import math
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from controller import campaign as cp  # noqa: E402
from controller import runspec  # noqa: E402
from controller.report import makespan, trace_records  # noqa: E402
from evaluator import evaluate_candidate as ec  # noqa: E402
from evaluator import screen as screen_mod  # noqa: E402
from evaluator.policies.champsim_ipc import run_order  # noqa: E402

DEFAULT_THRESHOLDS = (0.001, 0.01)
GOOD_WITHIN = 0.01        # "good": above LRU and within 1% of the campaign's best fitness
MAX_SUBSETS = 2000        # trace subsets of one size; sampled (fixed seed) beyond this
MAX_CODE_PAIRS = 20000    # candidate pairs for the code comparison; sampled (fixed seed) beyond this
TOP_GROUPS = 8
COPY = "identical to the parent program"


# --- fingerprints --------------------------------------------------------------

def llc_misses(rec):
    """Every LLC miss in the measured region, from the run's ChampSim JSON (None if it's gone)."""
    return screen_mod.llc_misses((rec.get("logs") or {}).get("champsim_json"))


def fingerprint(evals, cand, traces):
    """{cycles, misses, run_s} per trace in manifest order, or None unless every trace completed.
    A candidate the duplicate screen matched to a behavior is that behavior (its "screened_from")."""
    summary = cp.read_json(evals / cand / "summary.json", {}) or {}
    if ec.screened(summary):
        fp = fingerprint(evals, summary["screen"]["matched"], traces)
        return fp and {**fp, "screened_from": summary["screen"]["matched"]}
    recs = trace_records(evals, cand)
    if any(recs.get(t, {}).get("status") != "completed" or recs[t].get("cycles") is None for t in traces):
        return None
    misses = tuple(llc_misses(recs[t]) for t in traces)
    return {"cycles": tuple(recs[t]["cycles"] for t in traces),
            "misses": None if None in misses else misses,
            "run_s": [recs[t].get("run_seconds") for t in traces],
            "fitness": (cp.read_json(evals / cand / "summary.json", {}) or {}).get("fitness")}


def evolve_block(code):
    """The lines between the EVOLVE-BLOCK markers (the whole file if there are none)."""
    lines = code.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if "EVOLVE-BLOCK-START" in l)
        end = next(i for i, l in enumerate(lines) if "EVOLVE-BLOCK-END" in l)
        return lines[start + 1:end]
    except StopIteration:
        return lines


def openevolve_diversity(code1, code2):
    """OpenEvolve 0.4.0's ProgramDatabase._fast_code_diversity, which feeds its diversity feature."""
    if code1 == code2:
        return 0.0
    return (abs(len(code1) - len(code2)) * 0.1 + abs(code1.count("\n") - code2.count("\n")) * 10
            + len(set(code1).symmetric_difference(set(code2))) * 0.5)


# --- measures ------------------------------------------------------------------

def distance(a, b):
    """Behavior distance: the largest per-trace |log| ratio of two cycle vectors (0.01 = 1%)."""
    return max(abs(math.log(x / y)) for x, y in zip(a, b))


def hill(counts, q):
    """Hill number of order q: the effective number of behaviors given how many candidates each has."""
    n = sum(counts)
    p = [c / n for c in counts]
    if q == 1:
        return math.exp(-sum(x * math.log(x) for x in p))
    return sum(x ** q for x in p) ** (1 / (1 - q))


def complete_linkage(vectors, threshold):
    """Clusters (lists of indexes) in which every pair is closer than threshold."""
    import numpy as np
    n = len(vectors)
    logs = np.log(np.asarray(vectors, dtype=float))
    if n <= 500:
        d = np.abs(logs[:, None, :] - logs[None, :, :]).max(axis=2)
    else:           # row by row, to bound memory
        d = np.vstack([np.abs(logs - row).max(axis=1) for row in logs])
    np.fill_diagonal(d, np.inf)
    clusters = {i: [i] for i in range(n)}
    while len(clusters) > 1:
        i, j = np.unravel_index(np.argmin(d), d.shape)
        if d[i, j] >= threshold:
            break
        i, j = min(i, j), max(i, j)
        d[i, :] = d[:, i] = np.maximum(d[i, :], d[j, :])
        d[i, i] = np.inf
        d[j, :] = d[:, j] = np.inf
        clusters[i] += clusters.pop(j)
    return list(clusters.values())


def vendi(vectors, counts, scale):
    """Vendi score of the candidates: exp(entropy) of the eigenvalues of their similarity matrix /
    n, with k(x, y) = exp(-|log x - log y|^2 / 2 scale^2). Computed on the distinct behaviors
    weighted by their candidate counts, which has the same nonzero eigenvalues as the full matrix."""
    import numpy as np
    x = np.log(np.asarray(vectors, dtype=float))
    sq = (x * x).sum(1)
    k = np.exp(-np.maximum(sq[:, None] + sq[None, :] - 2 * x @ x.T, 0) / (2 * scale ** 2))
    w = np.sqrt(np.asarray(counts, dtype=float) / sum(counts))
    eig = np.linalg.eigvalsh(w[:, None] * k * w[None, :])
    eig = eig[eig > 1e-12]
    return float(np.exp(-(eig * np.log(eig)).sum()))


def ranks(xs):
    order = sorted(range(len(xs)), key=xs.__getitem__)
    r, i = [0.0] * len(xs), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    try:
        return round(statistics.correlation(ranks(a), ranks(b)), 3)
    except statistics.StatisticsError:      # fewer than 2 pairs, or a constant
        return None


def separation(vectors, traces, run_s):
    """What each trace and each subset of traces tells apart."""
    n_t, total = len(traces), len(vectors)
    rng = random.Random(0)
    per_trace = [{"trace": t, "behaviors": len({v[i] for v in vectors}),
                  "median_run_s": round(statistics.median(run_s[t])) if run_s.get(t) else None}
                 for i, t in enumerate(traces)]
    subsets = []
    for k in range(1, n_t + 1):
        if math.comb(n_t, k) <= MAX_SUBSETS:
            combos = list(itertools.combinations(range(n_t), k))
        else:
            combos = [tuple(sorted(rng.sample(range(n_t), k))) for _ in range(MAX_SUBSETS)]
        seen = sorted(len({tuple(v[i] for i in s) for v in vectors}) for s in combos)
        subsets.append({"k": k, "subsets": len(combos), "min": seen[0], "median": seen[len(seen) // 2], "max": seen[-1]})
    chosen = []
    while len({tuple(v[i] for i in chosen) for v in vectors}) < total:
        chosen.append(max((i for i in range(n_t) if i not in chosen),
                          key=lambda i: (len({tuple(v[j] for j in chosen + [i]) for v in vectors}),
                                         -(per_trace[i]["median_run_s"] or 0))))
    return {"per_trace": per_trace, "subsets": subsets,
            "greedy_set": [traces[i] for i in chosen],
            "greedy_set_run_s": sum(per_trace[i]["median_run_s"] or 0 for i in chosen)}


# --- the campaign ---------------------------------------------------------------

def checkpoints(c, gens):
    """OpenEvolve program id -> candidate id, and per generation the candidates its checkpoint kept, by island."""
    prog, kept = {}, {}
    for g in [0] + gens:
        d = c.checkpoint(g)
        if not d.exists():
            continue
        for f in (d / "programs").glob("*.json"):
            p = cp.read_json(f)
            prog[p["id"]] = hashlib.sha256(p["code"].encode()).hexdigest()[:16]
        meta = cp.read_json(d / "metadata.json", {}) or {}
        kept[g] = [[prog.get(pid) for pid in island] for island in meta.get("islands", [])]
    return prog, kept


def diversity(c, thresholds=DEFAULT_THRESHOLDS):
    manifest = c.manifest()
    traces = [t["id"] for t in manifest["traces"]]
    evals = ec.results_root() / "evaluations" / manifest.experiment_id / c.meta["evaluator"]["policy"]
    gens = sorted(int(p.name) for p in (c.path / "generations").glob("*") if p.name.isdigit())
    prog, kept = checkpoints(c, gens)
    fp_cache = {}

    def fp(cand):
        if cand and cand not in fp_cache:
            fp_cache[cand] = fingerprint(evals, cand, traces)
        return fp_cache.get(cand)

    # Every candidate once, at its first appearance.
    entries = {g: (cp.read_json(c.gen_dir(g) / "generation.json", {}) or {}).get("entries", []) for g in gens}
    cands, code = {}, {}
    for g in gens:
        for e in entries[g]:
            cand = e.get("candidate_id")
            if cand and cand not in cands:
                cands[cand] = {"generation": g, "island": e.get("island")}
                path = c.gen_dir(g) / "candidates" / f"{cand}.h"
                code[cand] = path.read_text() if path.exists() else None
    scored = {k: v for k, v in cands.items() if fp(k)}
    by_behavior = {}
    for cand in scored:
        by_behavior.setdefault(fp(cand)["cycles"], []).append(cand)
    behaviors = list(by_behavior)
    behavior_of = {cand: b for b, members in by_behavior.items() for cand in members}
    counts = [len(by_behavior[b]) for b in behaviors]

    # Cross-check: LLC misses must split the candidates exactly as cycles do.
    with_misses = [k for k in scored if fp(k)["misses"]]
    miss_groups = {}
    for cand in with_misses:
        miss_groups.setdefault(fp(cand)["misses"], set()).add(behavior_of[cand])
    cycle_groups = {}
    for cand in with_misses:
        cycle_groups.setdefault(behavior_of[cand], set()).add(fp(cand)["misses"])
    cross = {"candidates": len(with_misses), "miss_groups": len(miss_groups),
             "agree": all(len(v) == 1 for v in miss_groups.values()) and all(len(v) == 1 for v in cycle_groups.values())}

    div = {"candidates": len(scored), "behaviors": len(behaviors),
           "hill_1": round(hill(counts, 1), 2) if counts else 0, "hill_2": round(hill(counts, 2), 2) if counts else 0,
           "within": [], "vendi": None, "vendi_scale": thresholds[0] if thresholds else None}
    if behaviors:
        for th in thresholds:
            div["within"].append({"threshold": th, "behaviors": len(complete_linkage(behaviors, th))})
        if thresholds:
            div["vendi"] = round(vendi(behaviors, counts, thresholds[0]), 2)
    pairs = list(itertools.combinations(behaviors, 2))
    div["close_pairs"] = [{"threshold": th, "pairs": sum(distance(a, b) < th for a, b in pairs), "of": len(pairs)}
                          for th in thresholds]

    # Per generation.
    seen, rows = set(), []
    for g in gens:
        mine = [k for k in dict.fromkeys(e.get("candidate_id") for e in entries[g]) if k in scored]
        before = set(seen)
        like_parent = parent_known = 0
        for e in entries[g]:
            cand, parent = e.get("candidate_id"), fp(prog.get(e.get("parent_id")))
            if cand in scored and parent:
                parent_known += 1
                like_parent += fp(cand)["cycles"] == parent["cycles"]
        new = {behavior_of[k] for k in mine} - before
        seen |= {behavior_of[k] for k in mine}
        row = {"generation": g, "replies": len(entries[g]),
               "copies": sum(e.get("rejected") == COPY for e in entries[g]),
               "scored": len(mine), "new_behaviors": len(new),
               "repeats": sum(behavior_of[k] in before for k in mine),
               "like_parent": like_parent, "parent_known": parent_known, "behaviors_so_far": len(seen)}
        if g in kept:
            pop = [k for island in kept[g] for k in island]
            known = [fp(k)["cycles"] for k in pop if fp(k)]
            row.update(population=len(pop), population_scored=len(known), population_behaviors=len(set(known)))
        rows.append(row)

    # Per island.
    islands = []
    by_island = {}
    for cand, info in scored.items():
        by_island.setdefault(info["island"], set()).add(behavior_of[cand])
    for isl in sorted(by_island, key=lambda x: (x is None, x)):
        others = set().union(*(v for k, v in by_island.items() if k != isl))
        islands.append({"island": isl, "replies": sum(e.get("island") == isl for g in gens for e in entries[g]),
                        "scored": sum(info["island"] == isl for info in scored.values()),
                        "behaviors": len(by_island[isl]), "only_here": len(by_island[isl] - others)})

    # Good policies.
    fit = {b: fp(by_behavior[b][0])["fitness"] for b in behaviors}
    best = max((f for f in fit.values() if f is not None), default=None)
    good = [b for b in behaviors if fit[b] is not None and best and fit[b] > 1 and fit[b] >= best * (1 - GOOD_WITHIN)]
    good_rep = {"best": best, "above_lru": sum(1 for f in fit.values() if f and f > 1), "within": GOOD_WITHIN,
                "behaviors": len(good), "candidates": sum(len(by_behavior[b]) for b in good)}

    # Traces.
    run_s = {}
    for cand in scored:
        for t, s in zip(traces, fp(cand)["run_s"]):
            if s is not None:
                run_s.setdefault(t, []).append(s)
    sep = separation(behaviors, traces, run_s) if behaviors else None

    # Code vs behavior.
    with_code = [k for k in scored if code.get(k)]
    all_pairs = list(itertools.combinations(with_code, 2))
    if len(all_pairs) > MAX_CODE_PAIRS:
        all_pairs = random.Random(0).sample(all_pairs, MAX_CODE_PAIRS)
    blocks = {k: evolve_block(code[k]) for k in with_code}
    line_d, oe_d, beh_d, same = [], [], [], []
    for a, b in all_pairs:
        line_d.append(1 - difflib.SequenceMatcher(None, blocks[a], blocks[b], autojunk=False).ratio())
        oe_d.append(openevolve_diversity(code[a], code[b]))
        beh_d.append(distance(behavior_of[a], behavior_of[b]))
        same.append(behavior_of[a] == behavior_of[b])

    def med(xs):
        return round(statistics.median(xs), 3) if xs else None
    code_rep = {"pairs": len(all_pairs), "same_behavior_pairs": sum(same),
                "line_distance_same": med([d for d, s in zip(line_d, same) if s]),
                "line_distance_different": med([d for d, s in zip(line_d, same) if not s]),
                "openevolve_same": med([d for d, s in zip(oe_d, same) if s]),
                "openevolve_different": med([d for d, s in zip(oe_d, same) if not s]),
                "spearman_line": spearman(line_d, beh_d), "spearman_openevolve": spearman(oe_d, beh_d)}

    groups = []
    for b in sorted(behaviors, key=lambda b: -len(by_behavior[b]))[:TOP_GROUPS]:
        members = sorted(by_behavior[b], key=lambda k: (scored[k]["generation"], k))
        groups.append({"candidates": len(members), "fitness": fit[b],
                       "first_generation": scored[members[0]]["generation"],
                       "islands": sorted({scored[k]["island"] for k in members}, key=lambda x: (x is None, x)),
                       "members": members})

    return {"campaign": c.id, "experiment": manifest.experiment_id, "traces": len(traces),
            "candidates_seen": len(cands), "diversity": div, "cross_check": cross, "generations": rows,
            "islands": islands, "good": good_rep, "separation": sep, "code": code_rep, "groups": groups}


# --- the duplicate screen, replayed ---------------------------------------------

def screen_rows(generations, traces, order, screen, cpus):
    """The duplicate screen replayed on recorded results.

    generations: [(generation, [(candidate, {trace: {status, cycles, misses, run_s}})])], each
    candidate once, at its first appearance. order: the traces longest first. A candidate can
    match only behaviors that earlier generations completed on every trace, since a generation's
    candidates run at the same time. A screening result shared by two different behaviors is
    ambiguous and never screened. Times are the records' run seconds on `cpus` CPUs."""
    rest = [t for t in order if t != screen]
    index, out, wrong = {}, dict(candidates=0, matches=0, ambiguous=0, simulated_s=0.0, skipped_s=0.0,
                                 stopped_s=0.0, held_s=0.0, held_freed_s=0.0, held_added_s=0.0), []
    for g, cands in generations:
        found = {}
        for cand, recs in cands:
            if not recs:
                continue
            out["candidates"] += 1
            times = {t: (recs.get(t) or {}).get("run_s") or 0.0 for t in traces}
            complete = all((recs.get(t) or {}).get("status") == "completed" for t in traces)
            vector = tuple(recs[t]["cycles"] for t in traces) if complete else None
            full = makespan([times[t] for t in order], cpus)
            out["simulated_s"] += sum(times.values())
            out["held_s"] += cpus * full
            s = recs.get(screen) or {}
            key = (s.get("cycles"), s.get("misses")) if s.get("status") == "completed" else None
            hit = index.get(key) if key else None
            if hit == screen_mod.AMBIGUOUS:
                out["ambiguous"] += 1
                hit = None
            if hit is not None:
                out["matches"] += 1
                hit = tuple(hit["cycles"])
                if vector != hit:
                    wrong.append({"generation": g, "candidate": cand,
                                  "why": "failed on another trace" if vector is None else "differs on another trace",
                                  "distance": None if vector is None else round(distance(vector, hit), 6)})
                st = times[screen]
                stopped = sum(min(times[t], st) for t in rest[:max(0, cpus - 1)])
                out["skipped_s"] += sum(times.values()) - st
                out["stopped_s"] += stopped
                out["held_freed_s"] += cpus * (full - st)
            else:
                out["held_added_s"] += cpus * (makespan([times[screen]] + [times[t] for t in rest], cpus) - full)
            if vector is not None and key:
                found.setdefault(key, {})[vector] = {"candidate": cand, "generation": g, "cycles": vector}
        screen_mod.merge(index, found)
    hours = {k: round(v / 3600, 1) for k, v in out.items() if k.endswith("_s")}
    return {"trace": screen, "cpus": cpus, "candidates": out["candidates"], "matches": out["matches"],
            "wrong": len(wrong), "wrong_matches": wrong, "ambiguous": out["ambiguous"],
            "worst_wrong": max((w["distance"] if w["distance"] is not None else math.inf for w in wrong), default=None),
            "behaviors_indexed": sum(v != screen_mod.AMBIGUOUS for v in index.values()),
            "ambiguous_keys": sum(v == screen_mod.AMBIGUOUS for v in index.values()),
            **{k.replace("_s", "_cpu_hours"): v for k, v in hours.items()}}


def screen_replay(c, trace=None, cpus=None):
    manifest = c.manifest()
    traces = [t["id"] for t in manifest["traces"]]
    if trace and trace not in traces:
        raise ValueError(f"{trace} is not one of {manifest.experiment_id}'s traces")
    root = ec.results_root()
    evals = root / "evaluations" / manifest.experiment_id / c.meta["evaluator"]["policy"]
    baselines = ec.load_baselines(manifest, root / "baselines" / manifest.experiment_id)
    order = [t["id"] for t in run_order(manifest["traces"], baselines)]
    cpus = cpus or c.meta["evaluator"]["cpus_per_candidate"]
    gens = sorted(int(p.name) for p in (c.path / "generations").glob("*") if p.name.isdigit())
    seen, generations = set(), []
    for g in gens:
        cands = []
        for e in (cp.read_json(c.gen_dir(g) / "generation.json", {}) or {}).get("entries", []):
            cand = e.get("candidate_id")
            if cand and cand not in seen:
                seen.add(cand)
                # A candidate a screen already matched is replayed as the full evaluation it stood in for.
                summary = cp.read_json(evals / cand / "summary.json", {}) or {}
                source = summary["screen"]["matched"] if ec.screened(summary) else cand
                recs = {t: {"status": r.get("status"), "cycles": r.get("cycles"), "run_s": r.get("run_seconds"),
                            "misses": llc_misses(r) if r.get("status") == "completed" else None}
                        for t, r in trace_records(evals, source).items()}
                cands.append((cand, recs))
        generations.append((g, cands))
    per_trace = [screen_rows(generations, traces, order, t, cpus) for t in traces]
    clean = [r for r in per_trace if r["wrong"] == 0]
    best = max(clean, key=lambda r: r["held_freed_cpu_hours"] - r["held_added_cpu_hours"], default=None)
    detail = next(r for r in per_trace if r["trace"] == trace) if trace else best
    return {"campaign": c.id, "experiment": manifest.experiment_id, "traces": len(traces), "cpus": cpus,
            "per_trace": per_trace, "detail": detail}


def print_screen(r):
    print(f"duplicate screen replayed on {r['campaign']} ({r['experiment']}, {r['traces']} traces), "
          f"modeled at {r['cpus']} CPUs per candidate")
    print("  screening trace            matches  wrong (largest gap)  ambiguous  simulation skipped  held CPU-h freed (net)")
    for t in sorted(r["per_trace"], key=lambda t: (t["wrong"], -(t["held_freed_cpu_hours"] - t["held_added_cpu_hours"]))):
        net = round(t["held_freed_cpu_hours"] - t["held_added_cpu_hours"], 1)
        gap = "" if t["worst_wrong"] is None else ("failed" if t["worst_wrong"] == math.inf else f"{t['worst_wrong']:.3%}")
        print(f"    {t['trace']:<24} {t['matches']:>7}  {t['wrong']:>5} {gap:>13}  {t['ambiguous']:>9}  "
              f"{t['skipped_cpu_hours']:>8} of {t['simulated_cpu_hours']:<7}  {net:>8} of {t['held_cpu_hours']}")
    print("  (largest gap: the biggest per-trace cycle difference between a wrongly matched candidate and the behavior it matched)")
    d = r["detail"]
    if not d:
        print("  every trace has wrong matches; no screen is safe on this campaign's records")
        return
    print(f"  {d['trace']}: {d['matches']} of {d['candidates']} new candidates matched an earlier generation's "
          f"behavior; {d['wrong']} wrong; {d['ambiguous']} not screened (ambiguous); {d['behaviors_indexed']} "
          f"behaviors indexed, {d['ambiguous_keys']} ambiguous keys")
    print(f"    simulation: {d['skipped_cpu_hours']} of {d['simulated_cpu_hours']} CPU-hours skipped, "
          f"{d['stopped_cpu_hours']} spent on runs stopped early")
    print(f"    held at {d['cpus']} CPUs: {d['held_freed_cpu_hours']} CPU-hours freed by matches, "
          f"{d['held_added_cpu_hours']} added by starting the screening trace first, of {d['held_cpu_hours']}")
    for w in d["wrong_matches"][:20]:
        gap = "" if w["distance"] is None else f", {w['distance']:.3%} apart"
        print(f"    wrong: g{w['generation']} {w['candidate']} ({w['why']}{gap})")


def print_diversity(r):
    d = r["diversity"]
    print(f"campaign {r['campaign']} ({r['experiment']}, {r['traces']} traces): {d['candidates']} of "
          f"{r['candidates_seen']} candidates scored on every trace")
    within = ", ".join(f"{w['behaviors']} within {w['threshold']:.1%}" for w in d["within"])
    print(f"  behaviors: {d['behaviors']} distinct ({within}); effective number {d['hill_1']} (Hill 1), "
          f"{d['hill_2']} (Hill 2); Vendi {d['vendi']} at {d['vendi_scale']:.1%}")
    for p in d["close_pairs"]:
        print(f"  pairs of distinct behaviors within {p['threshold']:.1%} on every trace: {p['pairs']} of {p['of']}")
    x = r["cross_check"]
    print(f"  LLC misses group the {x['candidates']} candidates with miss data into {x['miss_groups']} behaviors: "
          + ("the same grouping as cycles" if x["agree"] else "DIFFERENT from cycles"))
    g = r["good"]
    print(f"  good (fitness > 1 and within {g['within']:.0%} of the best, {g['best']}): {g['behaviors']} behaviors, "
          f"{g['candidates']} candidates; {g['above_lru']} behaviors above LRU")
    print("  gen  replies  copies  scored  new  repeats  like parent  so far  population (scored, behaviors)")
    for row in r["generations"]:
        pop = (f"{row['population']} ({row['population_scored']}, {row['population_behaviors']})"
               if "population" in row else "-")
        print(f"  {row['generation']:>3}  {row['replies']:>7}  {row['copies']:>6}  {row['scored']:>6}  {row['new_behaviors']:>3}"
              f"  {row['repeats']:>7}  {row['like_parent']:>5} of {row['parent_known']:<3}  {row['behaviors_so_far']:>6}  {pop}")
    print("  island  replies  scored  behaviors  only here")
    for i in r["islands"]:
        print(f"  {str(i['island']):>6}  {i['replies']:>7}  {i['scored']:>6}  {i['behaviors']:>9}  {i['only_here']:>9}")
    s = r["separation"]
    if s:
        print("  behaviors one trace separates alone (median run s):")
        for t in sorted(s["per_trace"], key=lambda t: -t["behaviors"]):
            print(f"    {t['trace']:<24} {t['behaviors']:>4}   {t['median_run_s']}")
        print("  k traces: fewest / median / most behaviors separated   "
              + "  ".join(f"{q['k']}: {q['min']}/{q['median']}/{q['max']}" for q in s["subsets"]))
        print(f"  smallest separating set (greedy, short traces first): {', '.join(s['greedy_set'])} "
              f"(about {s['greedy_set_run_s']} s per candidate)")
    c = r["code"]
    print(f"  code, {c['pairs']} pairs ({c['same_behavior_pairs']} with the same behavior): line distance median "
          f"{c['line_distance_same']} same vs {c['line_distance_different']} different; OpenEvolve's measure "
          f"{c['openevolve_same']} vs {c['openevolve_different']}; Spearman with behavior distance "
          f"{c['spearman_line']} (lines), {c['spearman_openevolve']} (OpenEvolve)")
    print("  largest behaviors: candidates, fitness, first generation, islands, members")
    for grp in r["groups"]:
        more = f" +{len(grp['members']) - 4}" if len(grp["members"]) > 4 else ""
        print(f"    {grp['candidates']:>4}  {grp['fitness']}  g{grp['first_generation']}  {grp['islands']}  "
              f"{' '.join(grp['members'][:4])}{more}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("campaign")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--thresholds", default=",".join(str(t) for t in DEFAULT_THRESHOLDS),
                        help="relative per-trace cycle differences treated as the same behavior (default 0.001,0.01)")
    parser.add_argument("--screen", nargs="?", const="", metavar="TRACE",
                        help="replay the duplicate screen instead, detailed for TRACE")
    parser.add_argument("--cpus", type=int, help="with --screen: CPUs per candidate (default: the campaign's)")
    args = parser.parse_args()
    try:
        if args.screen is not None:
            r = screen_replay(cp.Campaign(args.campaign), args.screen or None, args.cpus)
            print(json.dumps(r, indent=1)) if args.json else print_screen(r)
            return 0
        thresholds = tuple(float(t) for t in args.thresholds.split(",") if t)
        r = diversity(cp.Campaign(args.campaign), thresholds)
    except (cp.CampaignError, runspec.SpecError, ec.InfraError, ValueError) as e:
        print(f"diversity: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=1)) if args.json else print_diversity(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
