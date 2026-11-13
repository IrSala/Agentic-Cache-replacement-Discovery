# Oracle-Based and Behavioral (Decision-Level) Metrics for Analyzing and Comparing Cache Replacement Policies

Scope note: compiled 2026-11-06. Primary PDFs were downloaded and text-searched for exact numbers (Hawkeye, Harmony, Glider, Mockingjay, Parrot, LRB, FOO/PFOO, Writeback-Aware Caching, EVA, SHiP, CacheQuery, ChampSim, CacheForge, CacheFit, CacheMind). Items marked "(search snippet only)" or "(bibliographic only)" were not read in full.

## Q1. Belady's MIN/OPT and its variants (bypass, prefetch/demand, cost/MLP, variable size, writeback, OPTgen); how papers measure "distance to optimal"

### Takeaway
Classic MIN minimizes total misses for unit-size, unit-cost blocks. Once the objective changes, it is no longer "the" optimum. That happens with demand misses under prefetching (Demand-MIN), variable object sizes (NP-hard; FOO/PFOO bounds), writeback costs (NP-complete), MLP or performance, and multicore interleavings, where MIN is not even well defined. Papers report "distance to optimal" in at least five incompatible ways: normalized hit rate (LRU→OPT gap closed), fraction of the gap from random to MIN closed, IPC gain over LRU next to MIN's IPC gain, error relative to infinite-lookahead OPT, and bracketing between offline upper and lower bounds. The choice of OPT variant (with or without bypass, demand-only or all accesses) has to match the policy's capabilities and the metric being reported.

### Cited Findings
**Classic MIN/OPT**
- Belady's 1966 IBM Systems Journal paper introduced MIN, which evicts the block referenced furthest in the future — [Belady 1966, DOI](https://doi.org/10.1147/sj.52.0078).
- Mattson et al. (1970) introduced stack algorithms and one-pass evaluation of all cache sizes. Berger et al. note that "the algorithm commonly called 'Belady' was invented (and proved to be optimal) by Mattson" and that "Belady's MIN algorithm actually operates somewhat differently" — [Mattson et al. 1970, DOI](https://doi.org/10.1147/sj.92.0078); [Berger et al., arXiv 1711.03709](https://arxiv.org/abs/1711.03709).
- Belady can be computed in O(N log C) — [Berger et al.](https://arxiv.org/abs/1711.03709).

**OPTgen (Hawkeye): MIN reconstructed online, with bypass**
- OPTgen keeps a per-set occupancy vector of overlapping "liveness intervals". On a reuse of X, if every entry in X's usage interval is below associativity, OPT would have hit and the entries are incremented. Otherwise the access is an OPT miss — [Hawkeye, ISCA 2016](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).
- **Bypass is built in.** "By not incrementing any of the shaded elements of the occupancy vector for cache misses, OPTgen assumes that misses will bypass the cache. If we wanted OPTgen to instead assume a cache with no bypassing, then the most recent entry … would have been initialized to 1 instead of 0." — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).
- **Lookahead window.** OPTgen models 8× the associativity of history: a 128-entry vector of 4-bit entries per set for a 16-way cache. Unsampled, this would be over 260K entries for a 2 MB 16-way LLC — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).
- **Accuracy.** OPTgen is exact with all sets and a time quantum of 1. Sampling 64 sets costs 0.5% accuracy versus true OPT, and a time quantum of 4 costs a further 0.3%, for "99% accuracy in modeling the OPT solution" — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).
- **Lookahead matters a lot.** Hawkeye Fig. 2 plots Belady's error against infinite-lookahead OPT for windows of 1×–16× cache capacity. Mockingjay summarizes the prior result: "Belady's MIN performs worse than LRU if given a window into the future that is equal to the size of the cache." Parrot's Fig. 2: reaching 80% of infinite-window Belady requires computing reuse distances ~2600 accesses into the future — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf); [Mockingjay, HPCA 2022](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf); [Parrot, arXiv 2006.16239](https://arxiv.org/abs/2006.16239).

**Prefetch-aware variants: Demand-MIN and Flex-MIN (Harmony, ISCA 2018)**
- MIN "minimizes the total number of cache misses—including those for prefetched lines—[but] does not minimize the number of demand misses." — [Harmony](https://www.cs.utexas.edu/~lin/papers/isca18.pdf).
- **Demand-MIN rule.** "Evict the line that will be prefetched furthest in the future, and if no such line exists, evict the line that will see a demand request furthest in the future." Even MIN applied with prefetches ignored would not reach the Demand-MIN solution — [Harmony](https://www.cs.utexas.edu/~lin/papers/isca18.pdf).
- **Demand misses.** On a 4-core SPEC 2006 mix, average MPKI is 29.8 for LRU, 21.7 for MIN and 16.9 for Demand-MIN. The extra demand hits come at the cost of more prefetch traffic — [Harmony](https://www.cs.utexas.edu/~lin/papers/isca18.pdf).
- **Flex-MIN.** Flex-MIN uses a heuristic metric, LED (Lines Evicted per Demand-hit), to pick a point between MIN and Demand-MIN. The authors say "Flex-MIN is not optimal in any theoretical sense" — [Harmony](https://www.cs.utexas.edu/~lin/papers/isca18.pdf).
- **Harmony results.** Harmony, which is Hawkeye trained on Flex-MIN, improves IPC over LRU by 7.7% on 4 cores (vs 3.7% for optimized PACMan) and by 9.4% on 8 cores (vs 4.4%) — [Harmony](https://www.cs.utexas.edu/~lin/papers/isca18.pdf).

**Cost and MLP**
- Hawkeye notes that "MLP and prefetch-friendliness can be used to reduce the overall performance penalty of LLC misses" and that Hawkeye itself "focuses on cache misses". Miss-count optimality is therefore not performance optimality — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).
- Cost-sensitive replacement with nonuniform miss costs (Jeong & Dubois) exists as a line of work (bibliographic only) — [listing](https://www.bohrium.com/paper-details/cache-replacement-algorithms-with-nonuniform-miss-costs/812112989778870273-2507).

**Variable object sizes (Berger, Beckmann, Harchol-Balter, SIGMETRICS 2018)**
- OPT with variable sizes is NP-hard, and remains "strongly NP-complete even with just three object sizes" — [arXiv 1711.03709](https://arxiv.org/abs/1711.03709).
- **FOO and PFOO.** FOO casts caching as a min-cost flow problem. Its practical versions, PFOO-U and PFOO-L, give upper and lower bounds on OPT — [arXiv 1711.03709](https://arxiv.org/abs/1711.03709).
- **Common size-aware heuristics are poor proxies for OPT.** Against PFOO-U, Belady-Size is 19% higher on average, Freq/Size 22% higher and Belady 72% higher. PFOO-U and PFOO-L bound OPT within a 4.2% range on average — [arXiv 1711.03709](https://arxiv.org/abs/1711.03709).
- **A misleading bound hides the gap.** On a CDN trace, "the best prior upper bound on OPT is within 1% of online algorithms … leading to the false impression that there is no room for improvement". PFOO shows OPT is about 22% below the online algorithms — [arXiv 1711.03709](https://arxiv.org/abs/1711.03709).

**Writebacks**
- Writeback-aware caching (Beckmann, Gibbons, Haeupler, McGuffey, APOCS 2020) has an NP-complete offline problem "regardless of the items' writeback cost(s) and miss cost(s)", and it is Max-SNP hard. It extends the Farach-Colton & Liberatore proof for paging with writebacks. Their online Writeback-Aware Landlord attains the optimal competitive ratio — [APOCS 2020 PDF](https://www.pdl.cmu.edu/ftp/Storage/2020.apocs.writeback.pdf).

**Multicore**
- "Belady's MIN is not well defined for multicore systems." — [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf), footnote 1.
- Hawkeye: "the optimal solution of the past memory access stream may not represent the optimal caching decisions for the future" because of non-deterministic schedules — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).

**How papers measure distance to optimal**

| Metric | How it is defined or reported | Source |
|---|---|---|
| Normalized cache hit rate (fraction of the LRU→OPT gap closed) | (r − r_LRU)/(r_OPT − r_LRU), reported next to raw hit rate; 0 is LRU, 1 is Belady | [Parrot](https://arxiv.org/abs/2006.16239) |
| Fraction of the gap from random to MIN closed | EVA closes 57% vs SHiP 47%, DRRIP 41%, PDP 42% | [EVA, HPCA 2017](https://people.csail.mit.edu/sanchez/papers/2017.eva.hpca.pdf) |
| IPC gain over LRU, next to MIN's IPC gain | Single core, no prefetcher: SHiP 3.4%, Hawkeye 4.5%, Mockingjay 5.7%, Belady's MIN 6.0% | [Mockingjay Table I](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf) |
| Error relative to infinite-lookahead OPT | Used for lookahead studies | [Hawkeye Fig. 2](https://www.cs.utexas.edu/~lin/papers/isca16.pdf) |
| Gap between online algorithms and MIN | Reported as 25%–40% for CDN caches | [LRB, NSDI 2020](https://www.usenix.org/system/files/nsdi20-paper-song.pdf) |
| Upper and lower bounds on OPT | PFOO-U and PFOO-L, with absolute miss-ratio error | [FOO/PFOO](https://arxiv.org/abs/1711.03709) |

- On the normalized hit rate, Parrot says it "represents the effectiveness of a given policy with respect to the two baselines". Parrot also compares against Glider only on normalized hit rates, after checking that its LRU and Belady hit rates were close to the numbers Glider reported — [Parrot](https://arxiv.org/abs/2006.16239).
- In Parrot's Table 1, LRU's raw LLC hit rate is 0.0% on lbm and libquantum and 0.1% on milc. In those cases r_LRU is the floor of the normalized metric — [Parrot](https://arxiv.org/abs/2006.16239).
- **Equal-capacity framing.** Parrot converts gains into equivalent LRU cache capacity: 2–3× for omnetpp and mcf, up to 16× for libquantum, citing Jaleel 2010. Mockingjay calls itself "the first replacement policy that can obtain better performance than an LRU cache that is twice as large" — [Parrot](https://arxiv.org/abs/2006.16239); [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf).
- **Relaxed optimum.** LRB defines relaxed Belady, which evicts any object whose next request is beyond a threshold. Using the "Belady boundary" as that threshold costs 9–13% more misses than MIN — [LRB](https://www.usenix.org/system/files/nsdi20-paper-song.pdf).

### Inferences
- **Normalized hit rate breaks down when the gap is small.** When r_OPT − r_LRU is small (streaming or thrash-dominated benchmarks), the score becomes unstable or undefined. Report the absolute gap with it and drop or flag benchmarks with tiny gaps.
- **Choose the OPT variant to match the policy.**
  - Use OPT-with-bypass for policies that can bypass, since OPTgen's MIN already assumes bypass.
  - Use no-bypass MIN for policies that must insert.
  - Use Demand-MIN as a second reference when L2/LLC prefetches are in the stream.
  - Otherwise "victim matches OPT" penalizes policies for capabilities they lack, or rewards ignoring prefetch value.
- **MIN's IPC is not an upper bound on IPC.** Harmony shows that fewer total misses is not the same as fewer demand misses, and Hawkeye notes MLP effects. So "fraction of MIN's IPC gain achieved" is a heuristic, not a bound.
- **Writebacks need an explicit rule.** Writeback-aware OPT is intractable. Simply treat L2 writebacks arriving at the LLC either as ordinary accesses or as non-demand fills, and document which.
- **Multicore shared-LLC replay** should not use a single global OPT as "ground truth" (Mockingjay footnote). Restrict OPT-agreement metrics to single-core streams, or to one recorded interleaving with that caveat stated.

### Gaps
- I did not read Jeong & Dubois (CSOPT), Qureshi et al. "A Case for MLP-Aware Cache Replacement" (ISCA 2006), or a primary source on an offline OPT with bypass beyond OPTgen's description. Their exact formulations are unverified here.
- No source found that standardizes which OPT variant (bypass vs no-bypass, prefetch handling, writeback handling) to use in ChampSim-based studies. Practice differs per paper.

## Q2. Decision-level metrics (per-decision accuracy vs OPT, victim agreement, miss-set overlap, insertion/eviction breakdowns, miss classification, reuse/stack-distance and per-PC analyses)

### Takeaway
Established decision-level metrics fall into three groups:
- **Label accuracy against OPT.** Hawkeye and Glider score cache-friendly/averse predictions against OPT's caching decisions.
- **Victim-level agreement with OPT at the policy's own state.** Parrot reports top-1/top-5 accuracy and the "reuse distance gap". LRB uses the "good decision ratio" against a relaxed-Belady boundary.
- **Mechanism-attribution breakdowns.** Hawkeye reports the share of evictions from its predictor vs the LRU fallback (71%/29%), and Mockingjay the share using ETA (92%). Per-PC "bias of OPT" is another.

Parrot shows that exact victim agreement is "interesting, but imperfect" because several near-equivalent victims often exist. LRB's boundary-based metric explicitly handles that and correlates strongly with end-to-end miss ratio. I found no published policy-vs-policy victim-agreement or miss-set-overlap metric in this literature (see Q5).

### Cited Findings
**Hawkeye and Glider: per-access label accuracy against OPT**
- **Per-PC bias.** Hawkeye reports "the average per-PC bias—the probability that loads by the same PC have the same caching behavior as OPT—is 90.4%". Elsewhere it gives the "average bias of the OPT solution for load instructions" as 91% — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).
- **Predictor accuracy.** The Hawkeye predictor is "81% accurate in predicting OPT's decisions for future accesses" (Fig. 14). Glider reports that Hawkeye's predictor "achieves only 72.4% accuracy on a set of challenging benchmarks", and 72.2% in its offline comparison. The figures differ because the benchmark sets differ — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf); [Glider, MICRO 2019](https://www.cs.utexas.edu/~lin/papers/micro19c.pdf).
- **Glider offline.** Glider's attention LSTM, trained offline on Belady labels, reaches 82.6% vs 72.2% for Hawkeye, a +10.4% improvement. Its offline ISVM reaches +9.1% — [Glider](https://www.cs.utexas.edu/~lin/papers/micro19c.pdf).
- **Glider online.** Online, Glider reaches 88.8% vs 84.9% for Hawkeye. On the offline-training subset it improves from 73.5% to 82.4%, mirroring the offline 72.2% → 81.2% — [Glider](https://www.cs.utexas.edu/~lin/papers/micro19c.pdf).

**Parrot: victim-level agreement with Belady at the learned policy's own cache states**
- **Top-K accuracy** is "the percentage of the time that the optimal line to evict according to Belady's is in the top-K lines with highest probability of eviction under our learned policy". With 16-way sets, top-16 is trivially 100% — [Parrot App. B.1](https://arxiv.org/abs/2006.16239).
- **Reuse distance gap** is the "average difference between the reuse distance of the optimal line to evict l* and the reuse distance of the line evicted by PARROT, i.e., d_t(l*) − d_t(l_w)"; "a policy with a reuse distance gap of 0 is optimal" — [Parrot](https://arxiv.org/abs/2006.16239).
- **Results** (Parrot Table 2, mean of 3 seeds):

| Program | Top-1 | Top-5 | Reuse distance gap |
|---|---|---|---|
| cactusADM | 91.3% | 98.3% | 2.0 |
| lbm | 83.6% | 98.0% | 2.1 |
| libquantum | 83.2% | 97.3% | 2.2 |
| mcf | 75.2% | 87.6% | 6.4 |
| omnetpp | 35.0% | 65.1% | 533.4 |
| astar | 17.2% | 50.8% | 20065.8 |
| gems | 15.0% | 45.5% | 46.4 |

  Source: [Parrot](https://arxiv.org/abs/2006.16239).
- **Why victim agreement is imperfect.** omnetpp and Web Search have much lower top-1/top-5 than mcf "even though its normalized cache hit rate in these programs is similar … this can occur when several cache lines have similar reuse distances … so evicting any of them is roughly equivalent. Thus top-K accuracy is an interesting, but imperfect metric." — [Parrot](https://arxiv.org/abs/2006.16239).
- **The reuse distance gap does not track hit rate either.** It varies "wildly" (omnetpp vs mcf) at similar hit rates, reflecting access patterns more than decision quality — [Parrot](https://arxiv.org/abs/2006.16239).
- **On-policy vs oracle-state evaluation.** Parrot trains with DAgger because off-policy training on Belady's states causes compounding errors: "states visited during training under Belady's differ from those visited during test time". On-policy training gives +9.8% normalized hit rate on average, and the effect is program-dependent — [Parrot](https://arxiv.org/abs/2006.16239).

**LRB: good decision ratio (CDN caches)**
- **Belady boundary** is "the minimum of time to next request for all evicted objects by Belady's MIN algorithm". An eviction is "good" if the object's next request is beyond that boundary — [LRB](https://www.usenix.org/system/files/nsdi20-paper-song.pdf).
- **Good decision ratio** is "# good decisions / # total eviction decisions" and "correlates strongly with the byte miss ratio it ultimately achieves" — [LRB](https://www.usenix.org/system/files/nsdi20-paper-song.pdf).
- **Why it was introduced.** End-to-end design-space exploration "requires full simulations that take several hours for a single configuration". The good decision ratio "allows us to run a simulation only once" and then score model choices quickly — [LRB](https://www.usenix.org/system/files/nsdi20-paper-song.pdf).
- **Design choices it drove.** Features (32 deltas, 10 EDCs), model (GBM over LogReg, LinReg, SVM and a small NN), target (log time-to-next-request), loss (L2), training set (128K) and eviction sample size (64) were all chosen by good decision ratio — [LRB](https://www.usenix.org/system/files/nsdi20-paper-song.pdf).
- **Ties are absorbed by design.** Relaxed Belady evicts randomly among objects beyond the boundary, for example 19% of objects for a 64 GB cache — [LRB](https://www.usenix.org/system/files/nsdi20-paper-song.pdf).

**Insertion and eviction attribution (fallback rates)**
- **Hawkeye.** When all candidates are predicted cache-friendly, Hawkeye evicts the oldest line (LRU). "The Hawkeye Predictor accounts for 71% of the overall evictions, though the distribution varies widely across" benchmarks (Fig. 16: evictions split into cache-averse vs cache-friendly) — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).
- **Mockingjay** "is able to use ETA information—and thus mimic MIN—for 92% of its evictions". It argues binary policies have "many ties among lines in the same class, which are typically broken by falling back on the same LRU heuristic that these policies attempt to improve upon" — [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf).
- **How errors are handled.** Hawkeye's mispredictions "result in the eviction of the least-recently used cache-friendly" line. Mockingjay instead evicts the line with the largest absolute ETR, the line furthest from its predicted reuse in either direction — [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf).
- **Reuse-distance prediction accuracy.** Mockingjay's reuse-distance predictor is 85% accurate vs 43% for IbRDP. Mockingjay also shows KPK and IbRDP's max(ETR, age) rule makes non-MIN choices once age exceeds half the reuse distance — [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf).

**Miss classification and access-pattern taxonomy**
- **Access-pattern taxonomy.** From the RRIP paper (Jaleel et al., ISCA 2010), as tabulated in SHiP:
  - Recency-friendly: (a1…ak, ak…a1)^N
  - Thrashing: (a1…ak)^N with k > C
  - Streaming: k = ∞
  - Mixed: [(a1…ak)^A P_ε(b1…bm)]^N with k < C < m

  "LRU … behaves well for both recency-friendly and streaming access patterns. However, LRU performs poorly for thrashing and mixed access patterns" — [SHiP, MICRO 2011](https://mrmgroup.cs.princeton.edu/papers/MICRO11_SHiP_Wu_Final.pdf); [RRIP DOI](https://doi.org/10.1145/1816038.1815971).
- **3C model** (compulsory/capacity/conflict): Hill & Smith, "Evaluating associativity in CPU caches", IEEE TC 1989 — [DOI](https://doi.org/10.1109/12.40842); [PDF copy](https://typeset.io/pdf/evaluating-associativity-in-cpu-caches-19pprswizq.pdf).
- **OPT-based miss characterization.** Sugumar & Abraham (SIGMETRICS 1993), "Efficient Simulation of Caches under Optimal Replacement with Applications to Miss Characterization", uses OPT-simulated caches to characterize misses (bibliographic only) — [listing](https://scilit.com/publications/de7e63666d3615e9dc1810c8e5a57954); [dblp](https://dblp.org/pid/01/6046).

**Reuse and stack distance, per-PC analyses**
- Mattson et al. introduced stack algorithms and stack-distance (one-pass, all-sizes) evaluation — [DOI](https://doi.org/10.1147/sj.92.0078).
- **Long lookahead is needed.** Parrot shows that capturing Belady's benefit requires reuse distances far beyond cache capacity (80% of the benefit needs ~2600 accesses of lookahead) — [Parrot](https://arxiv.org/abs/2006.16239).
- **Global vs per-set reuse distance.** Mockingjay finds "global reuse distances have extremely high variability" and uses per-set reuse distances instead — [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf).
- **Per-PC structure.** Per-PC bias of OPT decisions is about 90% (see above), which is why PC-indexed predictors work — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).

### Inferences
- **"Victim matches OPT at the policy's own state" is Parrot's top-1 accuracy, evaluated on-policy.** Parrot's DAgger finding implies on-policy vs OPT-state evaluation gives different numbers, so state which one you report.
- **Define OPT-equivalent victim sets rather than a single OPT victim.** Both Parrot (ties) and LRB (boundary) show this is needed. Treat as equivalent all lines never reused again, or all lines beyond the Belady boundary or beyond the lookahead window. Report:
  - strict top-1 agreement;
  - "victim in OPT-equivalent set" (an LRB-style good-decision ratio);
  - the reuse-distance gap, capped or log-scaled, because Parrot's raw gap ranges from about 2 to 20,000.
- **Fallback-rate metrics transfer directly as behavioral fingerprints.** Examples: the share of evictions made by the "smart" path vs the LRU/age fallback (Hawkeye 71%, Mockingjay 92%), and the share of inserts at each RRPV or bypassed. Two evolved policies with the same hit rate but different fallback rates are behaviorally different.
- **Miss-set overlap is a natural extension.** Use Jaccard similarity on the (access index → hit/miss) vectors, or Cohen's kappa so it is robust to base rates. No source in this survey reports it for replacement policies. Stratify it by PC, set, OPT label, reuse-distance bucket and access type (load, RFO, prefetch, writeback), so differences are not dominated by streaming accesses that every policy misses.

### Gaps
- Could not verify Sugumar & Abraham's exact miss categories. They are believed to separate misses due to non-optimal replacement from capacity misses, but that is unverified.
- No primary source found that reports victim agreement or miss-set overlap between two non-oracle replacement policies.
- Dead-block-predictor metrics (coverage and false-positive rate, e.g., SDBP, Perceptron reuse prediction) were not fetched in this pass.

## Q3. Trace-driven functional replay vs timing simulation: offline LLC trace datasets and gyms, fidelity, speed, feedback loop

### Takeaway
The main ML-for-replacement works trained and evaluated on fixed LLC access streams obtained functionally:
- **Parrot.** Raw DynamoRIO traces filtered through LRU L1/L2, 64 sampled sets, about 5M LLC accesses per program. Its repo also provides a ChampSim patch pinned to a specific commit for producing CRC2-based LLC traces, plus an OpenAI Gym environment.
- **Glider.** Offline labels from Belady.

These streams treat the LLC request sequence as policy-independent. Sources acknowledge this breaks under multicore scheduling (Hawkeye: MIN not well-defined for multicore; Mockingjay). Classic papers studied the validity of trace-driven multiprocessor simulation. But I found no quantitative study of how much a single-core ChampSim LLC stream changes when the LLC policy changes. LRB documents the speed motivation: one replay pass vs hours per full simulation.

### Cited Findings
**Parrot's trace pipeline and environment**
- **Trace collection.** "We run them and collect raw memory access traces over a 50 second interval using dynamic binary instrumentation tools … Last-level cache access traces are obtained from this sequence by passing the raw memory accesses through the L1 and L2 caches using an LRU replacement policy." — [Parrot](https://arxiv.org/abs/2006.16239).
- **Hierarchy.** 4-way 32 KB L1, 8-way 256 KB L2, 16-way 2 MB LLC, following Glider — [Parrot](https://arxiv.org/abs/2006.16239).
- **Sampling and splits.** Parrot randomly chooses 64 LLC sets ("totaling an average of about 5M accesses per program") and splits each trace 80/10/10 into train/validation/test. Each access is (address, PC) — [Parrot](https://arxiv.org/abs/2006.16239).
- **Generalization check.** On Web Search, 5.3% of test-time addresses and 6% of PCs were unseen in training — [Parrot](https://arxiv.org/abs/2006.16239).
- **Repo.** The repo says the exact traces cannot be released and gives two recipes — [google-research/cache_replacement README](https://github.com/google-research/google-research/tree/master/cache_replacement):
  1. Recommended: clone ChampSim, `git checkout 8798bed8117b2873e34c47733d2ba4f79b6014d4`, apply `environment/traces/champsim.patch`, build with `create_llc_trace`, run on CRC2 traces to produce `llc_access_trace.csv`, then filter to the paper's 64 sets.
  2. A DynamoRIO `drcachesim` "miss_tracer" with L1D 32 KB/4-way and LL 256 KB/8-way.
- **Gym environment.** The same README describes "a cache replacement OpenAI gym environment in `cache_replacement/environment/`, along with various baselines, including S4LRU, LRU, Belady's, and the Nearest Neighbors baseline" — [README](https://github.com/google-research/google-research/tree/master/cache_replacement).

**Other LLC trace sources and sanity checks**
- Glider labels training data with Belady's MIN and trains offline on PC sequences (an attention LSTM), then distills to an online ISVM — [Glider](https://www.cs.utexas.edu/~lin/papers/micro19c.pdf).
- The CRC2 traces are the de facto shared input set for ChampSim replacement studies, used by Parrot's recipe — [Parrot README](https://github.com/google-research/google-research/tree/master/cache_replacement).
- **Cross-pipeline sanity check.** Parrot "verified that the measured hit rates for LRU and Belady's on our cache accesses are close to the numbers reported by Shi et al. (2019)" before comparing normalized hit rates — [Parrot](https://arxiv.org/abs/2006.16239).

**Fidelity limits**
- **Multicore.** "Belady's MIN is not well defined for multicore systems" — [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf). Hawkeye flags "variability that can arise from non-deterministic schedules" — [Hawkeye](https://www.cs.utexas.edu/~lin/papers/isca16.pdf).
- **Classic validity studies** (bibliographic only):
  - Goldschmidt & Hennessy, "The Accuracy of Trace-Driven Simulations of Multiprocessors", SIGMETRICS 1993, pp. 146–157 — [researchr](https://researchr.org/publication/GoldschmidtH93/bibtex).
  - Koldinger, Eggers & Levy, "On the Validity of Trace-Driven Simulation for Multiprocessors", ISCA 1991 (mentioned in the same search results; not opened).
- **ChampSim's own limits.** ChampSim is trace-driven ("the workload to be simulated is instrumented and run offline … produces a digest … called a trace"), and "the trace format used imposes some restrictions on the correctness of ChampSim's simulation" — [ChampSim paper, arXiv 2210.14324](https://arxiv.org/abs/2210.14324).
- **Prefetches.** With prefetching, policy choices change prefetch hit/miss outcomes and memory traffic, as in Harmony's MIN vs Demand-MIN trade-off. A fixed-stream replay that treats prefetch accesses as ordinary accesses will not capture this trade-off — [Harmony](https://www.cs.utexas.edu/~lin/papers/isca18.pdf).
- **Methodology contrast.** Mockingjay evaluated in timing mode on ChampSim with 200M warmup and 1B measured instructions (SimPoint), on 33 memory-sensitive SPEC06/17/GAP workloads — [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf).

**Speed and feedback loop**
- **LRB.** Full simulations "take several hours for a single configuration". The decision-quality metric lets them "run a simulation only once to gather training data, prediction data, and learn the Belady boundary", then evaluate design choices without full simulation — [LRB](https://www.usenix.org/system/files/nsdi20-paper-song.pdf).
- **FOO/PFOO.** Exact theoretical bounds are "too slow to run on traces this long", which motivated PFOO — [FOO/PFOO](https://arxiv.org/abs/1711.03709).
- **CacheForge.** Each LLM-generated policy is compiled and run in ChampSim on SPEC CPU2006. The paper reports sensitivity studies over "10,687 generated policies" — [CacheForge, arXiv 2610.07668](https://arxiv.org/abs/2610.07668).

### Inferences
- **Why single-core replay is mostly sound.** In single-core ChampSim with a non-inclusive LLC and no LLC prefetcher, the L2→LLC request sequence (demand misses, L2 prefetches, L2 writebacks) is determined by L1/L2 state, which does not depend on the LLC policy. So functional replay should be close to exact apart from timing-mediated effects.
- **Known divergence channels**, all inferred:
  - prefetch-queue drops and issue timing;
  - MSHR merges of requests in flight;
  - LLC prefetcher training on LLC hit/miss;
  - inclusive back-invalidations;
  - multicore interleaving.
- **Measure the divergence directly rather than assuming it.** Record the LLC stream under two very different policies (e.g., LRU vs Mockingjay) and compute sequence edit distance, or the fraction of accesses that are not common to both streams. Then compare replayed hit rates with ChampSim-reported LLC hit rates.
- **The Parrot recipe is a ready template.** It pins the ChampSim commit, applies a patch to dump LLC accesses, samples sets and splits train/test. Pinning the commit is also what makes cross-version port comparisons reproducible.
- **Fast inner loop, timing outer loop.** Decision metrics are cheap and computed in one pass (the LRB rationale), but end-to-end IPC needs timing simulation. A two-tier loop fits: functional replay for behavioral similarity and diversity, with ChampSim timing runs only for promising or behaviorally novel candidates.

### Gaps
- No quantitative study found that measures how much an LLC access stream diverges across LLC replacement policies in ChampSim, single-core or multicore.
- No published speed numbers found for functional LLC replay vs ChampSim timing runs. Parrot's environment speed is not reported in the paper text searched.
- Did not find other public LLC-trace gyms or datasets besides Parrot's environment and CRC2-derived traces. A broader search may find more, e.g., CDN-side libCacheSim, which is outside the LLC scope.
- Goldschmidt & Hennessy and Koldinger et al. findings not read; only titles and venues are confirmed.

## Q4. Interpretability and characterization of policies (Glider distillation, RLR feature analysis, EVA, synthetic patterns and fingerprinting)

### Takeaway
Characterization methods so far fall into four groups:
- **Model-introspection insight.** Glider's attention analysis revealed an unordered, sparse PC history, distilled into an ISVM.
- **Principled value frameworks.** EVA treats replacement as an MDP and shows MIN's naive generalizations are suboptimal under uncertainty.
- **Canonical access patterns.** The RRIP/SHiP taxonomy of recency-friendly, thrashing, streaming and mixed patterns.
- **Formal black-box fingerprinting.** CacheQuery/Polca learns a policy as a Mealy machine from access sequences and checks equivalence against candidate automata.

The last is the closest prior art to "fingerprint a policy by its decisions" and to equivalence-checking a port.

### Cited Findings
**Glider: introspecting an LSTM and distilling it into an ISVM**
- **History length.** The LSTM benefits from a history of ~30 past PCs, "significantly larger than the history length of 3 considered by previous solutions" — [Glider](https://www.cs.utexas.edu/~lin/papers/micro19c.pdf).
- **Attention analysis.** The attention layer shows that "caching decisions depend primarily on the presence of a few memory accesses, not on the full ordered sequence". Increasing the attention scaling factor was used to force sparse, interpretable attention, since unscaled attention was "nearly uniform" — [Glider](https://www.cs.utexas.edu/~lin/papers/micro19c.pdf).
- **Distillation.** An offline ISVM using a k-sparse, unordered PC history "with only 6 unique PCs approaches the accuracy of the attention-based LSTM". Online, Glider is 88.8% accurate vs 84.9% for Hawkeye — [Glider](https://www.cs.utexas.edu/~lin/papers/micro19c.pdf).

**Parrot ablations**
- Parrot's ablations isolate mechanisms: on-policy (DAgger) training gives +9.8% normalized hit rate, the ranking loss +3.5% over log-likelihood, and reuse-distance prediction +16.8%. History length saturates around 80 accesses — [Parrot](https://arxiv.org/abs/2006.16239).

**EVA (Beckmann & Sanchez, HPCA 2017)**
- "MIN assumes perfect knowledge of the future that is unavailable in practice, and the obvious generalizations of MIN are suboptimal with imperfect information" — [EVA](https://people.csail.mit.edu/sanchez/papers/2017.eva.hpca.pdf).
- EVA ranks lines by "economic value added (EVA), the difference of their expected hits from the average", justified via MDP theory — [EVA](https://people.csail.mit.edu/sanchez/papers/2017.eva.hpca.pdf).
- EVA closes 57% of the random→MIN gap vs SHiP 47%, DRRIP 41% and PDP 42%, and matches SHiP with 8% less cache area — [EVA](https://people.csail.mit.edu/sanchez/papers/2017.eva.hpca.pdf).

**Mockingjay: multiclass vs binary prediction**
- Mockingjay's section V analyzes why multiclass (reuse-time) prediction beats binary: small errors do not reorder lines, errors have local impact, and priority is interpreted "at the time of eviction" rather than insertion — [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf).

**Canonical access patterns as test vectors**
- The RRIP/SHiP pattern table defines recency-friendly, thrashing (k > C), streaming (k = ∞) and mixed sequences with explicit formulas usable as synthetic microbenchmarks. LRU handles recency-friendly and streaming well but thrashing and mixed poorly — [SHiP](https://mrmgroup.cs.princeton.edu/papers/MICRO11_SHiP_Wu_Final.pdf).

**CacheQuery/Polca (PLDI 2020): black-box fingerprinting by automata learning**
- **Model.** A per-set replacement policy is modeled as a deterministic Mealy machine whose inputs are Ln(i) (access line i) and Evct (free a line), and whose output is the evicted index. Learning uses Angluin-style membership and equivalence queries via LearnLib — [CacheQuery, arXiv 1912.09770](https://arxiv.org/abs/1912.09770).
- **Equivalence checking.** Equivalence queries are approximated by conformance test suites, "but there are Mealy machines M for which there is no finite test suite that demonstrates non-equivalence", so the approximation "is not complete" — [CacheQuery](https://arxiv.org/abs/1912.09770).
- **Findings.**
  - Intel L1 identified as PLRU "by checking its equivalence to a manually implemented PLRU automaton".
  - "Previously undocumented" policies learned for Skylake/Kaby Lake L2 (New1) and L3 leader sets (New2).
  - Programs synthesized for 8 of 9 policies, "obtained from both the simulators and silicon CPUs".

  Source: [CacheQuery](https://arxiv.org/abs/1912.09770).

**RLR**
- RLR (Sethumurugan, Yin, Sartori, HPCA 2021) used reinforcement learning to discover features, then designed a cheap non-PC policy. A secondary summary says it avoids PC because of storage and datapath overhead (bibliographic listing and secondary summary only) — [reviewer.ly listing](https://cs.reviewer.ly/app/publication/7266f24e-a12a-4562-af04-71f50f0a2709); [blog summary](https://ziyueqiu.github.io/blog/2022/RLR-(HPCA-21)).

**CacheMind (2026 preprint): LLM analysis over traces**
- CacheMind is a RAG-plus-LLM tool for natural-language questions over cache traces, e.g., "Which PC is causing most evictions under policy X?".
- Its CacheMindBench "trace-grounded" tier covers "hit/miss classification, per-PC miss rates, cross-policy comparisons". It reports 66.67%–89.33% on 75 trace-grounded questions, depending on the retriever.

  Source: [CacheMind, arXiv 2602.12422](https://arxiv.org/abs/2602.12422).

### Inferences
- **The SHiP/RRIP pattern formulas make a cheap behavioral fingerprint suite.** Replay each pattern, and its parameter sweeps (k relative to C, scan length, mix ratio), through every candidate. Record hit rate and victim positions to get a compact behavior vector per policy. CacheQuery shows that sequences of Ln(i) and Evct are enough to identify a deterministic policy up to the equivalence the tests can distinguish.
- **For port validation, a Mealy-machine view gives a principled test.**
  - Drive both implementations (old-ChampSim Hawkeye/Mockingjay and the port) with identical per-set access sequences, from CRC2-derived streams plus synthetic patterns.
  - Compare victim way and insertion state step by step.
  - The first divergence localizes the bug.
  - CacheQuery's caveat applies: passing finite tests does not prove equivalence.
- **Learned components are not finite-state policies.** Hawkeye's and Mockingjay's predictors and samplers make the policy a large-state machine that depends on PC and address hashes. Full automata learning is impractical for them, but lockstep differential replay on recorded streams remains feasible.

### Gaps
- RLR primary text not obtained. Its feature-importance findings (e.g., the relative weight of preuse distance, hits since insertion and access type) are unverified here.
- No source found that uses synthetic microbenchmarks to fingerprint simulator-level LLC policies in ChampSim, as opposed to hardware reverse engineering (CacheQuery, nanoBench-style work).
- Beckmann & Sanchez's "Modeling cache performance beyond LRU" (HPCA 2016) and Talus were not read. Their applicability to modeling arbitrary policies from reuse-distance distributions is not verified here.

## Q5. Prior work comparing two policies' behavior directly, clustering policies by behavior, and differential testing or validation of implementations and ports

### Takeaway
Direct pairwise policy comparison exists mainly in formal and worst-case form:
- Reineke & Grund's relative competitiveness ("P's misses ≤ k·Q's misses + c on every sequence").
- Sensitivity to initial state.
- Relative dominance (Dorrigiv et al.).

All of these are computed by the Relacs tool for small associativities. I found no empirical work that clusters replacement policies by decision agreement or miss-set overlap. Recent LLM-driven LLC policy search measures diversity in code space instead. CacheFit picks parents by MiniLM embedding distance of the C++ source, while explicitly citing quality-diversity search's use of behavioral diversity.

For port validation, the evidence is indirect:
- ChampSim's promise of stable module hooks.
- A GitHub issue where a configured policy silently ran as LRU.
- Parrot's practice of checking that LRU and Belady hit rates match a prior paper.
- CacheQuery's equivalence checks against reference automata.

### Cited Findings
**Formal pairwise comparison (Reineke & Grund)**
- Relative competitiveness generalizes Sleator–Tarjan competitiveness to bound one policy against another on any workload. Relacs computes relative competitiveness, sensitivity ("how strongly a policy's initial cache state affects its long-run performance") and relative dominance (Dorrigiv, López-Ortiz, Munro) — [Relacs tool page](https://embedded.cs.uni-saarland.de/relacs.php).
- Relacs supports LRU, FIFO, FWF, PLRU, Transpose, MRU, NonDet and OPT at fixed associativities — [Relacs tool page](https://embedded.cs.uni-saarland.de/relacs.php).
- Relative competitiveness appeared at LCTES 2008 and as a SIGMETRICS 2008 extended abstract. Ratios of LRU relative to FIFO and MRU yielded the first may-analyses for those policies (search snippet only) — [LCTES'08 slides](https://www.rw.cdl.uni-saarland.de/people/reineke/private/talks/relativeCompetitiveAnalysisLCTES08.pdf); [Reineke dissertation](https://www.rw.cdl.uni-saarland.de/people/reineke/private/publications/DissertationCachesInWCETAnalysis.pdf).

**Equivalence checking against a reference (CacheQuery)**
- The PLRU identification above was done "by checking its equivalence to a manually implemented PLRU automaton". Equivalence via finite conformance tests is incomplete in general — [CacheQuery](https://arxiv.org/abs/1912.09770).

**Diversity in LLM-driven LLC policy search is measured in code space**
- **CacheFit (2026 preprint).** "Quality-diversity search structures its archive by behavioral diversity and favors distant parents". CacheFit's own Feature 3 chooses Parent B to maximize 1 − cos(e(A), e(P)), "where e(·) is the MiniLM embedding of the C++ policy (all-MiniLM-L6-v2)". It notes that random selection "returns near-duplicates of A in expectation" — [CacheFit, arXiv 2610.10579](https://arxiv.org/abs/2610.10579).
- **CacheForge (2026 preprint)** keeps diversity with a dynamic temperature controller, mutation-rate control and "tree-based diversification", including crossover between "top-performing but structurally different designs". It studies how memory, temperature, mutation rate, prompt and seed diversity shape convergence among 10,687 generated policies — [CacheForge, arXiv 2610.07668](https://arxiv.org/abs/2610.07668).
- **MELC** (hackathon project) evolves LLC policies as structured descriptions that are compiled to ChampSim C++ and benchmarked against Mockingjay (search snippet only) — [MELC GitHub](https://github.com/zmkzLe/MELC).

**Implementation and port validation pitfalls**
- **ChampSim compatibility.** ChampSim states that "module hook signatures are unlikely to change (without sufficient cause) to ensure that already published artifacts remain compatible with newer versions" — [ChampSim, arXiv 2210.14324](https://arxiv.org/abs/2210.14324).
- **Silent misconfiguration.** A ChampSim GitHub issue reports that a configured LLC policy silently ran as LRU: "No matter which replacement policy I set in config.json, it defaults to LRU" (search snippet) — [ChampSim issue #596](https://github.com/ChampSim/ChampSim/issues/596).
- **Pinning and baseline checks.** Parrot pins ChampSim commit `8798bed8117b2873e34c47733d2ba4f79b6014d4` for trace generation, and validates its pipeline by matching LRU and Belady hit rates to Glider's reported numbers — [Parrot README](https://github.com/google-research/google-research/tree/master/cache_replacement); [Parrot](https://arxiv.org/abs/2006.16239).
- **Reference implementations and original conditions.**
  - Mockingjay's reference source is at github.com/ishanashah/Mockingjay. It was evaluated on "the most recent version of the ChampSim simulator" at the time (2021–22), with a 32 KB budget for all compared policies.
  - Harmony "defaults to Hawkeye" without prefetching.
  - These are the behaviors a port must reproduce.

  Source: [Mockingjay](https://www.cs.utexas.edu/~lin/papers/hpca22.pdf).
- **Evolving codebase.** Replacement-policy studies on ChampSim release their exact simulator version because "the original version is being evolved" (search snippet; a ResearchGate-hosted multicore replacement paper) — [ResearchGate](https://www.researchgate.net/publication/350485248_Near-optimal_replacement_policies_for_shared_caches_in_multicore_processors).
- **Jain & Lin's survey.** Their *Cache Replacement Policies* synthesis lecture separates coarse-grained policies from fine-grained ones that differentiate lines "based on eviction information from previous lifetimes of similar cache lines". It is a useful taxonomy for grouping evolved policies by mechanism (search snippet only) — [NSF PAR copy](https://par.nsf.gov/servlets/purl/10113803).

### Inferences
- **Relative competitiveness is worst-case and limited to small finite-state policies.** It does not scale to PC-predictor policies, but its form transfers empirically. On recorded streams, report pairwise miss ratios, with miss-set Jaccard similarity and victim agreement at a shared state, as a matrix. Cluster policies with hierarchical clustering on 1 − agreement. This would be a new contribution: I found no prior empirical policy-clustering study.
- **"Victim agreement at a shared state" needs care.** Two policies quickly reach different cache contents, so their victims are not comparable step by step.
  - Option (a): fork from a common state, such as OPT's or LRU's, and ask each policy which victim it would pick. This is counterfactual, off-policy agreement.
  - Option (b): compare outcome-level sets such as miss sets, eviction counts per PC and the reuse-distance histogram of victims, which do not require aligned states.

  Parrot's on-policy vs off-policy distinction applies.
- **For port differential testing, inputs, state and RNG must be identical.**
  - Use an identical per-set access stream.
  - Use identical initial state (Reineke & Grund's sensitivity shows initial state can matter long-term).
  - Seed any RNG (Hawkeye's and Mockingjay's sampled-set selection and hashing) identically.
  - Do lockstep comparison of victim way, insertion RRPV/ETR and predictor-table updates.
  - Divergences that remain under identical inputs indicate porting bugs. Divergences that appear only in timing mode indicate stream or timing effects (Q3).
- **Code-embedding distance (CacheFit) measures how source text differs, not what the policy does.** Behavioral distance from decision replay is the missing piece.

### Gaps
- No published empirical study found that compares two replacement policies by victim agreement, miss-set overlap, or clustering by behavior.
- No published differential-testing or port-validation methodology found for ChampSim replacement policies across ChampSim versions, and no quantification of version-to-version result drift.
- Relacs computed results (specific ratios) and Reineke & Grund's sensitivity findings for particular policies were not retrieved.
- "Rebasing"-style studies of trace or simulator conversion errors in ChampSim (e.g., IISWC 2023 work on industry traces) were not checked in this pass.
