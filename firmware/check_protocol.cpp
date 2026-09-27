#include "ha_x2d/protocol.h"

#include <assert.h>
#include <string.h>

int main() {
  ha_x2d::LineFramer framer;
  const char* hello = "{\"v\":1,\"id\":7,\"op\":\"hello\"}\r\n";
  ha_x2d::LineFramer::Event event = ha_x2d::LineFramer::Event::none;
  for (const char* p = hello; *p; ++p) event = framer.feed(*p);
  assert(event == ha_x2d::LineFramer::Event::line);
  auto request = ha_x2d::decode(framer.data(), framer.length);
  assert(request.error == nullptr && request.id == 7 &&
         request.op == ha_x2d::Operation::hello);
  char reply[ha_x2d::MAX_LINE_BYTES];
  size_t length = ha_x2d::encode(request, "0123456789ABCDEF", "FEDCBA9876543210",
                                 0, {}, reply, sizeof(reply));
  assert(length > 0 && length <= ha_x2d::MAX_LINE_BYTES && reply[length - 1] == '\n');
  JsonDocument doc;
  assert(!deserializeJson(doc, reply, length - 1));
  assert(doc["result"]["capabilities"].size() == 3);
  assert(doc["result"]["device_id"] == "0123456789ABCDEF");

  const char* invalid[] = {
      "{\"v\":1,\"id\":true,\"op\":\"hello\"}",
      "{\"v\":1,\"id\":1.5,\"op\":\"hello\"}",
      "{\"v\":1,\"id\":0,\"op\":\"hello\"}",
      "{\"v\":1,\"id\":2147483648,\"op\":\"hello\"}",
      "{\"v\":1,\"id\":1,\"op\":\"hello\",\"extra\":0}",
      "{\"v\":1,\"id\":1,\"op\":\"hello\",\"op\":\"status\"}",
      "{v:1,id:2,op:'status'}",
      "{'v':1,'id':2,'op':'status'}",
      "{\"v\":1,\"id\":2,\"op\":'status'}",
      "{\"v\":1,\"id\":+2,\"op\":\"status\"}",
      "{\"v\":1,\"id\":02,\"op\":\"status\"}",
      "{\"v\":1,\"id\":1,\"op\":\"hello\\u0000tx\"}",
      "{\"v\":1,\"id\":1,\"op\\u0000tx\":\"hello\"}",
      "{\"v\\u0000tx\":1,\"id\":1,\"op\":\"hello\"}",
      "{\"v\":1,\"id\\u0000tx\":1,\"op\":\"hello\"}",
      "{\"v\":1,\"id\":1,\"op\":\"hello\"} trailing",
      "{\"v\":1,\"id\":1,\"op\":\"hello\"}{\"v\":1}",
      "{\"v\":1,\"id\":1,\"op\":\"hello\"",
      "[1,2,3]",
      "{\"v\":1,\"id\":1,\"op\":{\"deep\":[]}}",
  };
  for (const char* value : invalid)
    assert(ha_x2d::decode(value, strlen(value)).error != nullptr);

  const char* unsupported = "{\"v\":1,\"id\":9,\"op\":\"tx\"}";
  request = ha_x2d::decode(unsupported, strlen(unsupported));
  assert(request.has_id && request.id == 9 &&
         strcmp(request.error, "unsupported_operation") == 0);

  const char* status = "{\"v\":1,\"id\":8,\"op\":\"status\"}";
  request = ha_x2d::decode(status, strlen(status));
  assert(request.error == nullptr && request.op == ha_x2d::Operation::status);
  length = ha_x2d::encode(request, "0123456789ABCDEF", "FEDCBA9876543210",
                          UINT32_MAX, {}, reply, sizeof(reply));
  assert(!deserializeJson(doc, reply, length - 1));
  assert(doc["result"]["radio"]["detected"] == false);
  assert(doc["result"]["radio"]["partnum"].isNull());
  assert(doc["result"]["tx_enabled"] == false);

  ha_x2d::RadioStatus present{true, 0, 0x14, 1};
  length = ha_x2d::encode(request, "0123456789ABCDEF", "FEDCBA9876543210",
                          1, present, reply, sizeof(reply));
  assert(!deserializeJson(doc, reply, length - 1));
  assert(doc["result"]["radio"]["detected"] == true);
  assert(doc["result"]["radio"]["version"] == 0x14);

  framer.reset();
  for (size_t i = 0; i < 512; ++i) assert(framer.feed('x') == ha_x2d::LineFramer::Event::none);
  assert(framer.feed('\n') == ha_x2d::LineFramer::Event::too_long);
  for (const char* p = hello; *p; ++p) event = framer.feed(*p);
  assert(event == ha_x2d::LineFramer::Event::line);
  assert(ha_x2d::decode(framer.data(), framer.length).error == nullptr);
  framer.reset();
  for (size_t i = 0; i < 511; ++i) assert(framer.feed(' ') == ha_x2d::LineFramer::Event::none);
  assert(framer.feed('\n') == ha_x2d::LineFramer::Event::line);
  assert(framer.length == 511);
  framer.reset();
  assert(framer.feed('{') == ha_x2d::LineFramer::Event::none);
  framer.reset();  // USB disconnect drops an incomplete request.
  for (const char* p = hello; *p; ++p) event = framer.feed(*p);
  assert(ha_x2d::decode(framer.data(), framer.length).error == nullptr);
}
