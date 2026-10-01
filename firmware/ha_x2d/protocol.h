#pragma once

#include <ArduinoJson.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

namespace ha_x2d {
constexpr size_t MAX_LINE_BYTES = 4096;
constexpr uint8_t MAX_SHUTTERS = 16;
constexpr uint8_t PROTOCOL_VERSION = 2;

// Whole lines enter atomically; USB drains only the currently writable bytes.
// A slow host must not turn Serial.write() into a wait on STOP's path.
class OutputBuffer {
 public:
  static constexpr size_t CAPACITY = MAX_LINE_BYTES * 2;
  bool append(const char* data, size_t length) {
    if (!data || !length || length > CAPACITY - used_) return false;
    const size_t tail = (head_ + used_) % CAPACITY;
    const size_t first = length < CAPACITY - tail ? length : CAPACITY - tail;
    memcpy(bytes_ + tail, data, first);
    memcpy(bytes_, data + first, length - first);
    used_ += length;
    return true;
  }
  const char* data() const { return bytes_ + head_; }
  size_t contiguous() const {
    return used_ < CAPACITY - head_ ? used_ : CAPACITY - head_;
  }
  void consume(size_t length) {
    if (length > used_) length = used_;
    head_ = (head_ + length) % CAPACITY;
    used_ -= length;
  }
  void clear() { head_ = used_ = 0; }
  size_t size() const { return used_; }
 private:
  char bytes_[CAPACITY]{};
  size_t head_ = 0, used_ = 0;
};

class LineFramer {
 public:
  explicit LineFramer(size_t limit = MAX_LINE_BYTES)
      : limit_(limit > 1 && limit <= MAX_LINE_BYTES ? limit : MAX_LINE_BYTES) {}
  enum class Event { none, line, too_long };
  Event feed(char byte) {
    if (byte == '\n') {
      const bool overflow = dropping_;
      if (used_ && data_[used_ - 1] == '\r') --used_;
      length = used_; used_ = 0; dropping_ = false;
      return overflow ? Event::too_long : Event::line;
    }
    if (!dropping_) {
      if (used_ < limit_ - 1) data_[used_++] = byte;
      else dropping_ = true;
    }
    return Event::none;
  }
  void reset() { used_ = length = 0; dropping_ = false; }
  const char* data() const { return data_; }
  size_t length = 0;
 private:
  char data_[MAX_LINE_BYTES - 1]{};
  const size_t limit_;
  size_t used_ = 0;
  bool dropping_ = false;
};

enum class Operation { none, hello, status, shutters, provision, pair, confirm, command };
enum class Action : uint8_t { none, open, close, stop };
struct Request {
  bool has_id = false;
  uint32_t id = 0;
  Operation op = Operation::none;
  uint8_t shutter_id = 0;
  Action action = Action::none;
  char session[17]{};
  const char* error = "invalid_request";
};
struct RadioStatus {
  bool detected = false;
  uint8_t partnum = 0, version = 0, marcstate = 0;
};

inline bool hex16(const char* value) {
  if (!value || strlen(value) != 16) return false;
  for (size_t i = 0; i < 16; ++i)
    if (!((value[i] >= '0' && value[i] <= '9') || (value[i] >= 'A' && value[i] <= 'F'))) return false;
  return true;
}

// ArduinoJson permits bare/single-quoted keys and coalesces duplicate keys.
// Requests contain only two object levels and integer/string values; compare
// colon counts to parsed sizes to reject duplicates at both trust boundaries.
inline bool strict_shape(const char* bytes, size_t length, size_t top, size_t args) {
  size_t colons[3]{};
  int depth = 0;
  bool quoted = false, escaped = false, started = false, finished = false;
  for (size_t i = 0; i < length; ++i) {
    const unsigned char c = bytes[i];
    if (quoted) {
      if (c < 0x20) return false;
      if (escaped) escaped = false;
      else if (c == '\\') escaped = true;
      else if (c == '"') quoted = false;
      continue;
    }
    if (c == ' ' || c == '\t' || c == '\r') continue;
    if (finished) return false;
    if (c == '"') { if (!depth) return false; quoted = true; }
    else if (c == '{') {
      if (!depth) { if (started) return false; started = true; }
      if (++depth > 2) return false;
    } else if (c == '}') {
      if (--depth < 0) return false;
      if (!depth) finished = true;
    } else if (c == ':') {
      if (!depth) return false;
      size_t j = i;
      while (j && (bytes[j-1] == ' ' || bytes[j-1] == '\t' || bytes[j-1] == '\r')) --j;
      if (!j || bytes[j-1] != '"') return false;
      ++colons[depth];
    } else if (c >= '0' && c <= '9') {
      if (!depth) return false;
      if (c == '0' && i + 1 < length && bytes[i+1] >= '0' && bytes[i+1] <= '9' &&
          (i == 0 || bytes[i-1] < '0' || bytes[i-1] > '9')) return false;
    } else if (c != ',' && c != '-') return false;
  }
  return finished && !quoted && colons[1] == top && colons[2] == args;
}

inline Request decode(const char* bytes, size_t length) {
  Request r;
  if (length >= MAX_LINE_BYTES) { r.error = "line_too_long"; return r; }
  JsonDocument doc;
  if (deserializeJson(doc, bytes, length, DeserializationOption::NestingLimit(2)) ||
      !doc.is<JsonObject>()) return r;
  const JsonObject obj = doc.as<JsonObject>();
  const JsonObject args = obj["args"].as<JsonObject>();
  if (!strict_shape(bytes, length, obj.size(), args.size())) return r;
  if (obj["id"].is<int32_t>() && obj["id"].as<int32_t>() > 0) {
    r.has_id = true; r.id = obj["id"].as<uint32_t>();
  }
  if (!r.has_id || !obj["v"].is<int>() || obj["v"].as<int>() != PROTOCOL_VERSION ||
      !obj["op"].is<const char*>()) return r;
  const char* op = obj["op"];
  if (!strcmp(op, "hello")) {
    if (obj.size() != 3) return r;
    r.op = Operation::hello; r.error = nullptr; return r;
  }
  if (obj.size() != 5 || !obj["session"].is<const char*>() ||
      !hex16(obj["session"]) || !obj["args"].is<JsonObject>()) return r;
  strcpy(r.session, obj["session"]);
  if (!strcmp(op, "status") || !strcmp(op, "shutters")) {
    if (args.size()) return r;
    r.op = !strcmp(op, "status") ? Operation::status : Operation::shutters;
  } else {
    if (!strcmp(op, "provision")) r.op = Operation::provision;
    else if (!strcmp(op, "pair")) r.op = Operation::pair;
    else if (!strcmp(op, "confirm")) r.op = Operation::confirm;
    else if (!strcmp(op, "command")) r.op = Operation::command;
    else { r.error = "unsupported_operation"; return r; }
    if (args.size() != (r.op == Operation::command ? 2u : 1u) ||
        !args["shutter_id"].is<int>() || args["shutter_id"].as<int>() < 1 ||
        args["shutter_id"].as<int>() > MAX_SHUTTERS) { r.op = Operation::none; return r; }
    r.shutter_id = args["shutter_id"];
    if (r.op == Operation::command) {
      if (!args["action"].is<const char*>()) { r.op = Operation::none; return r; }
      const char* action = args["action"];
      if (!strcmp(action, "open")) r.action = Action::open;
      else if (!strcmp(action, "close")) r.action = Action::close;
      else if (!strcmp(action, "stop")) r.action = Action::stop;
      else { r.op = Operation::none; return r; }
    }
  }
  r.error = nullptr;
  return r;
}
}  // namespace ha_x2d
