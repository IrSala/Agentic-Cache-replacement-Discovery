#ifndef REPLACEMENT_MOCKINGJAY_H
#define REPLACEMENT_MOCKINGJAY_H

#include <cstdint>
#include <unordered_map>
#include <vector>

#include "cache.h"
#include "modules.h"

class mockingjay : public champsim::modules::replacement {
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
  bool is_sampled_set(long set) const;
  uint64_t crc_hash(uint64_t value) const;
  uint64_t get_pc_signature(uint64_t pc, bool hit, bool prefetch, uint32_t core) const;
  uint32_t get_sampled_cache_index(uint64_t full_addr) const;
  uint64_t get_sampled_cache_tag(uint64_t full_addr) const;
  int search_sampled_cache(uint64_t tag, uint32_t set) const;
  void detrain(uint32_t set, int way);
  int temporal_difference(int init, int sample) const;
  static int increment_timestamp(int input);
  static int time_elapsed(int global, int local);
  sampled_cache_line& sampled_line(uint32_t set, int way);
  const sampled_cache_line& sampled_line(uint32_t set, int way) const;
public:
  explicit mockingjay(CACHE* cache);
  long find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set,
                   champsim::address ip, champsim::address full_addr, access_type type);
  void update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr,
                                champsim::address ip, champsim::address victim_addr, access_type type, uint8_t hit);
};

#endif
