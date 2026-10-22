#ifndef REPLACEMENT_EVOLVED_REPL_H
#define REPLACEMENT_EVOLVED_REPL_H

// LLC replacement policy for ChampSim (pinned commit 410cee6), evolved by
// OpenEvolve. The seed is a header-only port of replacement/lru.
// Only the code between the EVOLVE-BLOCK markers may change.

#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <map>
#include <numeric>
#include <unordered_map>
#include <vector>

#include "cache.h"
#include "modules.h"

class evolved_repl : public champsim::modules::replacement
{
  // EVOLVE-BLOCK-START
  long NUM_WAY;
  std::vector<uint64_t> last_used_cycles;
  uint64_t cycle = 0;

public:
  explicit evolved_repl(CACHE* cache) : evolved_repl(cache, cache->NUM_SET, cache->NUM_WAY) {}

  evolved_repl(CACHE* cache, long sets, long ways)
      : replacement(cache), NUM_WAY(ways), last_used_cycles(static_cast<std::size_t>(sets * ways), 0)
  {
  }

  long find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                   champsim::address full_addr, access_type type)
  {
    auto begin = std::next(std::begin(last_used_cycles), set * NUM_WAY);
    auto end = std::next(begin, NUM_WAY);

    // Find the way whose last use cycle is most distant
    auto victim = std::min_element(begin, end);
    assert(begin <= victim);
    assert(victim < end);
    return std::distance(begin, victim);
  }

  void replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address victim_addr,
                              access_type type)
  {
    // Mark the way as being used on the current cycle
    last_used_cycles.at((std::size_t)(set * NUM_WAY + way)) = cycle++;
  }

  void update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                champsim::address victim_addr, access_type type, uint8_t hit)
  {
    // Mark the way as being used on the current cycle
    if (hit && access_type{type} != access_type::WRITE) // Skip this for writeback hits
      last_used_cycles.at((std::size_t)(set * NUM_WAY + way)) = cycle++;
  }
  // EVOLVE-BLOCK-END
};

#endif
