#include "mockingjay.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdlib>

#include "champsim.h"

mockingjay::mockingjay(CACHE* cache)
    : replacement(cache), NUM_SET(cache->NUM_SET), NUM_WAY(cache->NUM_WAY), LOG2_SET(static_cast<unsigned>(__builtin_ctz(static_cast<unsigned>(NUM_SET)))),
      LOG2_SIZE(LOG2_SET + static_cast<unsigned>(__builtin_ctz(static_cast<unsigned>(NUM_WAY))) + static_cast<unsigned>(champsim::to_underlying(cache->OFFSET_BITS))),
      LOG2_SAMPLED_SETS(LOG2_SIZE > 16 ? LOG2_SIZE - 16 : 0),
      SAMPLED_CACHE_TAG_BITS(31 > LOG2_SIZE ? 31 - LOG2_SIZE : 1), INF_RD(static_cast<int>(NUM_WAY) * HISTORY - 1),
      INF_ETR(static_cast<int>(NUM_WAY) * HISTORY / GRANULARITY - 1), MAX_RD(INF_RD - 22),
      FLEXMIN_PENALTY(2.0 - std::log2(static_cast<double>(NUM_CPUS)) / 4.0), etr(static_cast<std::size_t>(NUM_SET * NUM_WAY)),
      etr_clock(static_cast<std::size_t>(NUM_SET), GRANULARITY), current_timestamp(static_cast<std::size_t>(NUM_SET)),
      sampled_cache((std::size_t{1} << (LOG2_SET + LOG2_SAMPLED_CACHE_SETS)) * SAMPLED_CACHE_WAYS)
{}

mockingjay::sampled_cache_line& mockingjay::sampled_line(uint32_t set, int way)
{
  return sampled_cache.at(static_cast<std::size_t>(set) * SAMPLED_CACHE_WAYS + static_cast<std::size_t>(way));
}
const mockingjay::sampled_cache_line& mockingjay::sampled_line(uint32_t set, int way) const
{
  return sampled_cache.at(static_cast<std::size_t>(set) * SAMPLED_CACHE_WAYS + static_cast<std::size_t>(way));
}

bool mockingjay::is_sampled_set(long set) const
{
  const unsigned mask_length = LOG2_SET - LOG2_SAMPLED_SETS;
  if (mask_length == 0) return true;
  const uint32_t mask = (uint32_t{1} << mask_length) - 1;
  return (static_cast<uint32_t>(set) & mask) == ((static_cast<uint32_t>(set) >> (LOG2_SET - mask_length)) & mask);
}

uint64_t mockingjay::crc_hash(uint64_t value) const
{
  constexpr uint64_t polynomial = 3988292384ULL;
  for (unsigned i = 0; i < 3; ++i) value = (value & 1) ? ((value >> 1) ^ polynomial) : (value >> 1);
  return value;
}

uint64_t mockingjay::get_pc_signature(uint64_t pc, bool hit, bool prefetch, uint32_t core) const
{
  const unsigned signature_bits = std::min<unsigned>(LOG2_SIZE > 10 ? LOG2_SIZE - 10 : 1, 64);
  if (NUM_CPUS == 1) pc = (pc << 1 | static_cast<uint64_t>(hit)) << 1 | static_cast<uint64_t>(prefetch);
  else pc = (pc << 1 | static_cast<uint64_t>(prefetch)) << 2 | core;
  return crc_hash(pc) >> (64 - signature_bits);
}

uint32_t mockingjay::get_sampled_cache_index(uint64_t full_addr) const
{
  return static_cast<uint32_t>((full_addr >> LOG2_BLOCK_SIZE) & ((uint64_t{1} << (LOG2_SAMPLED_CACHE_SETS + LOG2_SET)) - 1));
}
uint64_t mockingjay::get_sampled_cache_tag(uint64_t full_addr) const
{
  return (full_addr >> (LOG2_SET + LOG2_BLOCK_SIZE + LOG2_SAMPLED_CACHE_SETS)) & ((uint64_t{1} << SAMPLED_CACHE_TAG_BITS) - 1);
}
int mockingjay::search_sampled_cache(uint64_t tag, uint32_t set) const
{
  for (int way = 0; way < SAMPLED_CACHE_WAYS; ++way)
    if (sampled_line(set, way).valid && sampled_line(set, way).tag == tag) return way;
  return -1;
}
void mockingjay::detrain(uint32_t set, int way)
{
  auto& line = sampled_line(set, way);
  if (!line.valid) return;
  const auto signature = static_cast<uint32_t>(line.signature);
  rdp[signature] = std::min(rdp.count(signature) ? rdp[signature] + 1 : INF_RD, INF_RD);
  line.valid = false;
}

long mockingjay::find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set,
                             champsim::address ip, champsim::address full_addr, access_type type)
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

int mockingjay::temporal_difference(int init, int sample) const
{
  if (sample > init) return std::min(init + static_cast<int>((sample - init) * TEMP_DIFFERENCE), INF_RD);
  if (sample < init) return std::max(init - static_cast<int>((init - sample) * TEMP_DIFFERENCE), 0);
  return init;
}
int mockingjay::increment_timestamp(int input) { return (input + 1) % (1 << TIMESTAMP_BITS); }
int mockingjay::time_elapsed(int global, int local) { return global >= local ? global - local : global + (1 << TIMESTAMP_BITS) - local; }

void mockingjay::update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                          champsim::address victim_addr, access_type type, uint8_t hit)
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
