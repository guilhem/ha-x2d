#include <cassert>
#include <string>
#include <vector>
#include "mysensors.h"

using namespace x2d;

struct Radio {
  bool healthy = true, running = false, stop = false, finish = false, fault = false;
  uint32_t starts = 0;
  uint8_t copies = 0;
  bool available() const { return healthy; }
  bool start_burst(const radio::Waveform &wave, uint32_t, uint32_t &) {
    assert(!running);
    copies = wave.copies();
    running = true;
    stop = false;
    ++starts;
    return true;
  }
  radio::FrameState poll_burst(uint8_t &completed) {
    assert(running);
    completed = stop ? 1 : finish ? copies : 0;
    return fault ? radio::FrameState::unknown : stop || finish ? radio::FrameState::complete : radio::FrameState::busy;
  }
  void request_stop() { stop = true; }
  void end_burst() { running = false; }
};
struct Policy {
  uint32_t entropy = 42;
  uint32_t random_u32() { return ++entropy; }
  const char *firmware() { return "test"; }
};
struct Rig {
  journal::Journal journal;
  Radio radio;
  Policy policy;
  mysensors::Gateway<Radio, Policy> gateway;
  uint32_t now = 0;
  explicit Rig(journal::MemoryFlash &flash, bool enrollment = false, uint8_t selected_slot = 1)
      : journal(flash), gateway(journal, radio, policy, "0123456789ABCDEF") {
    gateway.begin(true, enrollment, 208500, enrollment ? PairingAuthorization{selected_slot, 0x5A, 0} : PairingAuthorization{});
    gateway.connected();
  }
  std::string drain() {
    std::string out;
    while (gateway.output_size()) {
      out.append(gateway.output_data(), gateway.output_contiguous());
      gateway.consume_output(gateway.output_contiguous());
    }
    return out;
  }
  void feed(const std::string &data) {
    size_t at = 0;
    while (at < data.size()) {
      const size_t used = gateway.feed(data.data() + at, data.size() - at);
      if (!used) break;
      at += used;
    }
  }
  void tick() { gateway.tick(now++); }
  uint32_t next(uint8_t slot = 1) {
    uint32_t value;
    assert(journal.next_counter(slot, &value));
    return value;
  }
};
static void paired(journal::MemoryFlash &flash, uint8_t count = 1) {
  journal::Journal journal(flash);
  assert(journal.open() == journal::StorageState::empty);
  for (uint8_t slot = 1; slot <= count; ++slot) {
    assert(journal.provision(slot, {static_cast<uint32_t>(0x100000 + slot), 10, 1}) == journal::Status::ok);
    assert(journal.confirm(slot) == journal::Status::ok);
  }
  while (journal.maintenance_due()) assert(journal.maintain() == journal::Status::ok);
}
static void has(const std::string &text, const std::string &part) {
  if (text.find(part) == std::string::npos) {
    fprintf(stderr, "Missing [%s] in [%s]\n", part.c_str(), text.c_str());
    abort();
  }
}

static void parser_and_discovery() {
  mysensors::Message message;
  for (const std::string bad : {"", "1;1;1;1;29", "-1;1;1;1;29;1", "256;1;1;1;29;1",
       "1;1;5;1;29;1", "1;1;1;2;29;1", "1;1;1;1;29;\t", "01a;1;1;1;29;1",
       "1;1;1;1;29;12345678901234567890123456789"})
    assert(!mysensors::decode(bad.data(), bad.size(), message));
  const std::string with_nul("1;1;1;1;29;1\0more", 17);
  assert(!mysensors::decode(with_nul.data(), with_nul.size(), message));
  const std::string good = "1;1;1;1;29;1";
  assert(mysensors::decode(good.data(), good.size(), message) && message.type == 29);
  journal::MemoryFlash flash;
  paired(flash, 16);
  Rig rig(flash);
  const auto first = rig.drain();
  assert(!rig.gateway.failed());
  for (unsigned slot = 1; slot <= 16; ++slot) {
    has(first, "1;" + std::to_string(slot) + ";0;0;5;X2D 0123456789ABCDEF " + std::to_string(slot) + "\n");
    has(first, "1;" + std::to_string(slot) + ";1;0;2;1\n");
  }
  const uint32_t writes = flash.programs();
  for (const std::string line : {"0;255;3;0;2;\n", "255;255;3;0;20;\n", "1;255;3;0;19;\n",
       "1;1;2;0;29;1\n", "1;1;2;1;31;\n", "1;19;2;0;24;\n", "1;255;3;0;18;\n",
       "1;1;0;0;5;fake\n", "1;1;4;0;0;anything\n", "255;1;1;1;29;1\n",
       "1;17;1;1;2;0\n", "1;18;1;1;2;0\n", "1;1;1;1;29;0\n"}) {
    for (char byte : line) rig.feed(std::string(1, byte));  // real fragmented USB
    rig.tick();
    rig.drain();
  }
  assert(rig.radio.starts == 0 && flash.programs() == writes);
  rig.feed(std::string(200, 'x') + "\n1;1;2;0;2;\r\n");
  has(rig.drain(), "invalid_message");
  rig.feed("1;1;1;1;29;1;injected\n1;1;1;1;3;100\n1;19;1;1;24;write\n");
  rig.tick();
  has(rig.drain(), "unsupported_command");
  assert(!rig.radio.starts);
}

static void standard_policy() {
  journal::MemoryFlash flash;
  paired(flash);
  Rig rig(flash);  // TX enabled, supervised enrollment disabled
  has(rig.drain(), "ready,pos_unknown");
  const auto writes = flash.programs();
  rig.tick();
  rig.gateway.disconnected();
  rig.gateway.connected();
  rig.tick();
  rig.drain();
  rig.feed("1;17;1;1;2;1\n1;18;1;1;2;1\n");
  has(rig.drain(), "association_disabled");
  rig.tick();
  assert(rig.radio.starts == 0 && rig.next() == 10 && flash.programs() == writes);
  assert(rig.journal.shutter(2).state == journal::SlotState::unused);
  rig.radio.healthy = false;
  rig.feed("1;1;1;1;29;1\n");
  has(rig.drain(), "tx_off,pos_unknown");
  rig.tick();
  assert(rig.radio.starts == 0 && rig.next() == 10 && flash.programs() == writes);
}

static void commands_stop_and_reconnect() {
  journal::MemoryFlash flash;
  paired(flash);
  Rig rig(flash);
  rig.drain();
  rig.feed("1;1;1;1;30;1\n");
  auto out = rig.drain();
  has(out, "1;1;1;1;30;1\n");
  has(out, "1;1;1;0;31;1\n");
  assert(out.find("1;1;1;0;2;0\n") == std::string::npos);
  rig.tick();
  assert(rig.radio.starts == 1 && rig.next() == 11);
  rig.radio.finish = true;
  rig.tick();
  out = rig.drain();
  has(out, "1;1;1;0;2;0\n");
  has(out, "1:emitted");
  rig.radio.finish = false;
  rig.feed("1;1;1;0;29;1\n");
  rig.tick();
  rig.feed("1;1;1;0;30;1\n1;1;1;1;31;1\n");
  rig.tick();
  assert(rig.radio.starts == 3 && rig.next() == 13); // close in queue consumed no counter
  rig.radio.finish = true;
  rig.tick();
  has(rig.drain(), "1:pos_unknown");
  rig.radio.finish = false;
  rig.feed("1;1;1;0;29;1\n");
  rig.tick();
  rig.feed("1;1;1;0;30;1\n1;1;1;0;30;");
  rig.gateway.disconnected();
  assert(rig.gateway.output_size() == 0);
  rig.tick();
  const auto starts = rig.radio.starts;
  rig.gateway.connected();
  rig.feed("1\n"); // partial command from the previous connection was discarded
  rig.tick();
  has(rig.drain(), "invalid_message");
  assert(rig.radio.starts == starts && rig.next() == 14);
  // Uncertain TX consumes its reservation and invalidates the previous estimate.
  rig.feed("1;1;1;0;30;1\n");
  rig.tick();
  rig.radio.fault = true;
  rig.tick();
  has(rig.drain(), "rf_fault,pos_unknown");
  assert(rig.next() == 15);
}

static void supervised_second_slot() {
  journal::MemoryFlash flash;
  paired(flash);
  const std::string cover2 = "1;2;0;0;5;X2D 0123456789ABCDEF 2\n";
  const auto slot1_unchanged = [](Rig &rig) {
    uint32_t identity;
    char generation[17];
    assert(rig.journal.identity(1, &identity) && identity == 0x100001);
    assert(rig.journal.generation_hex(generation) && std::string(generation) == "0000000000000001");
    assert(rig.journal.shutter(1).state == journal::SlotState::paired);
    assert(rig.journal.shutter(1).last_command == 0 && rig.next() == 10);
  };
  {
    Rig rig(flash, true);  // Default trial still authorizes only slot 1.
    assert(rig.drain().find(cover2) == std::string::npos);
    const auto mutations = flash.mutations();
    const std::vector<uint8_t> before(flash.raw(), flash.raw() + journal::REGION_BYTES);
    rig.feed("1;17;1;1;2;1\n");
    has(rig.drain(), "2:pair_unqualified");
    rig.tick();
    assert(rig.radio.starts == 0 && flash.mutations() == mutations);
    assert(!memcmp(before.data(), flash.raw(), before.size()));
    assert(rig.journal.shutter(2).state == journal::SlotState::unused);
    slot1_unchanged(rig);
  }
  {
    Rig rig(flash, true, 2);
    assert(rig.drain().find(cover2) == std::string::npos);
    rig.feed("1;17;1;1;2;1\n");
    const auto out = rig.drain();
    has(out, "2:associating");
    assert(out.find(cover2) == std::string::npos);
    assert(rig.journal.shutter(2).state == journal::SlotState::pending);
    rig.tick();
    assert(rig.radio.starts == 1 && rig.next(2) == 2);
    rig.feed("1;18;1;1;2;1\n");
    has(rig.drain(), "radio_busy");
    rig.radio.finish = true;
    rig.tick();
    rig.now = 2001;
    rig.tick();
    rig.tick();
    const auto completed = rig.drain();
    has(completed, "2:awaiting_confirmation");
    assert(completed.find(cover2) == std::string::npos);
    assert(rig.radio.starts == 2 && rig.next(2) == 2 && !rig.radio.running);
    const auto mutations = flash.mutations();
    rig.feed("1;17;1;1;2;1\n");
    has(rig.drain(), "2:pair_counter_mismatch");
    rig.gateway.disconnected();
    rig.gateway.connected();
    assert(rig.drain().find(cover2) == std::string::npos);
    rig.now += 6000;
    rig.tick();
    assert(rig.radio.starts == 2 && flash.mutations() == mutations && rig.next(2) == 2);
    slot1_unchanged(rig);
  }
  {
    Rig rig(flash, true, 2);  // Reboot cannot replay the consumed attempt.
    assert(rig.drain().find(cover2) == std::string::npos);
    const auto mutations = flash.mutations();
    rig.tick();
    rig.feed("1;17;1;1;2;1\n");
    has(rig.drain(), "2:pair_counter_mismatch");
    rig.tick();
    assert(rig.radio.starts == 0 && flash.mutations() == mutations && rig.next(2) == 2);
    rig.feed("1;18;1;1;2;1\n");  // Only the human's confirmation presents cover 2.
    has(rig.drain(), cover2);
    assert(rig.journal.shutter(2).state == journal::SlotState::paired);
    const auto confirmed = flash.mutations();
    rig.feed("1;17;1;1;2;1\n");
    has(rig.drain(), "3:pair_unqualified");
    rig.feed("1;18;1;1;2;1\n");
    has(rig.drain(), "no_pending_association");
    rig.tick();
    assert(rig.radio.starts == 0 && flash.mutations() == confirmed && rig.next(2) == 2);
    assert(rig.journal.shutter(3).state == journal::SlotState::unused);
    slot1_unchanged(rig);
  }
  {
    Rig rig(flash);  // Ordinary firmware retains both covers and slot 1 commands.
    auto out = rig.drain();
    has(out, "1;1;0;0;5;X2D 0123456789ABCDEF 1\n");
    has(out, cover2);
    const auto mutations = flash.mutations();
    rig.tick();
    rig.gateway.disconnected();
    rig.gateway.connected();
    rig.drain();
    rig.tick();
    assert(rig.radio.starts == 0 && flash.mutations() == mutations);
    slot1_unchanged(rig);
    rig.feed("1;1;1;1;29;1\n");
    rig.tick();
    assert(rig.radio.starts == 1 && rig.next() == 11 && rig.next(2) == 2);
    rig.radio.finish = true;
    rig.tick();
    has(rig.drain(), "1:emitted");
  }
}

static void pairing_power_loss_and_corruption(uint8_t slot = 1) {
  journal::MemoryFlash flash;
  if (slot > 1) paired(flash, slot - 1);
  const auto cover = "1;" + std::to_string(slot) + ";0;0;5;X2D 0123456789ABCDEF " + std::to_string(slot) + "\n";
  {
    Rig rig(flash, true, slot);
    assert(rig.drain().find(cover) == std::string::npos);
    rig.feed("1;18;1;1;2;1\n");
    has(rig.drain(), "no_pending_association");
    rig.feed("1;17;1;1;2;1\n1;17;1;1;2;1\n1;18;1;1;2;1\n");
    auto out = rig.drain();
    has(out, "radio_busy");
    has(out, "1;17;1;0;2;0\n");
    has(out, "1;18;1;0;2;0\n");
    rig.tick();
    assert(rig.next(slot) == 2 && rig.radio.starts == 1);
  }
  {
    Rig rig(flash, true, slot);
    rig.drain();
    rig.tick();
    assert(rig.radio.starts == 0 && rig.next(slot) == 2);
    rig.feed("1;18;1;1;2;1\n");
    has(rig.drain(), cover);
    rig.feed("1;18;1;1;2;1\n");
    has(rig.drain(), "no_pending_association");
    assert(rig.radio.starts == 0);
  }
  flash.raw()[0] ^= 0xFF;
  const auto writes = flash.programs();
  Rig broken(flash, true, slot);
  has(broken.drain(), "storage_corrupt");
  broken.feed("1;1;1;1;29;1\n1;17;1;1;2;1\n1;18;1;1;2;1\n");
  broken.tick();
  has(broken.drain(), "storage_corrupt");
  assert(broken.radio.starts == 0 && flash.programs() == writes);
}

static void slow_host() {
  journal::MemoryFlash flash;
  paired(flash);
  Rig rig(flash);
  rig.drain();
  rig.feed("1;1;1;1;29;1\n");
  rig.tick();
  for (unsigned i = 0; i < 1000 && !rig.gateway.failed(); ++i)
    rig.feed("1;1;2;0;2;\n"); // host never reads; cannot block radio service
  assert(rig.gateway.failed());
  rig.tick();
  assert(!rig.radio.running && rig.next() == 11);
  has(rig.drain(), "serial_overflow");
  rig.gateway.disconnected();
  rig.gateway.connected();
  rig.drain();
  rig.tick();
  assert(!rig.gateway.failed() && rig.radio.starts == 1);
}

int main() {
  parser_and_discovery();
  standard_policy();
  commands_stop_and_reconnect();
  supervised_second_slot();
  pairing_power_loss_and_corruption();
  pairing_power_loss_and_corruption(2);
  slow_host();
  puts("mysensors: framing, discovery, echoes, STOP, reconnect, pairing and bounded buffers passed");
}
