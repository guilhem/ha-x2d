#include <cassert>
#include <string>
#include "ha_x2d/serial.h"

using namespace x2d;
int main() {
  OutputBuffer output;
  const std::string fill(OutputBuffer::CAPACITY - 3, 'x');
  assert(output.append(fill.data(), fill.size()));
  assert(!output.append("abcd", 4));
  assert(output.size() == fill.size());
  output.consume(OutputBuffer::CAPACITY - 5);
  assert(output.append("hello\n", 6));
  std::string drained;
  while (output.size()) {
    const size_t count = output.contiguous() < 2 ? output.contiguous() : 2;
    drained.append(output.data(), count);
    output.consume(count);
  }
  assert(drained == "xxhello\n");
  assert(output.append("old\n", 4));
  output.clear();
  assert(output.size() == 0 && !output.append(nullptr, 1));
  LineFramer framer;
  for (size_t i = 0; i < MAX_LINE_BYTES; ++i) assert(framer.feed('x') == LineFramer::Event::none);
  assert(framer.feed('\n') == LineFramer::Event::too_long);
  for (char c : std::string("hello\r")) framer.feed(c);
  assert(framer.feed('\n') == LineFramer::Event::line && framer.length == 5);
}
