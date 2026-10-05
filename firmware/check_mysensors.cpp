#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <string>
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
  explicit Rig(journal::MemoryFlash &flash, bool enrollment = false)
      : journal(flash), gateway(journal, radio, policy, "0123456789ABCDEF") {
    gateway.begin(true, enrollment, 208500);
    gateway.connected();
  }
  std::string drain() {
    std::string out;
    // Drain between incremental presentation ticks: sixteen devices must fit
    // the bounded serial buffer while RF continues to progress.
    for (unsigned i = 0; i < 40 || (!gateway.failed() && gateway.presentation_pending()) || gateway.output_size(); ++i) {
      assert(i < 2000);
      while (gateway.output_size()) {
        assert(gateway.output_size() <= 4096);
        out.append(gateway.output_data(), gateway.output_contiguous());
        gateway.consume_output(gateway.output_contiguous());
      }
      gateway.tick(now++);
    }
    return out;
  }
  void feed(const std::string &data) {
    size_t at = 0;
    while (at < data.size()) {
      const size_t used = gateway.feed(data.data() + at, data.size() - at);
      assert(used);
      at += used;
    }
  }
  void set(uint8_t node, uint8_t child, bool on = true, uint8_t type = 2) {
    feed(std::to_string(node) + ";" + std::to_string(child) + ";1;1;" + std::to_string(type) + ";" + (on ? "1\n" : "0\n"));
  }
  void tick() { gateway.tick(now++); }
  uint32_t next(uint8_t slot = 1, bool candidate = false) {
    uint32_t value;
    assert(journal.next_counter(slot, &value, candidate));
    return value;
  }
  void finish_enrollment() {
    radio.finish = true;
    for (unsigned i = 0; i < 60; ++i) drain();
    radio.finish = false;
    assert(!radio.running);
  }
};
static void paired(journal::MemoryFlash &flash, uint8_t count = 1) {
  journal::Journal journal(flash);
  assert(journal.open() == journal::StorageState::empty);
  assert(journal.initialize(1, 0x1000) == journal::Status::ok);
  for (uint8_t slot = 1; slot <= count; ++slot) {
    while (journal.maintenance_due()) assert(journal.maintain() == journal::Status::ok);
    assert(journal.provision(slot, {static_cast<uint32_t>(0x100000 + slot), 10, 1}) == journal::Status::ok);
    assert(journal.confirm(slot) == journal::Status::ok);
    assert(journal.set_service(slot, true) == journal::Status::ok);
    assert(journal.shutter(slot).logical_id == slot + 1);
  }
  while (journal.maintenance_due()) assert(journal.maintain() == journal::Status::ok);
}
static void has(const std::string &text, const std::string &part) {
  if (text.find(part) == std::string::npos) {
    fprintf(stderr, "Missing [%s] in [%s]\n", part.c_str(), text.c_str());
    abort();
  }
}
static void absent(const std::string &text, const std::string &part) {
  assert(text.find(part) == std::string::npos);
}

static void parser_and_discovery() {
  mysensors::Message message;
  for (const std::string bad : {"", "2;1;1;1;29", "-1;1;1;1;29;1", "256;1;1;1;29;1",
       "2;1;5;1;29;1", "2;1;1;2;29;1", "2;1;1;1;29;\t", "01a;1;1;1;29;1",
       "2;1;1;1;29;12345678901234567890123456789"})
    assert(!mysensors::decode(bad.data(), bad.size(), message));
  const std::string with_nul("2;1;1;1;29;1\0more", 17);
  assert(!mysensors::decode(with_nul.data(), with_nul.size(), message));
  const std::string good = "2;1;1;1;29;1";
  assert(mysensors::decode(good.data(), good.size(), message) && message.type == 29);
  const std::string stream = "1;255;4;0;3;" + std::string(50, 'A');
  assert(mysensors::decode(stream.data(), stream.size(), message));
  for (const std::string &bad : {
       "1;255;4;0;3;" + std::string(52, 'A'), "1;255;4;0;3;" + std::string(49, 'A'),
       "1;255;4;0;3;" + std::string(44, 'G'), "1;255;3;0;9;" + std::string(26, 'A'),
       "1;255;1;0;24;" + std::string(44, 'A')})
    assert(!mysensors::decode(bad.data(), bad.size(), message));
  journal::MemoryFlash flash;
  paired(flash, 16);
  Rig rig(flash);
  const auto first = rig.drain();
  assert(!rig.gateway.failed());
  for (unsigned node = 2; node <= 17; ++node) {
    has(first, std::to_string(node) + ";1;0;0;5;");
    has(first, std::to_string(node) + ";2;1;0;2;1\n");
  }
  absent(first, "\n1;1;0;0;5;");
  const uint32_t writes = flash.programs();
  for (const std::string line : {"0;255;3;0;2;\n", "255;255;3;0;20;\n", "2;255;3;0;19;\n",
       "2;1;2;0;29;1\n", "2;1;2;1;31;\n", "1;19;2;0;24;\n", "17;255;3;0;18;\n",
       "2;1;0;0;5;fake\n", "2;1;4;0;0;anything\n", "255;1;1;1;29;1\n",
       "2;1;1;1;29;0\n"}) {
    for (char byte : line) rig.feed(std::string(1, byte));
    rig.tick();
    rig.drain();
  }
  assert(rig.radio.starts == 0 && flash.programs() == writes);
  rig.feed(std::string(200, 'x') + "\n2;1;2;0;2;\r\n");
  has(rig.drain(), "Commande non supp");
  rig.feed("2;1;1;1;29;1;injected\n2;1;1;1;3;100\n1;19;1;1;24;write\n");
  has(rig.drain(), "Commande non supp");
  assert(!rig.radio.starts);
}

static void commands_stop_and_reconnect() {
  journal::MemoryFlash flash;
  paired(flash);
  Rig rig(flash);
  rig.drain();
  rig.set(2, 1, true, 30);
  auto out = rig.drain();
  has(out, "2;1;1;1;30;1\n");
  has(out, "2;1;1;0;31;1\n");
  assert(rig.radio.starts == 1 && rig.next() == 11);
  rig.radio.finish = true;
  out = rig.drain();
  has(out, "2;1;1;0;2;0\n");
  rig.radio.finish = false;
  rig.set(2, 1, true, 29);
  rig.tick();
  rig.feed("255;255;3;0;19;\n2;1;1;0;30;1\n2;1;1;1;31;1\n");
  rig.tick();
  assert(rig.radio.starts == 3 && rig.next() == 13);
  rig.radio.finish = true;
  rig.drain();
  rig.radio.finish = false;
  rig.set(2, 1, true, 29);
  rig.tick();
  rig.feed("2;1;1;0;30;1\n2;1;1;0;30;");
  rig.gateway.disconnected();
  assert(rig.gateway.output_size() == 0);
  rig.tick();
  const auto starts = rig.radio.starts;
  rig.gateway.connected();
  rig.feed("1\n");
  has(rig.drain(), "Commande non supp");
  assert(rig.radio.starts == starts && rig.next() == 14);
  rig.set(2, 1, true, 30);
  rig.tick();
  rig.radio.fault = true;
  has(rig.drain(), "Erreur radio");
  assert(rig.next() == 15);
}

static void fresh_lifecycle_and_restart() {
  journal::MemoryFlash flash;
  {
    Rig rig(flash, true);
    absent(rig.drain(), ";1;0;0;5;");
    assert(!rig.radio.starts);
    const auto writes = flash.programs();
    rig.feed("0;17;1;1;2;1\n255;17;1;1;2;1\n0;1;1;1;29;1\n255;1;1;1;30;1\n0;3;1;1;2;1\n255;3;1;1;2;1\n");
    rig.drain();
    assert(!rig.radio.starts && flash.programs() == writes);
    assert(!rig.journal.shutter(1).logical_id);
    rig.set(1, 17);
    rig.set(1, 17);
    rig.drain();
    assert(rig.journal.shutter(1).logical_id == 2 && rig.journal.shutter(1).attempts == 1);
    assert(rig.journal.shutter(2).state == journal::SlotState::unused);
    rig.set(2, 2);
    has(rig.drain(), "Radio occupee");
    assert(!rig.journal.shutter(1).in_service && rig.journal.shutter(1).has_candidate);
    rig.finish_enrollment();
    assert(rig.radio.starts == 2 && rig.next(1, true) == 2);
    rig.set(2, 4);
    rig.finish_enrollment();
    assert(rig.radio.starts == 4 && rig.next(1, true) == 4);
    rig.set(2, 4);
    rig.drain();
    assert(rig.radio.starts == 4 && rig.journal.shutter(1).attempts == 2);
  }
  {
    Rig rig(flash, true);
    rig.drain();
    assert(!rig.radio.starts && rig.next(1, true) == 4);
    rig.set(2, 2);
    has(rig.drain(), "2;1;0;0;5;");
    const auto primary = rig.journal.incarnation(1);
    rig.set(2, 2, false);
    rig.set(2, 3);
    rig.finish_enrollment();
    assert(rig.journal.shutter(1).replacement && !rig.journal.shutter(1).in_service);
    assert(rig.journal.incarnation(1, true) != primary);
    rig.set(2, 3, false);
    rig.drain();
    assert(!rig.journal.shutter(1).has_candidate && !rig.journal.shutter(1).in_service);
    const auto starts = rig.radio.starts;
    rig.set(2, 1, true, 29);
    rig.drain();
    assert(rig.radio.starts == starts);
    rig.set(2, 5);
    rig.drain();
    assert(rig.journal.shutter(1).logical_id == 0);
    rig.set(1, 17);
    rig.finish_enrollment();
    assert(rig.journal.shutter(1).logical_id == 3);
    rig.set(3, 2);
    has(rig.drain(), "3;1;0;0;5;");
    const auto writes = flash.programs();
    const auto before = rig.radio.starts;
    rig.feed("2;1;1;1;29;1\n2;2;1;1;2;1\n2;3;1;1;2;1\n2;4;1;1;2;1\n2;5;1;1;2;1\n");
    rig.drain();
    assert(rig.radio.starts == before && flash.programs() == writes);
    assert(rig.journal.incarnation(1) != primary);
  }
  Rig restored(flash, true);
  has(restored.drain(), "3;1;0;0;5;");
  assert(!restored.radio.starts);
}

static void decline_and_replacement_preserves_node() {
  journal::MemoryFlash flash;
  Rig rig(flash, true);
  rig.drain();
  rig.set(1, 17);
  rig.finish_enrollment();
  rig.set(1, 17, false);
  rig.drain();
  assert(rig.journal.shutter(1).state == journal::SlotState::unused);
  rig.set(1, 17);
  rig.finish_enrollment();
  rig.set(3, 2);
  rig.drain();
  assert(rig.journal.shutter(1).logical_id == 3);
  const auto epoch = rig.journal.incarnation(1);
  rig.set(3, 2, false);
  rig.set(3, 3);
  rig.finish_enrollment();
  rig.set(3, 2);
  rig.drain();
  assert(rig.journal.shutter(1).logical_id == 3 && rig.journal.shutter(1).in_service);
  assert(rig.journal.incarnation(1) != epoch);
}

static void corruption_and_slow_host() {
  journal::MemoryFlash flash;
  paired(flash);
  {
    Rig rig(flash);
    rig.drain();
    rig.set(2, 1, true, 29);
    rig.tick();
    for (unsigned i = 0; i < 1000 && !rig.gateway.failed(); ++i) rig.feed("2;1;2;0;2;\n");
    assert(rig.gateway.failed());
    rig.tick();
    assert(!rig.radio.running && rig.next() == 11);
    has(rig.drain(), "serial_overflow");
    rig.gateway.disconnected();
    rig.gateway.connected();
    rig.drain();
    assert(!rig.gateway.failed() && rig.radio.starts == 1);
  }
  flash.raw()[0] ^= 0xFF;
  const auto writes = flash.programs();
  Rig broken(flash, true);
  has(broken.drain(), "Erreur memoire");
  broken.feed("2;1;1;1;29;1\n1;17;1;1;2;1\n1;20;1;1;2;1\n");
  broken.drain();
  assert(!broken.radio.starts && flash.programs() == writes);
}

int main() {
  parser_and_discovery();
  commands_stop_and_reconnect();
  fresh_lifecycle_and_restart();
  decline_and_replacement_preserves_node();
  corruption_and_slow_host();
  puts("mysensors: virtual-node discovery, lifecycle, RF epoch isolation, restart, STOP and bounded buffers passed (simulated RF)");
}
