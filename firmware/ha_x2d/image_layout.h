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

inline uint32_t padded_image_size(uint32_t bytes) {
  return (bytes + IMAGE_ALIGNMENT - 1) & ~uint32_t{IMAGE_ALIGNMENT - 1};
}
// Read the bytes actually in flash, including FF padding written by both UF2
// and OTA. No embedded CRC/size metadata feeds back into the checksum.
inline FirmwareConfig running_config(const uint8_t *flash, uint32_t bytes,
                                     uint16_t version = VERSION) {
  const uint32_t padded = padded_image_size(bytes);
  if (!flash || bytes <= APPLICATION_OFFSET + 8 || bytes > MAX_BLOCKS * BLOCK_BYTES ||
      padded > MAX_BLOCKS * BLOCK_BYTES ||
      FLASH_BASE + padded > JOURNAL_ADDRESS) return {FIRMWARE_TYPE, version, 0, 0, BOOTLOADER_VERSION};
  return {FIRMWARE_TYPE, version, static_cast<uint16_t>(padded / BLOCK_BYTES),
          crc16(flash, padded), BOOTLOADER_VERSION};
}

// Validate the durable staged file, not merely the bytes seen on USB. Retaining
// the identical boot prefix also retains the bootloader and its filesystem map.
class ImageVerifier {
 public:
  ImageVerifier(const uint8_t *boot_prefix, uint32_t bytes, uint16_t crc, uint16_t version)
      : prefix_(boot_prefix), bytes_(bytes), expected_crc_(crc), expected_version_(version) {
    valid_ = prefix_ && bytes > APPLICATION_OFFSET + 8 && bytes % BLOCK_BYTES == 0 &&
             bytes <= MAX_BLOCKS * BLOCK_BYTES && bytes % IMAGE_ALIGNMENT == 0 &&
             FLASH_BASE + ((bytes + 4095) & ~uint32_t{4095}) <= JOURNAL_ADDRESS;
  }
  bool add(const uint8_t *data, size_t length) {
    if (!valid_ || !data || length > bytes_ - offset_) return valid_ = false;
    for (size_t i = 0; i < length; ++i) {
      const uint32_t at = offset_ + i;
      if (at < APPLICATION_OFFSET && data[i] != prefix_[at]) return valid_ = false;
      if (at >= APPLICATION_OFFSET && at < APPLICATION_OFFSET + sizeof(vectors_))
        vectors_[at - APPLICATION_OFFSET] = data[i];
      if (at >= APPLICATION_OFFSET && marker_ && !identity_) {
        if (data[i] >= '0' && data[i] <= '9' && digits_ < 5) {
          image_version_ = image_version_ * 10 + data[i] - '0';
          ++digits_;
        } else if (data[i] == ':' && digits_ && image_version_ == expected_version_) identity_ = true;
        else {
          // The executable also contains the verifier's plain marker literal.
          // Only a marker followed by a complete matching version is identity.
          marker_ = false;
          matched_ = digits_ = image_version_ = 0;
        }
      }
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
    return valid_ && offset_ == bytes_ && crc_ == expected_crc_ && identity_ &&
           stack > 0x20000000 && stack <= 0x20042000 && !(stack & 7) && (entry & 1) &&
           (entry & ~uint32_t{1}) >= FLASH_BASE + APPLICATION_OFFSET &&
           (entry & ~uint32_t{1}) < FLASH_BASE + bytes_;
  }
 private:
  const uint8_t *prefix_;
  uint32_t bytes_, offset_ = 0;
  uint16_t expected_crc_, crc_ = 0xFFFF;
  uint16_t expected_version_;
  uint32_t image_version_ = 0;
  uint8_t digits_ = 0;
  uint8_t vectors_[8]{};
  size_t matched_ = 0;
  bool valid_ = false, marker_ = false, identity_ = false;
};
}  // namespace x2d::ota
