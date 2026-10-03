#include <cassert>
#include <vector>
#include "image_layout.h"
using namespace x2d::ota;

static void word(std::vector<uint8_t> &data, size_t at, uint32_t value) {
  for (unsigned i = 0; i < 4; ++i) data[at + i] = value >> (8 * i);
}
static bool verify(const std::vector<uint8_t> &data, const std::vector<uint8_t> &prefix,
                   uint16_t crc, size_t chunk = 251) {
  ImageVerifier verifier(prefix.data(), data.size(), crc);
  for (size_t offset = 0; offset < data.size(); offset += chunk)
    if (!verifier.add(data.data() + offset, std::min(chunk, data.size() - offset))) return false;
  return verifier.finish();
}
int main() {
  std::vector<uint8_t> image(APPLICATION_OFFSET + 512, 0xFF);
  word(image, APPLICATION_OFFSET, 0x20042000);
  word(image, APPLICATION_OFFSET + 4, FLASH_BASE + APPLICATION_OFFSET + 33);
  memcpy(image.data() + APPLICATION_OFFSET + 255, IMAGE_MARKER, sizeof(IMAGE_MARKER));
  const auto prefix = image;
  const auto crc = crc16(image.data(), image.size());
  assert(verify(image, prefix, crc)); // marker crosses read boundaries
  assert(verify(image, prefix, crc, 1));
  assert(!verify(image, prefix, crc ^ 1));
  auto bad = image;
  bad[APPLICATION_OFFSET - 1] ^= 1;
  assert(!verify(bad, prefix, crc16(bad.data(), bad.size()))); // another bootloader/layout
  bad = image;
  bad[APPLICATION_OFFSET + 255] = '?';
  assert(!verify(bad, prefix, crc16(bad.data(), bad.size()))); // foreign product
  for (auto entry : {0x10000001u, 0x101EF001u, 0x10003020u}) {
    bad = image; word(bad, APPLICATION_OFFSET + 4, entry);
    assert(!verify(bad, prefix, crc16(bad.data(), bad.size())));
  }
  bad = image; word(bad, APPLICATION_OFFSET, 0x20042008);
  assert(!verify(bad, prefix, crc16(bad.data(), bad.size())));
  ImageVerifier truncated(prefix.data(), image.size(), crc);
  assert(truncated.add(image.data(), image.size() - 1) && !truncated.finish());
  ImageVerifier oversized(prefix.data(), MAX_BLOCKS * BLOCK_BYTES + BLOCK_BYTES, crc);
  assert(!oversized.add(image.data(), image.size()));
  puts("image: readback CRC, immutable boot prefix, gateway marker and vectors passed");
}
