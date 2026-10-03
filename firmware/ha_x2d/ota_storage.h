#pragma once

#include <LittleFS.h>
#include <PicoOTA.h>
#include "image_layout.h"

extern uint8_t _FS_start, _FS_end, __flash_binary_end;

namespace x2d::rp2040 {
inline bool valid_layout() {
  return reinterpret_cast<uintptr_t>(&_FS_start) == ota::STAGING_START &&
         reinterpret_cast<uintptr_t>(&_FS_end) == ota::STAGING_END &&
         *reinterpret_cast<const uint32_t *>(ota::FLASH_BASE + ota::APPLICATION_OFFSET - 4) == ota::FLASH_LENGTH &&
         reinterpret_cast<uintptr_t>(&__flash_binary_end) <= ota::JOURNAL_ADDRESS;
}

// Dedicated LittleFS staging partition. The raw X2D journal precedes it and is
// outside every filesystem erase/program operation and OTA destination range.
class OTAStorage final : public ota::Storage {
 public:
  bool begin(uint32_t bytes) override {
    if (committed_) return false;
    file_.close();
    if (bytes <= ota::APPLICATION_OFFSET + 8 || bytes > ota::MAX_BLOCKS * ota::BLOCK_BYTES)
      return false;
    if (!valid_layout()) return false;
    // Formatting an absent/corrupt staging FS is safe: it contains no identities.
    if (!LittleFS.begin() || !discard()) return false;
    file_ = LittleFS.open("firmware.bin", "w");
    expected_ = bytes;
    return static_cast<bool>(file_);
  }
  bool write(const uint8_t *block, size_t length) override {
    return file_ && file_.size() <= expected_ && length <= expected_ - file_.size() &&
           file_.write(block, length) == length;
  }
  bool finish(uint16_t crc) override {
    if (!file_ || file_.size() != expected_) return false;
    // Core 6.1.1 programs a whole 4KiB sector even for the last short read.
    // Pad explicitly so its final copy never writes stale bytes from a prior read.
    const uint32_t padded = (expected_ + 4095) & ~uint32_t{4095};
    if (ota::FLASH_BASE + padded > ota::JOURNAL_ADDRESS) return false;
    uint8_t tail[256];
    memset(tail, 0xFF, sizeof(tail));
    while (file_.size() < padded) {
      const size_t length = padded - file_.size() < sizeof(tail) ? padded - file_.size() : sizeof(tail);
      if (file_.write(tail, length) != length) return false;
    }
    file_.close();
    File verify = LittleFS.open("firmware.bin", "r");
    if (!verify || verify.size() != padded) return false;
    ota::ImageVerifier check(reinterpret_cast<const uint8_t *>(ota::FLASH_BASE), expected_, crc);
    uint8_t buffer[256];
    uint32_t checked = 0;
    while (checked < expected_) {
      const size_t wanted = expected_ - checked < sizeof(buffer) ? expected_ - checked : sizeof(buffer);
      const int length = verify.read(buffer, wanted);
      if (length != static_cast<int>(wanted) || !check.add(buffer, wanted)) return false;
      checked += wanted;
    }
    while (verify.available()) {
      const int length = verify.read(buffer, sizeof(buffer));
      if (length <= 0) return false;
      for (int i = 0; i < length; ++i) if (buffer[i] != 0xFF) return false;
    }
    verify.close();
    if (!check.finish()) return false;
    picoOTA.begin();
    // Never rewrite the ROM's boot2, OTA bootloader or partition table. A power
    // loss while copying the application leaves the recovery loader intact.
    if (!picoOTA.addFile("firmware.bin", ota::APPLICATION_OFFSET,
                        ota::FLASH_BASE + ota::APPLICATION_OFFSET,
                        padded - ota::APPLICATION_OFFSET) || !picoOTA.commit() ||
        !verify_command(padded)) return false;
    committed_ = true;
    return true;
  }
  void abort() override {
    if (committed_) return;
    file_.close();
    expected_ = 0;
    discard();
  }
 private:
  bool discard() {
    // A pending command must be removed BEFORE its source file is touched.
    if (LittleFS.exists(_OTA_COMMAND_FILE) && !LittleFS.remove(_OTA_COMMAND_FILE)) return false;
    return !LittleFS.exists("firmware.bin") || LittleFS.remove("firmware.bin");
  }
  bool verify_command(uint32_t padded) {
    File command = LittleFS.open(_OTA_COMMAND_FILE, "r");
    OTACmdPage page;
    if (!command || command.size() != sizeof(page) ||
        command.read(reinterpret_cast<uint8_t *>(&page), sizeof(page)) != sizeof(page)) return false;
    command.close();
    OTACRC32 crc;
    crc.add(&page, offsetof(OTACmdPage, crc32));
    const auto &entry = page.cmd[0];
    return !memcmp(page.sign, "Pico OTA", 8) && page.count == 1 && page.crc32 == crc.get() &&
           entry.command == _OTA_WRITE && !strncmp(entry.write.filename, "firmware.bin", 64) &&
           entry.write.fileOffset == ota::APPLICATION_OFFSET &&
           entry.write.flashAddress == ota::FLASH_BASE + ota::APPLICATION_OFFSET &&
           entry.write.fileLength == padded - ota::APPLICATION_OFFSET;
  }
  File file_;
  uint32_t expected_ = 0;
  bool committed_ = false;
};
}  // namespace x2d::rp2040
