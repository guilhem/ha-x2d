#pragma once

#include "ota.h"

namespace x2d::ota {
constexpr uintptr_t FLASH_BASE = 0x10000000;
constexpr uintptr_t JOURNAL_ADDRESS = 0x101EF000;
constexpr uintptr_t STAGING_START = 0x101FF000, STAGING_END = 0x103FF000;
constexpr uint32_t FLASH_LENGTH = JOURNAL_ADDRESS - FLASH_BASE;
constexpr char IMAGE_MARKER[] = "HA-X2D YD-RP2040 OTA/1:";

inline uint32_t get_u32(const uint8_t *p) {
  return uint32_t{p[0]} | (uint32_t{p[1]} << 8) | (uint32_t{p[2]} << 16) | (uint32_t{p[3]} << 24);
}

// Validate the durable staged file, not merely the bytes seen on USB. Retaining
// the identical boot prefix also retains the bootloader and its filesystem map.
class ImageVerifier {
 public:
  ImageVerifier(const uint8_t *boot_prefix, uint32_t bytes, uint16_t crc)
      : prefix_(boot_prefix), bytes_(bytes), expected_crc_(crc) {
    valid_ = prefix_ && bytes > APPLICATION_OFFSET + 8 && bytes % BLOCK_BYTES == 0 &&
             bytes <= MAX_BLOCKS * BLOCK_BYTES;
  }
  bool add(const uint8_t *data, size_t length) {
    if (!valid_ || !data || length > bytes_ - offset_) return valid_ = false;
    for (size_t i = 0; i < length; ++i) {
      const uint32_t at = offset_ + i;
      if (at < APPLICATION_OFFSET && data[i] != prefix_[at]) return valid_ = false;
      if (at >= APPLICATION_OFFSET && at < APPLICATION_OFFSET + sizeof(vectors_))
        vectors_[at - APPLICATION_OFFSET] = data[i];
      if (at >= APPLICATION_OFFSET && !marker_) {
        matched_ = data[i] == IMAGE_MARKER[matched_] ? matched_ + 1 :
                   data[i] == IMAGE_MARKER[0] ? 1 : 0;
        marker_ = matched_ == sizeof(IMAGE_MARKER) - 1;
      }
    }
    crc_ = crc16(data, length, crc_);
    offset_ += length;
    return true;
  }
  bool finish() const {
    const uint32_t stack = get_u32(vectors_), entry = get_u32(vectors_ + 4);
    return valid_ && offset_ == bytes_ && crc_ == expected_crc_ && marker_ &&
           stack > 0x20000000 && stack <= 0x20042000 && !(stack & 7) && (entry & 1) &&
           (entry & ~uint32_t{1}) >= FLASH_BASE + APPLICATION_OFFSET &&
           (entry & ~uint32_t{1}) < FLASH_BASE + bytes_;
  }
 private:
  const uint8_t *prefix_;
  uint32_t bytes_, offset_ = 0;
  uint16_t expected_crc_, crc_ = 0xFFFF;
  uint8_t vectors_[8]{};
  size_t matched_ = 0;
  bool valid_ = false, marker_ = false;
};
}  // namespace x2d::ota
