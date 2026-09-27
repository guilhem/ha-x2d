#pragma once

#include <ArduinoJson.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace ha_x2d {

constexpr size_t MAX_LINE_BYTES = 512;

class LineFramer {
 public:
  enum class Event { none, line, too_long };

  Event feed(char byte) {
    if (byte == '\n') {
      const bool too_long = dropping_;
      if (used_ && data_[used_ - 1] == '\r') --used_;
      length = used_;
      used_ = 0;
      dropping_ = false;
      return too_long ? Event::too_long : Event::line;
    }
    if (!dropping_) {
      if (used_ < MAX_LINE_BYTES - 1) data_[used_++] = byte;
      else dropping_ = true;
    }
    return Event::none;
  }

  void reset() {
    used_ = 0;
    dropping_ = false;
    length = 0;
  }

  const char* data() const { return data_; }
  size_t length = 0;

 private:
  char data_[MAX_LINE_BYTES - 1]{};
  size_t used_ = 0;
  bool dropping_ = false;
};

enum class Operation { none, hello, status };

struct Request {
  bool has_id = false;
  uint32_t id = 0;
  Operation op = Operation::none;
  const char* error = "invalid_request";
};

// ArduinoJson stops after one object and coalesces duplicate keys. Check that
// the entire line is one object with exactly three top-level key/value pairs.
inline bool exact_object_shape(const char* data, size_t length) {
  size_t colons = 0;
  int depth = 0;
  bool quoted = false;
  bool escaped = false;
  bool started = false;
  bool finished = false;
  for (size_t i = 0; i < length; ++i) {
    const char byte = data[i];
    if (quoted) {
      if (escaped) escaped = false;
      else if (byte == '\\') escaped = true;
      else if (byte == '"') quoted = false;
      continue;
    }
    if (byte == ' ' || byte == '\t' || byte == '\r') continue;
    if (finished) return false;
    // ArduinoJson accepts single quotes, bare keys and non-JSON integers.
    if (byte == '\'' || byte == '+' ||
        (byte >= 'A' && byte <= 'Z') || (byte >= 'a' && byte <= 'z') ||
        static_cast<unsigned char>(byte) < 0x20) return false;
    if (byte == '0' && i + 1 < length && data[i + 1] >= '0' &&
        data[i + 1] <= '9' &&
        (i == 0 || data[i - 1] < '0' || data[i - 1] > '9')) return false;
    if (byte == '"') quoted = true;
    else if (byte == '{' || byte == '[') {
      if (depth == 0) {
        if (byte != '{' || started) return false;
        started = true;
      }
      ++depth;
    } else if (byte == '}' || byte == ']') {
      if (--depth < 0) return false;
      if (depth == 0) finished = true;
    } else if (byte == ':' && depth == 1) {
      size_t j = i;
      while (j && (data[j - 1] == ' ' || data[j - 1] == '\t' ||
                   data[j - 1] == '\r')) --j;
      if (!j || data[j - 1] != '"') return false;
      ++colons;
    }
    else if (depth == 0) return false;
  }
  return finished && !quoted && colons == 3;
}

inline Request decode(const char* data, size_t length) {
  Request result;
  if (length > MAX_LINE_BYTES - 1) {
    result.error = "line_too_long";
    return result;
  }
  if (!exact_object_shape(data, length)) return result;

  JsonDocument document;
  if (deserializeJson(document, data, length,
                      DeserializationOption::NestingLimit(2)) ||
      !document.is<JsonObject>()) return result;

  JsonObject object = document.as<JsonObject>();
  if (object["id"].is<int32_t>()) {
    const int32_t id = object["id"].as<int32_t>();
    if (id > 0) {
      result.has_id = true;
      result.id = static_cast<uint32_t>(id);
    }
  }

  if (!result.has_id || object.size() != 3 || !object["v"].is<int32_t>() ||
      object["v"].as<int32_t>() != 1 || !object["op"].is<JsonString>())
    return result;

  JsonString operation = object["op"].as<JsonString>();
  const char* op = operation.c_str();
  if (memchr(op, '\0', operation.size())) return result;
  if (strcmp(op, "hello") == 0) result.op = Operation::hello;
  else if (strcmp(op, "status") == 0) result.op = Operation::status;
  else {
    result.error = "unsupported_operation";
    return result;
  }

  result.error = nullptr;
  return result;
}

struct RadioStatus {
  bool detected = false;
  uint8_t partnum = 0;
  uint8_t version = 0;
  uint8_t marcstate = 0;
};

// Returns zero only if the fixed response buffer cannot hold the JSON and LF.
inline size_t encode(const Request& request, const char* device_id,
                     const char* session, uint32_t uptime_ms,
                     RadioStatus radio, char* output, size_t capacity) {
  JsonDocument document;
  document["v"] = 1;
  if (request.has_id) document["id"] = request.id;
  else document["id"] = nullptr;
  document["ok"] = request.error == nullptr;
  if (request.error) document["error"] = request.error;
  else {
    JsonObject result = document["result"].to<JsonObject>();
    if (request.op == Operation::hello) {
      result["product"] = "ha-x2d";
      result["firmware"] = "0.1.0";
      result["device_id"] = device_id;
      result["session"] = session;
      result["max_line_bytes"] = MAX_LINE_BYTES;
      JsonArray capabilities = result["capabilities"].to<JsonArray>();
      capabilities.add("info");
      capabilities.add("status");
      capabilities.add("cc1101_probe");
    } else if (request.op == Operation::status) {
      result["uptime_ms"] = uptime_ms;
      JsonObject radio_result = result["radio"].to<JsonObject>();
      radio_result["detected"] = radio.detected;
      if (radio.detected) {
        radio_result["partnum"] = radio.partnum;
        radio_result["version"] = radio.version;
        radio_result["marcstate"] = radio.marcstate;
      } else {
        radio_result["partnum"] = nullptr;
        radio_result["version"] = nullptr;
        radio_result["marcstate"] = nullptr;
      }
      result["tx_enabled"] = false;
    }
  }

  const size_t size = measureJson(document);
  if (size + 2 > capacity || size + 1 > MAX_LINE_BYTES) return 0;
  serializeJson(document, output, capacity);
  output[size] = '\n';
  return size + 1;
}

}  // namespace ha_x2d
