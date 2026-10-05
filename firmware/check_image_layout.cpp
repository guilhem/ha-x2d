#include <cassert>
#include <cstdio>
#include <vector>
#include "image_layout.h"
using namespace x2d::ota;

static void word(std::vector<uint8_t> &data, size_t at, uint32_t value) {
  for (unsigned i = 0; i < 4; ++i) data[at + i] = value >> (8 * i);
}
static bool verify(const std::vector<uint8_t> &data, const std::vector<uint8_t> &prefix,
                   uint16_t crc, size_t chunk = 251) {
  ImageVerifier verifier(prefix.data(), data.size(), crc, VERSION);
  for (size_t offset = 0; offset < data.size(); offset += chunk)
    if (!verifier.add(data.data() + offset, std::min(chunk, data.size() - offset))) return false;
  return verifier.finish();
}
int main(int argc, char **argv) {
  if (argc == 2) {
    FILE *file = fopen(argv[1], "rb");
    assert(file);
    std::vector<uint8_t> bytes;
    uint8_t buffer[256];
    size_t got;
    while ((got = fread(buffer, 1, sizeof(buffer), file)))
      bytes.insert(bytes.end(), buffer, buffer + got);
    assert(!ferror(file));
    assert(fclose(file) == 0);
    assert(bytes.size() == padded_image_size(bytes.size()));
    const auto config = running_config(bytes.data(), bytes.size());
    assert(config.blocks && verify(bytes, bytes, config.crc));
    printf("active config: %u %u %u %04X %u\n", config.type, config.version, config.blocks,
           config.crc, config.bootloader_version);
    return 0;
  }
  std::vector<uint8_t> image(APPLICATION_OFFSET + 512, 0xFF);
  word(image, APPLICATION_OFFSET, 0x20042000);
  word(image, APPLICATION_OFFSET + 4, FLASH_BASE + APPLICATION_OFFSET + 33);
  memcpy(image.data() + APPLICATION_OFFSET + 255, IMAGE_MARKER, sizeof(IMAGE_MARKER) - 1);
  memcpy(image.data() + APPLICATION_OFFSET + 255 + sizeof(IMAGE_MARKER) - 1, "7:", 2);
  const auto prefix = image;
  const auto crc = crc16(image.data(), image.size());
  assert(verify(image, prefix, crc)); // marker crosses read boundaries
  assert(verify(image, prefix, crc, 1));
  auto marker_literals = image;
  memcpy(marker_literals.data() + APPLICATION_OFFSET + 32, IMAGE_MARKER, sizeof(IMAGE_MARKER));
  assert(verify(marker_literals, prefix, crc16(marker_literals.data(), marker_literals.size()), 1));
  const auto active = running_config(image.data(), image.size() - 3);
  assert(active.blocks == image.size() / BLOCK_BYTES && active.crc == crc && active.version == VERSION);
  assert(padded_image_size(image.size()) == image.size());  // aligned images gain no page
  assert(running_config(nullptr, image.size()).blocks == 0);
  assert(running_config(image.data(), MAX_BLOCKS * BLOCK_BYTES).blocks == 0); // rounds past uint16 block bound
  assert(running_config(image.data(), JOURNAL_ADDRESS - FLASH_BASE + 1).blocks == 0);
  assert(!verify(image, prefix, crc ^ 1));
  auto bad = image;
  bad[APPLICATION_OFFSET - 1] ^= 1;
  assert(!verify(bad, prefix, crc16(bad.data(), bad.size()))); // another bootloader/layout
  bad = image;
  bad[APPLICATION_OFFSET + 255] = '?';
  assert(!verify(bad, prefix, crc16(bad.data(), bad.size()))); // foreign product
  bad = image;
  bad[APPLICATION_OFFSET + 255 + sizeof(IMAGE_MARKER) - 1] = '8';
  assert(!verify(bad, prefix, crc16(bad.data(), bad.size()))); // offer version disagrees with compiled image
  bad = image;
  bad.resize(bad.size() - BLOCK_BYTES);
  assert(!verify(bad, prefix, crc16(bad.data(), bad.size()))); // noncanonical transfer padding
  for (auto entry : {0x10000001u, 0x101EF001u, 0x10003020u}) {
    bad = image; word(bad, APPLICATION_OFFSET + 4, entry);
    assert(!verify(bad, prefix, crc16(bad.data(), bad.size())));
  }
  bad = image; word(bad, APPLICATION_OFFSET, 0x20042008);
  assert(!verify(bad, prefix, crc16(bad.data(), bad.size())));
  ImageVerifier truncated(prefix.data(), image.size(), crc, VERSION);
  assert(truncated.add(image.data(), image.size() - 1) && !truncated.finish());
  ImageVerifier oversized(prefix.data(), MAX_BLOCKS * BLOCK_BYTES + BLOCK_BYTES, crc, VERSION);
  assert(!oversized.add(image.data(), image.size()));
  puts("image: readback CRC, immutable boot prefix, gateway marker and vectors passed");
}
