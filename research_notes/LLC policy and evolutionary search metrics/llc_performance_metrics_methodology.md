# Performance metrics and evaluation methodology for LLC replacement policies

Scope note: compiled 2026-11-06. Primary sources fetched this session: the ArchAgent paper (arXiv 2602.22425, Feb 2026), the CRC-2 site, rules page and kit README, the CRC-1 (JWAC-1, 2010) page, the Mockingjay paper (HPCA 2022, NSF PAR copy, full text extracted), the CHROME paper (HPCA 2024, author PDF, full text extracted), the SIGARCH DPC-4 write-up, and abstract or index records for the methodology papers (Smith 1988, John 2004, Eyerman/Eeckhout, Michaud, Alameldeen and Wood, SMARTS, Qureshi MLP). I did not fetch the DIP, RRIP, SHiP, Hawkeye, Harmony, Glider or RLR papers. Claims about them appear only where a fetched paper restates them; everything else is under Gaps. The "Inferences" bullets are my reasoning, not sourced claims.

## 1. Which metrics are reported, how they are defined and aggregated, and when they disagree

### Takeaway
The standard headline metric is IPC speedup over LRU. Single-core results are aggregated with a geometric mean across traces. Multi-core results use weighted speedup (sum of IPC_shared/IPC_alone, normalized to LRU's weighted IPC), with a geometric mean across mixes. Recent championships (DPC-4) use the harmonic mean of speedups. MPKI, demand-miss ratio, prefetch-hit ratio and energy are secondary or diagnostic metrics. They can disagree with IPC because of memory-level parallelism, bypassing and streaming trade-offs, writebacks and prefetch interactions.

### Cited Findings
**Single-core headline metric**
- CRC-1 (2010) scored the single-core track as "the geometric mean of replacement algorithm speedups" and the multi-core track as "the geometric mean of weighted speedup". The page does not explicitly name LRU as the baseline, but the sample policy header is true LRU. — [JWAC-1 / CRC-1 page](https://www.jilp.org/jwac-1/)
- Mockingjay (HPCA 2022): "To evaluate performance in a single-core setting, we report IPC speedup over LRU; for multi-core settings, we report the weighted speedup over LRU." — [Mockingjay paper (NSF PAR)](https://par.nsf.gov/servlets/purl/10334308)
- ArchAgent (2026) defines the single-core metric as the geometric mean of per-workload IPC_policy/IPC_LRU. — [ArchAgent, arXiv 2602.22425](https://arxiv.org/html/2602.22425)

**Weighted speedup as defined in replacement papers**
- Mockingjay: "For each program sharing the cache, we compute its IPC in a shared environment (IPC_shared) and its IPC when executing in isolation on the same cache (IPC_single). We then compute the weighted IPC of the mix as the sum of IPC_shared/IPC_single for all benchmarks in the mix, and we normalize this weighted IPC with the weighted IPC using the LRU replacement policy." — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- The stated rationale is that weighted speedup "is commonly used to evaluate shared caches … because it measures the overall performance of the mix and avoids domination by benchmarks of high IPC." — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- CHROME (HPCA 2024) reports "the normalized weighted speedup over LRU, a measure commonly used for evaluating shared caches." Across its 150 heterogeneous 4-core mixes it reports a "geometric mean speedup of 9.6% over LRU", so the mixes are aggregated with a geometric mean. — [CHROME paper](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)
- The CRC-2 kit README says the 4-core configurations can be used "to calculate the normalized weighted speedup for multi-core system". — [CRC-2 kit README (mirror)](https://github.com/tirupatihemanth/crc)

**Variants and championship scoring**
- ArchAgent's multi-core metric differs from weighted speedup. It takes the arithmetic mean of IPC across cores, normalizes it to LRU, then takes the geometric mean across workloads. — [ArchAgent](https://arxiv.org/html/2602.22425)
- DPC-4 (HPCA 2026): a participant's slide deck, not the official rules page, gives the score as `Score = (1C.fullBW × 1C.limitBW × 4C)^(1/3)`. In that formula the single-core terms are the geometric mean of IPC speedup and the multi-core term is the geometric mean of harmonic speedup. — [DPC-4 participant slides](https://docs.google.com/presentation/d/1U0pgHNeT2ly43KxVYfMAiMNP_4N-zqja/htmlpresent). The official rules page says the score is computed from individual per-configuration scores. — [DPC-4 rules](https://sites.google.com/view/dpc4-2026/rules) (it returned 404 when fetched this session; seen only as a search snippet)

**System-level multiprogram metrics and the debate**
- Eyerman and Eeckhout (IEEE Micro 2008) argue for top-down system-level metrics. They propose STP (system throughput, the weighted-speedup form) and ANTT (average normalized turnaround time, a user-oriented metric). — [Eyerman & Eeckhout 2008 record](https://www.bohrium.com/paper-details/system-level-performance-metrics-for-multiprogram-workloads/811913960784658432-2659)
- Michaud (IEEE CAL 2013) argues that "the weighted speedup and the harmonic mean of speedups are inconsistent" because they weight benchmarks unequally, and that "the weighted speedup favors unfairness". He proposes arithmetic or harmonic means of raw IPCs instead. — [Michaud, HAL](https://hal-univ-rennes1.archives-ouvertes.fr/hal-00737044)
- Eyerman and Eeckhout (IEEE CAL 2014) rebut Michaud: weighted-IPC metrics are consistent, give each program equal importance, and raw-IPC metrics depend on the programs' inherent behavior. — [ISC record](https://search.isc.ac/inventory/49/48433.htm)
- Eyerman, Michaud and Rogiest later conclude that both weighted speedup and the harmonic mean of speedups are genuine throughput metrics under stated assumptions. — [HAL record](https://hal-univ-rennes1.archives-ouvertes.fr/hal-01087743)
- A general multicore survey notes that some researchers prefer the harmonic mean of speedups partly because weighted speedup may not capture unfairness. — [arXiv 2408.12999](https://arxiv.org/pdf/2408.12999)

**Secondary and diagnostic metrics**
- CHROME reports LLC demand-miss ratio (CHROME's is 71.1%) and an "effective prefetch hit ratio (EPHR)" as diagnostics. It also reports bypass coverage and efficiency: 41.5% of incoming blocks bypassed, of which 70.8% are never reused. — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)
- Mockingjay also reports "impact on uncore energy consumption". — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- CHROME reports area and power overheads for its RL training structures, including 7.27 mW for the EQ. — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)

**When MPKI and IPC disagree**
- *Memory-level parallelism:* Qureshi et al. (ISCA 2006) argue that "isolated misses are more costly on performance than parallel misses", so a policy that minimizes miss count without accounting for MLP is not performance-optimal. Their MLP-aware replacement improves SPEC CPU2000 performance by up to 23%. — [TR-HPS-2006-003 (UT tech report)](https://utw10235.utweb.utexas.edu/pub/TR-HPS-2006-003.pdf); [Scilit record](https://scilit.com/publications/bd40189c6cb9ec68c8a0af26d47ef576)
- *Bypass and streaming trade-off (one benchmark against the mean):* on libquantum, a less aggressive bypass would raise Mockingjay's gain from 2.5% to 8.3%, but its average improvement would fall from 5.7% to 5.2%. A per-benchmark loss was accepted for the mean. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- *Prefetch interaction:* Harmony "is an extension of Hawkeye that is superior in the presence of prefetching. In the absence of prefetching, they behave identically." Rankings can therefore flip with prefetching on or off. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- *Writebacks and bypass accounting (simulator escape):* ArchAgent's agent exploited ChampSim's lack of LLC write-bypass support. "ArchAgent won by taking advantage of the fact that a bypassed write in the LLC would disappear from the system entirely". The safety check was an assertion removed by compiler optimization. The resulting Policy12 appeared to beat Mockingjay by 3–4%. — [ArchAgent](https://arxiv.org/html/2602.22425)
- *Bandwidth:* DPC-4 evaluated a limited-bandwidth configuration alongside full bandwidth. Its keynote speaker (Akanksha Jain, Google) "advocated for evaluating all prefetching techniques within constrained-bandwidth environments". — [SIGARCH DPC-4 Part I](https://www.sigarch.org/fourth-data-prefetching-championship-part-i/). Mockingjay's and ArchAgent's baseline DRAM is 25.6 GB/s. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308); [ArchAgent](https://arxiv.org/html/2602.22425)

### Inferences
- The standard practice matches AFAR's fitness: per-trace IPC_policy/IPC_LRU, geometric mean across traces. It is what CRC-1 scored, what Mockingjay and ArchAgent report, and what CHROME used as its tuning objective (Section 5).
- Read MPKI only as a diagnostic. A candidate that cuts misses by bypassing writes, or by trading isolated misses for parallel ones, can show MPKI gains that do not reach IPC, or IPC gains that come from accounting bugs.
- Monitor total DRAM traffic (reads plus writebacks) and conservation of LLC fills, writes and evictions as sanity checks, not as fitness terms. The ArchAgent escape shows why.
- For 4-core validation, the field's convention is weighted speedup normalized to LRU, geometric mean across mixes. Reporting harmonic speedup alongside it (DPC-4's choice) answers the Michaud fairness objection cheaply, because both come from the same IPC_shared/IPC_alone numbers.

### Gaps
- I did not verify the "maximum slowdown" or unfairness metrics (e.g., the max over programs of IPC_alone/IPC_shared from the scheduling literature) in any replacement-policy paper this session. Neither Mockingjay nor CHROME reports one.
- Not verified from primary sources this session:
  - the original definitions of weighted speedup (Snavely & Tullsen, ASPLOS 2000) and harmonic speedup (Luo et al., ISPASS 2001)
  - DIP, RRIP and SHiP metric choices
  - Glider and RLR metric choices
- I found no source that quantifies how often MPKI and IPC rankings flip across replacement policies on CRC-2 traces.
- DRAM energy or traffic is rarely a headline metric in the papers fetched. Mockingjay's energy methodology details were not extracted.

## 2. Methodology norms: championships, trace selection, warmup and run lengths, multi-core mixes, budgets, prefetchers

### Takeaway
CRC-2 (2017) is the de facto standard testbed. It defines four configurations: 1-core with a 2 MB LLC and 4-core with an 8 MB shared LLC, each with and without L1/L2 prefetchers. It sets a budget of 32 KB of replacement state per core. Post-2017 papers keep those configurations but use much longer runs than the kit defaults: 200M warmup + 1B measured in Mockingjay, 50M + 200M per core in CHROME. Workloads are memory-intensive SimPoint traces filtered at LLC MPKI > 1 under LRU. Multi-core results use 100–150 random 4-core mixes.

### Cited Findings
**CRC-1 (2010, older practice)**
- Two tracks: single-core with a 1 MB LLC, and a "4-core configuration with a 4 MB shared last level cache". The simulator was a binary that could not be modified except for the replacement header file.
- Evaluation used undistributed (hidden) single-core, multi-program and multi-threaded benchmarks. Contestants could build their own traces.
- Algorithms had to fit a "fixed storage budget as specified in the competition rules"; the page gives no figure.
- Scoring was the geometric mean of speedups (single-core) and the geometric mean of weighted speedup (multi-core).
- Source for all of the above: [JWAC-1](https://www.jilp.org/jwac-1/)

**CRC-2 (2017)**
- The four configurations are 1-core/2 MB LLC/no prefetcher, 1-core/2 MB/L1-L2 data prefetchers, 4-core/8 MB shared/no prefetcher, and 4-core/8 MB/prefetchers. — [CRC-2 rules](https://crc2.ece.tamu.edu/?page_id=39)
- Further rules: "All replacement policies must contain less than or equal to 32KB worth of state per core." Policies must "use only the published simulator interface functions" and must not cheat (e.g., by pre-parsing the trace). Everyone uses the official ChampSim distribution. — [CRC-2 rules](https://crc2.ece.tamu.edu/?page_id=39)
- The CRC-2 site describes the event as a contest for the best "L2 cache replacement policy" (i.e., last-level). Jain and Lin (UT Austin, Hawkeye) won. — [CRC-2 home](https://crc2.ece.tamu.edu/)
- Kit defaults are tiny: `-warmup_instructions` "Default value is 1,000,000" and `-simulation_instructions` "Default value is 10,000,000". The kit ships five 10M-instruction example traces (four SPEC CPU2006 and one CloudSuite 2.0). — [CRC-2 kit README](https://github.com/tirupatihemanth/crc)
- Extra configurations 5 and 6 (8 MB LLC on single-core) were provided but "will not be tested in the final competition". — [CRC-2 kit README](https://github.com/tirupatihemanth/crc)
- The fetched rules page states neither the official ranking formula nor the hidden evaluation trace list (see Gaps).

**Mockingjay (HPCA 2022): the de facto post-CRC-2 template**
- ChampSim "originally released by the 2nd JILP Cache Replacement Championship".
- Machine: 352-entry ROB; 32 KB 8-way L1I/L1D; 256 KB 8-way L2; 2 MB/core 16-way LLC with 20-cycle latency and 64 MSHRs; DRAM at 800 MHz, 25.6 GB/s.
- Workloads: "the 33 memory-sensitive applications of the SPEC CPU2006, SPEC CPU2017, and GAP … memory-sensitive applications to be those that have an LLC miss per kilo instructions (MPKI) greater than 1". SimPoint is used "to generate a single sample of 1 billion instructions. We warm the cache for 200 million instructions and measure the behavior of the next 1 billion instructions."
- Also evaluated: 25 of the >1300 Qualcomm CVP-1 traces, the ones with the highest LRU LLC MPKI.
- Multi-core: "similar to that of the 2nd Cache Replacement Championship … a random set of 100 mixes … until each benchmark has executed at least 1 billion instructions. If a benchmark finishes early, it is rewound … each application is warmed up for 200M instructions."
- All policies get a 32 KB budget. Mockingjay itself uses 31.91 KB (1-core) and 127.62 KB (4-core).
- Sensitivity: various prefetchers (IP-stride, Best-Offset, IPCP, ISB); Glider at 64 KB compared against Mockingjay at 32 KB and 43 KB.
- Source for all of the above: [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)

**CHROME (HPCA 2024)**
- ChampSim "with the version released by the 1st Instruction Prefetching Championship (IPC-1)", modeling Intel Alder Lake with up to 16 cores.
- SPEC traces are the DPC-3 traces: "20 traces from 14 SPEC CPU2006 workloads and 22 traces from 13 SPEC CPU2017 workloads". GAP uses 5 algorithms × 3 graphs = 15 traces.
- Mixes are homogeneous (n copies) and heterogeneous: "150 4-core, 25 8-core, and 25 16-core heterogeneous mixes". Each core is warmed for 50M instructions, then 200M are simulated.
- CHROME's total budget is 92.7 KB, well above the CRC-2 32 KB/core convention.
- Source for all of the above: [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)

**ArchAgent (2026)**
- Uses CRC-2 configurations: a 2 MiB LLC single-core; an 8 MiB shared LLC 4-core with 48 KiB L1D, 32 KiB L1I and 512 KiB L2 per core; L1D next-line plus L2 PC-stride prefetchers when prefetching is on; DRAM 25.6 GB/s.
- Workloads: 19 memory-intensive SPEC2006 SimPoint traces with LLC MPKI > 1 under LRU, plus 11 Google Workload Traces v2 for 4-core, mapped by a DynamoRIO scheduler.
- Run lengths:

  | Policy | Search | Validation |
  |---|---|---|
  | Policy31 | "max(50M, 100M)" per trace (≈1 h per run) | 1B |
  | Policy61 | 50M | 75M |
  | Policy62 | 20M | 75M |

- Search feedback averaged the prefetch-on and prefetch-off suite scores.
- The 48 KB storage limit was enforced "largely by prompting and manual review".
- Source for all of the above: [ArchAgent](https://arxiv.org/html/2602.22425)

**DPC-3 and DPC-4 (prefetching championships, relevant as current ChampSim norms)**
- DPC-4 was held with HPCA 2026 (submissions due Dec 2025, deadline later moved to early January 2026). It allowed up to one prefetcher per level, with storage caps of 32 KB (L1D), 128 KB (L2) and 256 KB (LLC). The baseline was Berti at L1D and Pythia at L2. — [SIGARCH DPC-4 Part I](https://www.sigarch.org/fourth-data-prefetching-championship-part-i/); [DPC-4 call for submissions](https://sites.google.com/view/dpc4-2026/call-for-submissions)
- DPC-4 workloads included SPEC, Google workloads ("a better representation of this reality", described as primarily frontend-bound) and AI/ML workloads, under full- and limited-bandwidth single-core configurations plus multi-core. — [SIGARCH DPC-4 Part I](https://www.sigarch.org/fourth-data-prefetching-championship-part-i/)
- The DPC-3 trace set (SPEC 2006/2017 SimPoints) is reused by replacement papers such as CHROME. — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)

### Inferences
- How AFAR's setup compares with these norms:
  - **Trace filter:** MPKI > 1 under LRU on CRC-2 SPEC06 SimPoints matches Mockingjay's and ArchAgent's criterion.
  - **Machine:** single-core 2 MB 16-way with no prefetcher is CRC-2 configuration 1.
  - **Run length:** 20M + 50M is at the short end. Only ArchAgent's search phase is comparable (Policy61 50M, Policy62 20M), and ArchAgent then validated at 75M–1B.
  - **Confirmation runs:** 50M + 200M equals CHROME's per-core lengths but is shorter than Mockingjay's 200M + 1B.
- A credible final validation should cover all four CRC-2 configurations. At minimum add the prefetch-on single-core configuration, because rankings shift with prefetching (Harmony/Hawkeye; ArchAgent's averaging of prefetch on and off).
- Check the storage budget (32 KB/core under CRC-2) by computing the policy's state bits mechanically, not by prompting. ArchAgent shows that manual or prompt-based enforcement is the norm but weak.
- Pin the ChampSim version and configuration and report them. The fetched papers use different ChampSim lineages (CRC-2 release, "most recent" in 2022, IPC-1 release) and different cache and core parameters (e.g., L2 of 256 KB in Mockingjay versus 512 KB in ArchAgent's description). Absolute speedups are therefore not directly comparable across papers.

### Gaps
- The official CRC-2 ranking formula (e.g., whether the four configurations were combined by geometric mean, and with what weights), the hidden evaluation trace list, and the evaluation warmup and simulation lengths were not on the pages fetched.
- I could not fetch the DPC-4 official rules (404). The scoring formula comes from a participant's slides, so treat it as secondary. The DPC-4 warmup/simulation lengths and the official winner were not confirmed; a bib entry suggests "Pushing the Limits of the Berti Prefetcher" placed first, but this is unverified.
- Not verified this session:
  - the DPC-3 rules
  - the methodology of DIP (ISCA 2007), RRIP (ISCA 2010) and SHiP (MICRO 2011), which used CMP$im and older SPEC2006/server traces
  - the methodology of Hawkeye, Harmony, Glider and RLR
- No source fetched gives area or latency constraints for CRC-2 beyond the storage budget. CRC-2 imposes no latency limit on its rules page, and DPC-4 explicitly had "no constraint on design complexity".

## 3. Statistical rigor: variance, confidence intervals, sampling error, simulator sensitivity, and the means debate

### Takeaway
Replacement-policy papers almost never report confidence intervals. They report point geometric means over a fixed trace set, sometimes with per-trace bars or S-curves. The methodology literature warns that single numbers hide variance (Smith 1988; John 2004), that multithreaded simulations have run-to-run variability serious enough to reverse conclusions (Alameldeen and Wood 2003), and that sampled simulation should come with explicit confidence bounds (SMARTS). The choice of mean is itself contested.

### Cited Findings
- Smith (CACM 1988) examines "the controversy surrounding single number performance reduction" and compares arithmetic, geometric and harmonic means. His well-known recommendation is the arithmetic mean for times and the harmonic mean for rates, with the geometric mean criticized as not tracking total execution time. That recommendation is from the paper and was not re-read in full this session. — [Smith 1988 PDF](https://www.eecg.toronto.edu/~moshovos/ACA06/readings/smith-performance.pdf); [CACM listing](https://cacm.acm.org/?p=10806)
- John (2004), per an indexed abstract summary: for a metric A/B, if A is weighted equally across benchmarks the harmonic mean is correct, and if B is weighted equally the arithmetic mean is correct. The summary also concludes "geometric mean is not meaningful for aggregating performance metrics over benchmarks". This is a secondhand summary; the full text was not reached. — [bohrium record of John 2004](https://www.bohrium.com/paper-details/more-on-finding-a-single-number-to-indicate-overall-performance-of-a-benchmark-suite/812342558716854274-15481); [DOI 10.1145/991124.991126](https://doi.org/10.1145/991124.991126)
- Alameldeen and Wood (HPCA 2003): variability, the spread among performance estimates from repeated runs, is "a serious challenge" for multithreaded workloads. In one experiment it produced wrong architectural conclusions 31% of the time. The remedy is multiple runs per configuration with confidence intervals and hypothesis testing. — [Alameldeen & Wood PDF](https://www2.cs.sfu.ca/~alaa/papers/hpca03_variability.pdf); [catalogue record](https://bvbvf1.bib-bvb.de/vufind/PrimoRecord/cdi_proquest_miscellaneous_1038290335)
- SMARTS (Wunderlich et al., ISCA 2003): with systematic sampling, "CPI and energy per instruction (EPI) can be estimated to within 3% with 99.7% confidence by measuring fewer than 50 million instructions per benchmark". Microarchitectural-state warm-up adds about 2% further uncertainty. The measured average CPI error was 0.64%. — [EPFL Infoscience record](https://infoscience.epfl.ch/handle/20.500.14299/36937)
- Run-length sensitivity in practice: ArchAgent reports that "results on shorter runs did not generalize to longer, representative runs" and adopted a 100M-search / 1B-validation cascade. — [ArchAgent](https://arxiv.org/html/2602.22425)
- ArchAgent's headline gains over the prior state of the art are small: 0.737% (no prefetch) and 0.907% (prefetch). Its tuned-policy numbers are internally inconsistent: 1.467% or 2.336% over Mockingjay in Section 5.2 versus 2.374% in the abstract. — [ArchAgent](https://arxiv.org/html/2602.22425)
- Population sensitivity: Mockingjay improves 5.7% over LRU on the memory-sensitive subset but only 3.9% on the entire SPEC 2006/2017/GAP suites (Hawkeye 3.2%). The aggregate depends heavily on which traces are included. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- Hyper-parameter selection on a geometric mean: CHROME grid-searched 1,000 hyper-parameter combinations on "20 memory-intensive SPEC traces" chosen at random and picked "the optimal hyper-parameter combination that provides the most significant geometric mean performance gain over the LRU baseline". — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)

### Inferences
- **Where noise actually comes from:** single-threaded, trace-driven ChampSim is deterministic, so re-running the same trace gives zero run-to-run variance. The relevant noise is sampling error from which traces, which SimPoint, and which 50M-instruction window. Alameldeen and Wood's multi-run remedy translates into resampling traces or windows, for example bootstrap confidence intervals of the geometric mean over the 14 traces, or measuring several windows per benchmark.
- **Short windows are only rough estimates:** SMARTS's ±3% at 99.7% bound needs many samples spread across the whole program, not one contiguous 50M window. A single 50M SimPoint window is a much weaker estimate of whole-program behavior. Differences under about 1% at the geometric mean (the size of ArchAgent's headline gains) should not be treated as significant without such a check.
- **The geometric mean is defensible for ratios to a baseline:** it is the standard (CRC-1, Mockingjay, ArchAgent, CHROME) because it is invariant to which machine is the reference. Smith's and John's critiques apply mainly to summarizing absolute times or rates. Reporting the geometric mean together with per-trace min/max and a win/loss count addresses the "single number hides variance" objection.
- **Treat search scores as optimistically biased:** selecting the best of many candidates on 14 traces is the same winner's-curse setting as CHROME's 1,000-point grid search. Re-measuring the selected policy on fresh traces or windows is the standard fix.

### Gaps
- Not fetched or verified this session:
  - Hennessy & Patterson's discussion of geometric means
  - Eeckhout's book "Computer Architecture Performance Evaluation Methods" (Morgan & Claypool, 2010)
  - Fleming & Wallace 1986 ("How not to lie with statistics")
  - Citron et al.'s "geometric mean" debates
- I found no replacement-policy paper (among Mockingjay, CHROME and ArchAgent) that reports confidence intervals or significance tests.
- No source quantifies how much LLC replacement speedups change across ChampSim versions. ArchAgent mentions only that its version is unspecified.

## 4. Generalization and robustness: unseen workloads, cache sizes, core counts, prefetching, and how robust policies are ranked

### Takeaway
Strong recent papers check generalization in five ways:
- extra suites beyond SPEC06 (SPEC17, GAP, CVP-1, Google Workload Traces)
- held-out workloads (CHROME tuned on SPEC and tested on unseen GAP)
- 1-, 4-, 8- and 16-core systems
- multiple prefetchers
- per-mix win counts and sorted S-curves

The clearest warning comes from ArchAgent: the SPEC-tuned state of the art (Mockingjay) slows down Google cloud traces by about 9.5% with prefetching.

### Cited Findings
- **Unseen-workload test:** CHROME "evaluated the performance of CHROME using GAP workloads that were not used for hyper-parameter tuning". In the 4-core system it beat LRU, Hawkeye, Glider, Mockingjay and CARE by 9.5%, 6.6%, 5.9%, 4.0% and 3.0%. — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)
- **Win/loss counts and S-curves:** CHROME plots weighted speedup for "all 150 cases sorted in ascending order". It reports "For 119 out of 150 heterogeneous mixes, CHROME yields the best performance … outperforms the second-best performing scheme, Mockingjay, in 137 out of 150 cases." — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)
- **Core-count scaling:** CHROME reports results for 4, 8 and 16 cores. For heterogeneous mixes it achieves 12.9% at 8 cores and 14.4% at 16 cores, against 9.2% and 10.4% for Mockingjay. It notes "the opportunity for improvement from cache management grows with more cores". — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)
- **Extra trace suites:** Mockingjay adds 25 high-MPKI CVP-1 traces. With a prefetcher, Mockingjay gains 20.1% there versus 13.4% for Harmony; without one, 16.4% versus 12.9% for Hawkeye. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- **Single-core results and the MIN bound:** Mockingjay's single-core gain over LRU is 5.7%, "which approaches the 6.0% improvement of Belady MIN's unrealizable policy". — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- **Multi-core results:** on 100 4-core mixes with no prefetcher, Mockingjay gains 15.2% over LRU, compared with 7.6% for SHiP and 12.9% for Hawkeye. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- **Prefetcher robustness:** Mockingjay "works well for a variety of prefetchers, including … IP-Stride, Best Offset Prefetcher, and IPCP, and for irregular prefetchers, such as, ISB", and its advantage grows with prefetcher accuracy. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- **Budget sensitivity:** Mockingjay "sees marginal improvements beyond a 32KB hardware budget", and at 32 KB it beats the 64 KB Glider on all four configurations. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
- **Cloud workloads (ArchAgent):** on Google Workload Traces v2 (4-core), SHiP gains 3.003% (no prefetch) and 2.905% (prefetch). ArchAgent's Policy62 gains 6.116% and 8.227%, while Mockingjay shows a 9.538% slowdown with prefetching. — [ArchAgent](https://arxiv.org/html/2602.22425)
- **Held-out split and per-workload tuning (ArchAgent):** ArchAgent does not describe a held-out workload split: search and validation use the same SPEC and Google trace sets. Policy31-Tuned was tuned per workload, so the overfitting risk is not addressed. — [ArchAgent](https://arxiv.org/html/2602.22425)
- **Cloud heterogeneity:** at DPC-4 the keynote argued that cloud workloads are heterogeneous, so tuning for the common case and hard-coded heuristics can hurt there. The Google traces were described as "a better representation of this reality". — [SIGARCH DPC-4 Part I](https://www.sigarch.org/fourth-data-prefetching-championship-part-i/)

### Inferences
- **Generalization ladder** for an evolved policy, ordered by cost:
  1. Same 14 traces with a different or longer window: 50M + 200M, then ideally 200M + 1B.
  2. The remaining CRC-2/ArchAgent SPEC06 traces, plus SPEC17 or DPC-3 traces and GAP. These are unseen benchmarks with the same machine.
  3. Prefetch-on single-core (CRC-2 configuration 2).
  4. 4-core 8 MB mixes (CRC-2 configurations 3 and 4) with weighted and harmonic speedup over around 100 random mixes, following Mockingjay's protocol.
  5. Cloud or industry traces (Google Workload Traces, CVP-1), where SPEC-tuned policies have failed.
  6. LLC-size sensitivity (e.g., 1 MB / 4 MB, or CRC-2's unscored 8 MB single-core configurations 5 and 6).
- **Robustness metrics worth reporting with the geometric mean:**
  - per-trace win/tie/loss counts against LRU and against the strongest baseline (CHROME-style)
  - worst-case per-trace speedup, i.e., maximum slowdown versus LRU
  - a sorted S-curve
  - full-suite geometric mean in addition to the memory-intensive geometric mean, as Mockingjay reports
- **Overfitting risk:** an LLM-evolved policy selected on 14 traces carries a higher risk of trace-specific overfitting than CHROME's 1,000-point grid search. Evolved code can hard-code trace-specific heuristics, the failure the DPC-4 keynote warned about. A held-out split is therefore the most important credibility step.

### Gaps
- Not verified this session:
  - DIP, RRIP, SHiP and Hawkeye results across cache sizes and associativity (the original papers include LLC-size sweeps, but they were not fetched)
  - Glider's and RLR's unseen-workload tests
- I found no standard, named "robust ranking" scheme (e.g., Borda count or worst-case-normalized score) used by any replacement championship. Rankings in the fetched sources are always by mean speedup.

## 5. Which metrics and methods suit a cheap search fitness, and which belong in final validation

### Takeaway
Precedent supports a cheap fitness of single-core geometric-mean IPC speedup over LRU on a fixed memory-intensive trace set with short windows. ArchAgent (20M–100M search windows) and CHROME (tuning on 20 SPEC traces) both did this. Precedent also says short-window search scores do not reliably transfer. Final claims should therefore rest on longer runs, all four CRC-2 configurations, multi-core weighted and harmonic speedup, held-out suites, and explicit budget and accounting checks.

### Cited Findings
- **Search-stage practice:** ArchAgent searched with 20M (Policy62), 50M (Policy61) or "max(50M, 100M)" instruction windows (Policy31). It validated at 75M or 1B, used geometric-mean IPC speedup over LRU, and averaged prefetch-on and prefetch-off scores in its feedback. — [ArchAgent](https://arxiv.org/html/2602.22425)
- **Transfer failure:** ArchAgent found "results on shorter runs did not generalize to longer, representative runs" and moved to a 100M-search / 1B-validation cascade. — [ArchAgent](https://arxiv.org/html/2602.22425)
- **Reward hacking:** the write-bypass escape (Policy12 appearing 3–4% better than Mockingjay) was discovered only through inspection. ArchAgent also penalized code length and used a rule-based verifier for line limits. — [ArchAgent](https://arxiv.org/html/2602.22425)
- **Tuning-set versus test-set split:** CHROME selected hyper-parameters by geometric-mean gain on 20 random memory-intensive SPEC traces, then reported GAP as unseen workloads. — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)
- **Validation-stage practice:**
  - Mockingjay (2022): 200M warmup + 1B measured; 100 4-core mixes; four configurations; a fixed 32 KB budget for all compared policies. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308)
  - CHROME (2024): 50M + 200M per core; 150/25/25 mixes at 4/8/16 cores; unseen-workload test. — [CHROME](https://xiaoyang-lu.github.io/files/HPCA2024/CHROME_paper.pdf)
- **Multiprogram metric choice:** weighted speedup is standard in replacement papers, and harmonic speedup is used by DPC-4 per participant slides. — [Mockingjay](https://par.nsf.gov/servlets/purl/10334308); [DPC-4 participant slides](https://docs.google.com/presentation/d/1U0pgHNeT2ly43KxVYfMAiMNP_4N-zqja/htmlpresent). Their fairness properties are disputed. — [Michaud](https://hal-univ-rennes1.archives-ouvertes.fr/hal-00737044); [Eyerman & Eeckhout 2014](https://search.isc.ac/inventory/49/48433.htm)

### Inferences
- **Keep the search fitness as it is, with cheap guards.** Geometric-mean IPC speedup over LRU on 14 traces at 20M + 50M is in line with precedent for search. Cheap additions:
  - **Worst-case term:** a minimum per-trace speedup or a penalty for traces below about 0.99× LRU, so the geometric mean cannot buy average gains with large per-trace losses (the libquantum effect).
  - **Accounting guard:** a hard failure if LLC writebacks or DRAM writes fall below LRU's in a way that indicates dropped writes.
  - **Mechanical storage-budget check:** under 32 KB of state per core, computed rather than prompted.
  - **Prefetch configuration (optional):** if compute allows, add the prefetch-on configuration and average, as ArchAgent did. Otherwise defer it to validation.
  - **No MPKI in the fitness:** it rewards miss-count tricks that do not reach IPC, so log it for diagnosis only.
- **Treat the search fitness as a training score and re-measure before any claim.** Top candidates should be re-measured at longer windows (AFAR's 50M + 200M confirmation matches CHROME's lengths). Report rank stability: does the top-k order persist from 50M to 200M?
- **Final validation, roughly in increasing cost:**
  1. The 14 traces at 200M + 1B (Mockingjay-style), or at least 50M + 200M.
  2. Held-out traces: the other CRC-2/ArchAgent SPEC06 traces, plus SPEC17/DPC-3 and GAP.
  3. Prefetch-on single-core.
  4. 4-core 8 MB mixes, about 100 random mixes, with normalized weighted speedup plus harmonic speedup and a maximum-slowdown statistic.
  5. Google Workload Traces or CVP-1 for cloud and industry robustness.
  6. Comparison against the strongest open baselines (SHiP++, Hawkeye/Harmony, Mockingjay) at equal budget on the same ChampSim build.
- **Statistical reporting for the final claim:**
  - geometric mean with a bootstrap 95% CI over traces or mixes
  - per-trace win/loss counts versus LRU and versus Mockingjay
  - worst-case slowdown
  - an S-curve
  - full-suite and memory-intensive-subset numbers
  - pinned simulator commit and configuration
- **How to interpret small gains:** gains under about 1% over Mockingjay, the size of ArchAgent's headline numbers, should be presented as within measurement uncertainty unless the CI excludes zero.

### Gaps
- No source fetched tests a direct correlation (e.g., Spearman rank correlation) between short-window fitness and long-window or multi-core results for replacement policies. ArchAgent reports only qualitatively that short runs did not transfer.
- No source fetched recommends a specific number of traces or mixes for a target confidence level in LLC replacement studies. The 100 mixes (Mockingjay) and 150 mixes (CHROME) are conventions, not derived sample sizes.
- No official CRC-2 or DPC-4 scoring document was retrieved, so whether championships combined configurations by geometric mean, and with what weights, is not confirmed from a primary source.
