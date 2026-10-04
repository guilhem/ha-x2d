#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

// MySensors firmware stream, owned by the same loop as the RF controller.
// Wire integers are little endian; stream payloads are hexadecimal text.
namespace x2d::ota {

constexpr uint16_t FIRMWARE_TYPE = 0x5832, VERSION = 6;
constexpr size_t BLOCK_BYTES = 16;
constexpr uint32_t APPLICATION_OFFSET = 0x3000;
constexpr uint32_t MAX_BLOCKS = 65535;
constexpr uint32_t RETRY_MS = 500;
constexpr uint8_t MAX_ATTEMPTS = 5;
constexpr uint8_t CONFIG_REQUEST = 0, CONFIG_RESPONSE = 1,
                  BLOCK_REQUEST = 2, BLOCK_RESPONSE = 3;

// begin() receives the padded image length; every write() is one sequential
// 16-byte block. finish() must read back, validate the board's image format and
// durably commit its boot command. Its successful return is irreversible:
// abort() is called only BEFORE that return, including after a failed begin.
class Storage {
 public:
  virtual ~Storage() = default;
  virtual bool begin(uint32_t bytes) = 0;
  virtual bool write(const uint8_t *block, size_t length) = 0;
  virtual bool finish(uint16_t crc) = 0;
  virtual void abort() = 0;
};

inline uint16_t crc16(const uint8_t *bytes, size_t length, uint16_t crc = 0xFFFF) {
  for (size_t i = 0; i < length; ++i) {
    crc ^= bytes[i];
    for (unsigned bit = 0; bit < 8; ++bit)
      crc = static_cast<uint16_t>((crc >> 1) ^ ((crc & 1) ? 0xA001 : 0));
  }
  return crc;
}

inline int nibble(char c) {
  return c >= '0' && c <= '9' ? c - '0' :
         c >= 'A' && c <= 'F' ? c - 'A' + 10 :
         c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}
inline bool decode_hex(const char *text, uint8_t *bytes, size_t length) {
  if (!text || strlen(text) != length * 2) return false;
  for (size_t i = 0; i < length; ++i) {
    const int hi = nibble(text[2 * i]), lo = nibble(text[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    bytes[i] = static_cast<uint8_t>((hi << 4) | lo);
  }
  return true;
}
inline void encode_hex(const uint8_t *bytes, size_t length, char *text) {
  constexpr char digits[] = "0123456789ABCDEF";
  for (size_t i = 0; i < length; ++i) {
    text[2 * i] = digits[bytes[i] >> 4];
    text[2 * i + 1] = digits[bytes[i] & 15];
  }
  text[2 * length] = 0;
}
inline uint16_t get_u16(const uint8_t *bytes) {
  return static_cast<uint16_t>(bytes[0] | (uint16_t{bytes[1]} << 8));
}
inline void put_u16(uint8_t *bytes, uint16_t value) {
  bytes[0] = static_cast<uint8_t>(value);
  bytes[1] = static_cast<uint8_t>(value >> 8);
}

// Host: bool ota_stream(uint8_t,const char*), bool ota_log(const char*),
// ota_pause(), ota_resume(), bool ota_quiescent(). pause() drops queued RF and
// settles any active frame; tick() must continue servicing RF before us.
// Only timeouts retry automatically. Malformed, unsolicited or out-of-order
// responses abort; identical previous blocks/configs neither rewrite storage
// nor extend the current block's retry deadline. No work is replayed on abort.
template<class Host> class Receiver {
 public:
  Receiver(Storage *storage, Host &host) : storage_(storage), host_(host) {}
  bool updating() const { return state_ != State::idle; }
  bool committed() const { return state_ == State::committed; }

  void receive(uint8_t type, const char *payload, uint32_t now) {
    if (committed()) return;
    if (!storage_) { fail("unavailable"); return; }
    if (type == CONFIG_RESPONSE) configure(payload, now);
    else if (type == BLOCK_RESPONSE) block(payload, now);
    else fail("stream_type");
  }

  void tick(uint32_t now, bool connected) {
    if (!updating() || committed()) return;
    if (!connected) { cancel(); return; }
    if (state_ == State::settling) {
      if (now - sent_at_ >= RETRY_MS * MAX_ATTEMPTS) { fail("radio_timeout"); return; }
      if (!host_.ota_quiescent()) return;
      began_ = true;  // even a partial/failed begin must be cleaned up
      if (!storage_->begin(uint32_t{blocks_} * BLOCK_BYTES)) { fail("begin"); return; }
      state_ = State::receiving;
      request(now);
    } else if (state_ == State::receiving && now - sent_at_ >= RETRY_MS) {
      if (attempts_ == MAX_ATTEMPTS || now - block_started_at_ >= RETRY_MS * MAX_ATTEMPTS) fail("timeout");
      else request(now);
    } else if (state_ == State::verifying) {
      if (crc_ != expected_crc_) { fail("crc"); return; }
      if (!storage_->finish(expected_crc_)) { fail("image"); return; }
      // Commit precedes notification. A lost USB/output after here cannot
      // revoke the boot command, unpause RF or prevent the board's fallback.
      state_ = State::committed;
      host_.ota_log("ota_staged");
    }
  }

  void cancel() {
    if (!updating() || committed()) return;
    if (began_) storage_->abort();
    began_ = false;
    state_ = State::idle;
    host_.ota_resume();
  }
  void invalid_message() { if (updating() && !committed()) fail("message"); }

 private:
  enum class State : uint8_t { idle, settling, receiving, verifying, committed };
  void fail(const char *reason) {
    cancel();
    char text[26];
    snprintf(text, sizeof(text), "ota_error:%s", reason);
    host_.ota_log(text);
  }
  bool emit_request() {
    uint8_t bytes[6];
    put_u16(bytes, FIRMWARE_TYPE);
    put_u16(bytes + 2, version_);
    put_u16(bytes + 4, index_);
    char text[13];
    encode_hex(bytes, sizeof(bytes), text);
    if (host_.ota_stream(BLOCK_REQUEST, text)) return true;
    cancel();
    return false;
  }
  void request(uint32_t now) {
    if (!attempts_) block_started_at_ = now;
    sent_at_ = now;
    ++attempts_;
    emit_request();
  }
  void configure(const char *payload, uint32_t now) {
    uint8_t bytes[8];
    if (!decode_hex(payload, bytes, sizeof(bytes))) { fail("config"); return; }
    const uint16_t type = get_u16(bytes), version = get_u16(bytes + 2),
                   blocks = get_u16(bytes + 4), crc = get_u16(bytes + 6);
    if (type != FIRMWARE_TYPE || uint32_t{blocks} * BLOCK_BYTES <= APPLICATION_OFFSET) {
      fail("config");
      return;
    }
    if (updating()) {
      if (version != version_ || blocks != blocks_ || crc != expected_crc_) fail("config_changed");
      else if (state_ == State::receiving) emit_request();
      return;
    }
    version_ = version;
    blocks_ = blocks;
    expected_crc_ = crc;
    index_ = attempts_ = 0;
    crc_ = 0xFFFF;
    sent_at_ = now;
    state_ = State::settling;
    host_.ota_pause();
  }
  void block(const char *payload, uint32_t now) {
    uint8_t bytes[6 + BLOCK_BYTES];
    if (state_ != State::receiving && state_ != State::verifying) { fail("unsolicited"); return; }
    if (!decode_hex(payload, bytes, sizeof(bytes))) { fail("block"); return; }
    if (get_u16(bytes) != FIRMWARE_TYPE || get_u16(bytes + 2) != version_) {
      fail("block_id");
      return;
    }
    const uint16_t index = get_u16(bytes + 4);
    if (index_ && index == index_ - 1) {
      if (memcmp(previous_, bytes + 6, BLOCK_BYTES)) fail("duplicate");
      else if (state_ == State::receiving) emit_request();
      return;
    }
    if (state_ != State::receiving || index != index_) { fail("block_order"); return; }
    if (!storage_->write(bytes + 6, BLOCK_BYTES)) { fail("write"); return; }
    memcpy(previous_, bytes + 6, BLOCK_BYTES);
    crc_ = crc16(bytes + 6, BLOCK_BYTES, crc_);
    ++index_;
    attempts_ = 0;
    if (index_ == blocks_) state_ = State::verifying;
    else request(now);
  }

  Storage *storage_;
  Host &host_;
  State state_ = State::idle;
  uint16_t version_ = 0, blocks_ = 0, expected_crc_ = 0, index_ = 0, crc_ = 0xFFFF;
  uint32_t sent_at_ = 0, block_started_at_ = 0;
  uint8_t previous_[BLOCK_BYTES]{}, attempts_ = 0;
  bool began_ = false;
};

}  // namespace x2d::ota
