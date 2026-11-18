# Comparing LLC replacement policies beyond IPC speedup: sources added 2026-11-11

Scope: is there a standard way to compare two replacement policies' behavior, beyond IPC speedup? This round extends `../LLC policy and evolutionary search metrics/llc_oracle_behavioral_metrics.md` (Q2, Q4, Q5), which covers Belady-based metrics, decision accuracy, CacheQuery and Relacs.

Method: WebSearch summaries, plus a full-text read of CacheForge. Each item says how far it was verified.

## Takeaway

- No standard exists for comparing two policies' behavior directly. This held across searches for policy similarity, decision agreement, miss-set overlap, behavioral clustering and quality-diversity descriptors for cache policies.
- The nearest standards:
  - per-workload speedup, hit rate or MPKI
  - the share of the gap to Belady's optimal policy closed
  - agreement with optimal decisions
- Storage and CDN caching have broader conventions: miss-ratio curves across cache sizes, and one normalized miss-ratio reduction over thousands of traces. CPU LLC work doesn't use these.

## Findings

**Hit-set overlap between two policies (one thesis).** Taeho Lim, "Pseudo Re-Reference Interval Prediction for Last-Level Cache Replacement", master's thesis, UNIST, 2019.
- It shows PRRIP's behavioral similarity to RRIP with an "IoU_hit" averaging 0.97 over six benchmarks.
- Known from search snippets only: the PDF fetch returned HTTP 403, so the definition is unverified.
- It's the only policy-vs-policy behavioral overlap measure found.
- [PDF](https://core.ac.uk/download/213600494.pdf)

**Gap to optimal closed.**
- A 2026 preprint on multi-class cache fairness defines gap closure as (m_LRU − m_policy)/(m_LRU − m_Belady) on miss rate (search snippet only) — [arXiv 2608.14561](https://arxiv.org/pdf/2608.14561).
- EVA instead reports the share of the random→MIN gap closed: 57%, vs 47% for SHiP — [EVA](https://people.csail.mit.edu/sanchez/papers/2017.eva.hpca.pdf).

**Miss-ratio curves for any policy (storage caching).**
- Miniature simulations run a scaled-down cache over a spatially hashed sample of requests. They give accurate miss-ratio curves for non-stack policies such as ARC and LIRS, on over a hundred traces (abstract and secondary sources) — [Waldspurger et al., ATC '17](https://www.usenix.org/conference/atc17/technical-sessions/presentation/waldspurger).
- Kosmo (FAST '24) cuts their memory cost for LFU, FIFO, 2Q and LRFU (abstract) — [Kosmo](https://www.usenix.org/conference/fast24/presentation/shakiba).
- A per-policy curve across cache sizes is a behavioral profile, but no LLC work uses one.

**Normalized miss-ratio reduction across many traces (libCacheSim users).**
- Miss-ratio reduction vs FIFO uses an asymmetric denominator: FIFO's miss ratio when the policy is better, the policy's own when it's worse. That keeps gains and losses comparable (search summary of one paper) — [DynamicAdaptiveClimb](https://arxiv.org/pdf/2511.21235).
- Studies run thousands of production traces through [libCacheSim](https://github.com/1a1a11a/libCacheSim).

**CacheForge (arXiv 2610.07668, v2 10 Nov 2026; full text read).**
- **Metrics:** IPC, MPKI, total and per-access-type hit rate (load, RFO, prefetch, writeback), storage in KB, IPC per KB, and per-workload IPC gains over LRU.
- **Hit rate vs IPC:** "many of the highest-hit-rate policies deliver lower IPC than the baselines" (Sec. V-B). A policy can raise hits "by zealously protecting prefetch and writeback lines" (Sec. V-A). The paper gives no numbers for this.
- **Diversity:** no similarity, clustering or duplicate analysis among its 10,687 generated policies. Sec. V-C's 21-feature analysis is about impact on hit rate, not similarity.
- [CacheForge](https://arxiv.org/html/2610.07668)

**Searches that found nothing.**
- MAP-Elites or quality-diversity applied to cache replacement: none found.
- An empirical clustering of replacement policies by decision agreement or miss-set overlap: none found, matching the earlier notes.

## How AFAR uses this

- `controller/diversity.py` treats per-trace results as the behavior: identical cycles on every trace mean one behavior. It reports richness, Hill numbers and the Vendi score (`docs/campaigns.md` §9).
- Gap-to-optimal, hit-set overlap and decision agreement need an LLC access-stream replay, which isn't built yet.
