#ifndef REPLACEMENT_EVOLVED_REPL_H
#define REPLACEMENT_EVOLVED_REPL_H

// LLC replacement policy for ChampSim (pinned commit 410cee6), evolved by
// OpenEvolve. Only the code between the EVOLVE-BLOCK markers may change.

#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <map>
#include <numeric>
#include <random>
#include <unordered_map>
#include <vector>

#include "cache.h"
#include "modules.h"

class evolved_repl : public champsim::modules::replacement
{
  // EVOLVE-BLOCK-START
  // Mockingjay (Shah, Jain and Lin, HPCA 2022): mockingjay-upload.h with three fixes (marked FIX),
  // which make it behave as the authors' mockingjay.llc_repl (github.com/ishanashah/Mockingjay):
  //   1. Hooks: ChampSim 410cee6 passes fills to update_replacement_state only if the policy also
  //      defines replacement_cache_fill (ChampSim issues #570, #598). So misses go through
  //      replacement_cache_fill, bypasses included, and update_replacement_state takes hits only:
  //      with a fill hook it is also called on lookup misses, which the authors' code never saw.
  //   2. Signature: the hash's low bits, as in the authors' code and the paper (11 bits here).
  //   3. Training: the step toward a new sample is at most 1, min(1, diff/16), as in both.
  // It is not a seed.
  struct sampled_cache_line { bool valid = false; uint64_t tag = 0, signature = 0; int timestamp = 0; };
  static constexpr int HISTORY = 8, GRANULARITY = 8, SAMPLED_CACHE_WAYS = 5, TIMESTAMP_BITS = 8;
  static constexpr double TEMP_DIFFERENCE = 1.0 / 16.0;
  long NUM_SET, NUM_WAY;
  unsigned LOG2_SET, LOG2_SIZE, LOG2_SAMPLED_SETS, LOG2_SAMPLED_CACHE_SETS = 4, SAMPLED_CACHE_TAG_BITS;
  int INF_RD, INF_ETR, MAX_RD;
  double FLEXMIN_PENALTY;
  std::vector<int> etr, etr_clock, current_timestamp;
  std::unordered_map<uint32_t, int> rdp;
  std::vector<sampled_cache_line> sampled_cache;

  sampled_cache_line& sampled_line(uint32_t set, int way)
  {
    return sampled_cache.at(static_cast<std::size_t>(set) * SAMPLED_CACHE_WAYS + static_cast<std::size_t>(way));
  }
  const sampled_cache_line& sampled_line(uint32_t set, int way) const
  {
    return sampled_cache.at(static_cast<std::size_t>(set) * SAMPLED_CACHE_WAYS + static_cast<std::size_t>(way));
  }

  bool is_sampled_set(long set) const
  {
    const unsigned mask_length = LOG2_SET - LOG2_SAMPLED_SETS;
    if (mask_length == 0) return true;
    const uint32_t mask = (uint32_t{1} << mask_length) - 1;
    return (static_cast<uint32_t>(set) & mask) == ((static_cast<uint32_t>(set) >> (LOG2_SET - mask_length)) & mask);
  }

  uint64_t crc_hash(uint64_t value) const
  {
    constexpr uint64_t polynomial = 3988292384ULL;
    for (unsigned i = 0; i < 3; ++i) value = (value & 1) ? ((value >> 1) ^ polynomial) : (value >> 1);
    return value;
  }

  uint64_t get_pc_signature(uint64_t pc, bool hit, bool prefetch, uint32_t core) const
  {
    const unsigned signature_bits = std::min<unsigned>(LOG2_SIZE > 10 ? LOG2_SIZE - 10 : 1, 64);
    if (NUM_CPUS == 1) pc = (pc << 1 | static_cast<uint64_t>(hit)) << 1 | static_cast<uint64_t>(prefetch);
    else pc = (pc << 1 | static_cast<uint64_t>(prefetch)) << 2 | core;
    const uint64_t hashed = crc_hash(pc); // FIX 2: keep the low bits
    return (hashed << (64 - signature_bits)) >> (64 - signature_bits);
  }

  uint32_t get_sampled_cache_index(uint64_t full_addr) const
  {
    return static_cast<uint32_t>((full_addr >> LOG2_BLOCK_SIZE) & ((uint64_t{1} << (LOG2_SAMPLED_CACHE_SETS + LOG2_SET)) - 1));
  }
  uint64_t get_sampled_cache_tag(uint64_t full_addr) const
  {
    return (full_addr >> (LOG2_SET + LOG2_BLOCK_SIZE + LOG2_SAMPLED_CACHE_SETS)) & ((uint64_t{1} << SAMPLED_CACHE_TAG_BITS) - 1);
  }
  int search_sampled_cache(uint64_t tag, uint32_t set) const
  {
    for (int way = 0; way < SAMPLED_CACHE_WAYS; ++way)
      if (sampled_line(set, way).valid && sampled_line(set, way).tag == tag) return way;
    return -1;
  }
  void detrain(uint32_t set, int way)
  {
    auto& line = sampled_line(set, way);
    if (!line.valid) return;
    const auto signature = static_cast<uint32_t>(line.signature);
    rdp[signature] = std::min(rdp.count(signature) ? rdp[signature] + 1 : INF_RD, INF_RD);
    line.valid = false;
  }

  int temporal_difference(int init, int sample) const
  {
    // FIX 3: a step of at most 1
    if (sample > init) return std::min(init + std::min(1, static_cast<int>((sample - init) * TEMP_DIFFERENCE)), INF_RD);
    if (sample < init) return std::max(init - std::min(1, static_cast<int>((init - sample) * TEMP_DIFFERENCE)), 0);
    return init;
  }
  static int increment_timestamp(int input) { return (input + 1) % (1 << TIMESTAMP_BITS); }
  static int time_elapsed(int global, int local) { return global >= local ? global - local : global + (1 << TIMESTAMP_BITS) - local; }

public:
  explicit evolved_repl(CACHE* cache)
      : replacement(cache), NUM_SET(cache->NUM_SET), NUM_WAY(cache->NUM_WAY), LOG2_SET(static_cast<unsigned>(__builtin_ctz(static_cast<unsigned>(NUM_SET)))),
        LOG2_SIZE(LOG2_SET + static_cast<unsigned>(__builtin_ctz(static_cast<unsigned>(NUM_WAY))) + static_cast<unsigned>(champsim::to_underlying(cache->OFFSET_BITS))),
        LOG2_SAMPLED_SETS(LOG2_SIZE > 16 ? LOG2_SIZE - 16 : 0),
        SAMPLED_CACHE_TAG_BITS(31 > LOG2_SIZE ? 31 - LOG2_SIZE : 1), INF_RD(static_cast<int>(NUM_WAY) * HISTORY - 1),
        INF_ETR(static_cast<int>(NUM_WAY) * HISTORY / GRANULARITY - 1), MAX_RD(INF_RD - 22),
        FLEXMIN_PENALTY(2.0 - std::log2(static_cast<double>(NUM_CPUS)) / 4.0), etr(static_cast<std::size_t>(NUM_SET * NUM_WAY)),
        etr_clock(static_cast<std::size_t>(NUM_SET), GRANULARITY), current_timestamp(static_cast<std::size_t>(NUM_SET)),
        sampled_cache((std::size_t{1} << (LOG2_SET + LOG2_SAMPLED_CACHE_SETS)) * SAMPLED_CACHE_WAYS)
  {
  }

  long find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                   champsim::address full_addr, access_type type)
  {
    for (long way = 0; way < NUM_WAY; ++way) if (!current_set[way].valid) return way;
    int max_etr = 0; long victim_way = 0;
    for (long way = 0; way < NUM_WAY; ++way) {
      const int value = etr.at(static_cast<std::size_t>(set * NUM_WAY + way));
      if (std::abs(value) > max_etr || (std::abs(value) == max_etr && value < 0)) { max_etr = std::abs(value); victim_way = way; }
    }
    const auto signature = static_cast<uint32_t>(get_pc_signature(ip.to<uint64_t>(), false, type == access_type::PREFETCH, triggering_cpu));
    if (type != access_type::WRITE && rdp.count(signature) && (rdp.at(signature) > MAX_RD || rdp.at(signature) / GRANULARITY > max_etr)) return NUM_WAY;
    return victim_way;
  }

  // FIX 1: hits arrive through update_replacement_state, misses through replacement_cache_fill
  // (way = NUM_WAY when the fill bypasses), each once, as in the authors' API.
  void update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                champsim::address victim_addr, access_type type, uint8_t hit)
  {
    if (hit) on_access(triggering_cpu, set, way, full_addr, ip, type, true);
  }

  void replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                              champsim::address victim_addr, access_type type)
  {
    on_access(triggering_cpu, set, way, full_addr, ip, type, false);
  }

  // The upload's update_replacement_state, unchanged.
  void on_access(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, access_type type, bool hit)
  {
    if (type == access_type::WRITE) { if (!hit) etr.at(static_cast<std::size_t>(set * NUM_WAY + way)) = -INF_ETR; return; }
    const auto signature = static_cast<uint32_t>(get_pc_signature(ip.to<uint64_t>(), hit, type == access_type::PREFETCH, triggering_cpu));
    const auto address = full_addr.to<uint64_t>();
    if (is_sampled_set(set)) {
      const auto sample_index = get_sampled_cache_index(address); const auto sample_tag = get_sampled_cache_tag(address);
      const int matched = search_sampled_cache(sample_tag, sample_index);
      if (matched >= 0) {
        const auto last_signature = static_cast<uint32_t>(sampled_line(sample_index, matched).signature);
        int sample = time_elapsed(current_timestamp.at(static_cast<std::size_t>(set)), sampled_line(sample_index, matched).timestamp);
        if (sample <= INF_RD) {
          if (type == access_type::PREFETCH) sample = static_cast<int>(sample * FLEXMIN_PENALTY);
          rdp[last_signature] = rdp.count(last_signature) ? temporal_difference(rdp[last_signature], sample) : sample;
          sampled_line(sample_index, matched).valid = false;
        }
      }
      int lru_way = 0, lru_rd = -1;
      for (int w = 0; w < SAMPLED_CACHE_WAYS; ++w) {
        auto& line = sampled_line(sample_index, w);
        if (!line.valid) { lru_way = w; lru_rd = INF_RD + 1; continue; }
        const int sample = time_elapsed(current_timestamp.at(static_cast<std::size_t>(set)), line.timestamp);
        if (sample > INF_RD) { lru_way = w; lru_rd = INF_RD + 1; detrain(sample_index, w); }
        else if (sample > lru_rd) { lru_way = w; lru_rd = sample; }
      }
      detrain(sample_index, lru_way);
      for (int w = 0; w < SAMPLED_CACHE_WAYS; ++w) {
        auto& line = sampled_line(sample_index, w);
        if (!line.valid) { line.valid = true; line.signature = signature; line.tag = sample_tag; line.timestamp = current_timestamp.at(static_cast<std::size_t>(set)); break; }
      }
      current_timestamp.at(static_cast<std::size_t>(set)) = increment_timestamp(current_timestamp.at(static_cast<std::size_t>(set)));
    }
    auto& clock = etr_clock.at(static_cast<std::size_t>(set));
    if (clock == GRANULARITY) {
      for (long w = 0; w < NUM_WAY; ++w) if (w != way && std::abs(etr.at(static_cast<std::size_t>(set * NUM_WAY + w))) < INF_ETR) --etr.at(static_cast<std::size_t>(set * NUM_WAY + w));
      clock = 0;
    }
    ++clock;
    if (way < NUM_WAY) {
      auto& entry = etr.at(static_cast<std::size_t>(set * NUM_WAY + way));
      if (!rdp.count(signature)) entry = NUM_CPUS == 1 ? 0 : INF_ETR;
      else entry = rdp.at(signature) > MAX_RD ? INF_ETR : rdp.at(signature) / GRANULARITY;
    }
  }
  // EVOLVE-BLOCK-END
};

#endif
