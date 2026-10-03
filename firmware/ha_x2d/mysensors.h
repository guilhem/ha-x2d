#pragma once

#include <stdio.h>
#include <string_view>
#include <x2d/controller.h>
#include "serial.h"
#include "ota.h"

// MySensors 2.x serial API, one USB gateway and one fixed virtual node.
// No MySensors radio network, state restoration, SmartSleep or JSON.
namespace x2d::mysensors {

constexpr uint8_t NODE = 1, PAIR = 17, CONFIRM = 18, DIAGNOSTIC = 19, SYSTEM = 255;
constexpr uint8_t PRESENTATION = 0, SET = 1, REQ = 2, INTERNAL = 3, STREAM = 4;
constexpr uint8_t S_BINARY = 3, S_COVER = 5, S_NODE = 17, S_CUSTOM = 23;
constexpr uint8_t V_STATUS = 2, V_VAR1 = 24, V_UP = 29, V_DOWN = 30, V_STOP = 31;
constexpr uint8_t I_VERSION = 2, I_LOG_MESSAGE = 9, I_SKETCH_NAME = 11, I_SKETCH_VERSION = 12,
                  I_REBOOT = 13, I_GATEWAY_READY = 14, I_HEARTBEAT_REQUEST = 18, I_PRESENTATION = 19,
                  I_DISCOVER = 20, I_DISCOVER_RESPONSE = 21, I_HEARTBEAT_RESPONSE = 22;
constexpr size_t PAYLOAD_BYTES = 25, STREAM_HEX_BYTES = 2 * PAYLOAD_BYTES, LINE_BYTES = 96;

inline bool valid_payload(uint8_t command, const char *text, size_t length) {
  if (command != STREAM) return length <= PAYLOAD_BYTES;
  if (length > STREAM_HEX_BYTES || length % 2) return false;
  for (size_t i = 0; i < length; ++i) if (ota::nibble(text[i]) < 0) return false;
  return true;
}

struct Message {
  uint8_t node = 0, child = 0, command = 0, echo = 0, type = 0;
  char payload[STREAM_HEX_BYTES + 1]{};
};

inline bool decode(const char *bytes, size_t length, Message &message) {
  if (!bytes || length >= LINE_BYTES) return false;
  uint8_t fields[5]{};
  size_t pos = 0;
  for (auto &field : fields) {
    unsigned value = 0, digits = 0;
    while (pos < length && bytes[pos] >= '0' && bytes[pos] <= '9') {
      if (++digits > 3) return false;
      value = value * 10 + bytes[pos++] - '0';
    }
    if (!digits || value > 255 || pos == length || bytes[pos++] != ';') return false;
    field = static_cast<uint8_t>(value);
  }
  if (fields[2] > STREAM || fields[3] > 1 ||
      !valid_payload(fields[2], bytes + pos, length - pos)) return false;
  for (size_t i = pos; i < length; ++i)
    if (bytes[i] < 32 || bytes[i] > 126) return false;
  message = {fields[0], fields[1], fields[2], fields[3], fields[4], {}};
  memcpy(message.payload, bytes + pos, length - pos);
  return true;
}

// Policy supplies random_u32() and firmware(). Radio supplies available() and
// the RadioRuntime backend. All methods, including transport callbacks, belong
// to the main loop. An output overflow fails this connection closed; tick()
// cancels its work outside observer callbacks and still services the radio.
template<class Radio, class Policy> class Gateway {
 public:
  Gateway(journal::Journal &journal, Radio &radio, Policy &policy, const char *device_id,
          ota::Storage *storage = nullptr)
      : policy_(policy), controller_(journal, radio, *this), ota_(storage, *this) {
    invalid_ = !hex16(device_id);
    if (!invalid_) memcpy(device_id_, device_id, sizeof(device_id_));
  }

  bool begin(bool transmit, bool enrollment, uint32_t chip_ns, PairingAuthorization authorization = {}) {
    return controller_.begin(transmit && !invalid_, enrollment, chip_ns, authorization);
  }
  void connected() {
    if (connected_ || invalid_) return;
    connected_ = true;
    if (!updating()) controller_.resume();
    send(0, SYSTEM, INTERNAL, 0, I_GATEWAY_READY, "Gateway startup complete.");
    present();
  }
  void disconnected() {
    if (!connected_ && !failed_) return;
    connected_ = false;  // cancellation callbacks must not publish old replies
    ota_.cancel();  // a committed boot command survives connection loss
    controller_.pause();
    input_.reset();
    output_.clear();
    failed_ = stopped_ = false;
    // A USB interruption cannot vouch for the resulting motor position.
    memset(positions_, 0, sizeof(positions_));
    status(controller_.valid() ? "disconnected,pos_unknown" : "storage_corrupt", 0);
  }
  void tick(uint32_t now) {
    now_ = now;
    if (failed_ && !stopped_) {
      stopped_ = true;
      controller_.pause();
    }
    controller_.tick(now);
    ota_.tick(now, connected_ && !failed());
  }
  size_t feed(const char *bytes, size_t length) {
    if (!connected_ || failed() || !bytes) return 0;
    // At most one bounded line per loop iteration, even under a USB flood.
    for (size_t i = 0; i < length; ++i) {
      const auto event = input_.feed(bytes[i]);
      if (event == LineFramer::Event::none) continue;
      Message message;
      if (event == LineFramer::Event::too_long || !decode(input_.data(), input_.length, message)) {
        ota_.invalid_message();
        status("invalid_message", 0);
      } else dispatch(message);
      return i + 1;
    }
    return length;
  }
  bool failed() const { return invalid_ || failed_; }
  bool updating() const { return ota_.updating(); }
  bool ota_committed() const { return ota_.committed(); }
  bool reboot_requested() const { return ota_committed() && reboot_requested_; }
  size_t output_size() const { return output_.size(); }
  size_t output_contiguous() const { return output_.contiguous(); }
  const char *output_data() const { return output_.data(); }
  void consume_output(size_t length) { output_.consume(length); }

  uint32_t random_u32() { return policy_.random_u32(); }
  void status(const char *message, uint8_t slot) {
    // Keep the public diagnostic inside the standard 25-byte payload. These
    // names retain the cause without exposing radio identities or counters.
    const std::string_view reason{message};
    if (reason == "association_profile_unqualified") message = "pair_unqualified";
    else if (reason == "association_counter_mismatch") message = "pair_counter_mismatch";
    else if (reason == "multiple_pending_associations") message = "multiple_pending";
    else if (reason == "identity_generation_failed") message = "identity_failed";
    else if (reason == "transmission_disabled") message = "tx_off,pos_unknown";
    else if (reason == "radio_unavailable") message = "radio_off,pos_unknown";
    else if (reason == "ready") message = "ready,pos_unknown";
    else if (reason == "association_pending") message = "pair_pending,pos_unknown";
    else if (reason == "paired") message = "paired,pos_unknown";
    char unknown[PAYLOAD_BYTES + 1];
    if (terminal_unknown_) {
      if (!strcmp(message, "emitted")) message = "pos_unknown";
      else {
        const char *cause = !strcmp(message, "radio_fault") ? "rf_fault" :
                            !strcmp(message, "incomplete_burst") ? "incomplete" :
                            !strcmp(message, "stop_preempted") ? "preempted" :
                            !strcmp(message, "session_disconnected") ? "usb_lost" :
                            !strcmp(message, "queue_cancelled") ? "cancelled" : "expired";
        snprintf(unknown, sizeof(unknown), "%s,pos_unknown", cause);
        message = unknown;
      }
      terminal_unknown_ = false;
    }
    if (slot) snprintf(diagnostic_, sizeof(diagnostic_), "%u:%.*s", slot, slot < 10 ? 23 : slot < 100 ? 22 : 21, message);
    else snprintf(diagnostic_, sizeof(diagnostic_), "%s", message);
    value(DIAGNOSTIC, V_VAR1, diagnostic_);
  }
  void tx_result(const radio::TxEvent &event) {
    const uint8_t slot = event.job.shutter_id;
    if (event.job.enrollment || slot < 1 || slot > MAX_SHUTTERS) return;
    if (!strcmp(event.outcome, "emitted")) {
      positions_[slot - 1] = event.job.action == Action::open ? 1 : event.job.action == Action::close ? 2 : 0;
    } else if (strcmp(event.outcome, "rejected") || event.completed_copies) {
      positions_[slot - 1] = 0;
    }
    terminal_unknown_ = !positions_[slot - 1] && (strcmp(event.outcome, "rejected") || event.completed_copies);
    // Failure diagnostics keep their cause; the state snapshot still uses the
    // documented open/unknown convention. No V_PERCENTAGE is ever emitted.
    if (controller_.paired(slot)) snapshot(slot);
  }

 private:
  friend class ota::Receiver<Gateway>;
  bool ota_stream(uint8_t type, const char *payload) {
    return send(NODE, SYSTEM, STREAM, 0, type, payload);
  }
  bool ota_log(const char *payload) {
    return send(NODE, SYSTEM, INTERNAL, 0, I_LOG_MESSAGE, payload);
  }
  void ota_pause() { controller_.pause("ota_busy"); }
  void ota_resume() { if (connected_ && !failed()) controller_.resume(); }
  bool ota_quiescent() const { return !controller_.active(); }

  bool send(uint8_t node, uint8_t child, uint8_t command, uint8_t echo,
            uint8_t type, const char *payload) {
    if (!connected_ || failed()) return false;
    char line[LINE_BYTES];
    const int size = snprintf(line, sizeof(line), "%u;%u;%u;%u;%u;%s\n", node, child, command, echo, type, payload);
    if (!valid_payload(command, payload, strlen(payload)) || size <= 0 || static_cast<size_t>(size) >= sizeof(line) ||
        !output_.append(line, static_cast<size_t>(size))) {
      failed_ = true;
      output_.clear();
      // A bounded final diagnostic; no reentrant runtime calls here.
      constexpr char fault[] = "1;19;1;0;24;serial_overflow\n";
      output_.append(fault, sizeof(fault) - 1);
      return false;
    }
    return true;
  }
  void value(uint8_t child, uint8_t type, const char *payload, uint8_t echo = 0) {
    send(NODE, child, SET, echo, type, payload);
  }
  const char *state(uint8_t slot) const { return positions_[slot - 1] == 2 ? "0" : "1"; }
  void snapshot(uint8_t slot) {
    // STOP is asserted BEFORE any command echoes can arrive, so native HA
    // never mistakes an echoed UP/DOWN for measured motion.
    value(slot, V_STOP, "1");
    value(slot, V_UP, "0");
    value(slot, V_DOWN, "0");
    value(slot, V_STATUS, state(slot));
  }
  void present_cover(uint8_t slot) {
    char name[PAYLOAD_BYTES + 1];
    snprintf(name, sizeof(name), "X2D %s %u", device_id_, slot);
    send(NODE, slot, PRESENTATION, 0, S_COVER, name);
    snapshot(slot);
  }
  void present() {
    send(NODE, SYSTEM, PRESENTATION, 0, S_NODE, "2.3.2");
    send(NODE, SYSTEM, INTERNAL, 0, I_SKETCH_NAME, "X2D USB");
    send(NODE, SYSTEM, INTERNAL, 0, I_SKETCH_VERSION, policy_.firmware());
    for (uint8_t slot = 1; slot <= MAX_SHUTTERS; ++slot)
      if (controller_.paired(slot)) present_cover(slot);
    send(NODE, PAIR, PRESENTATION, 0, S_BINARY, "X2D Pair shutter");
    send(NODE, CONFIRM, PRESENTATION, 0, S_BINARY, "X2D Confirm pairing");
    send(NODE, DIAGNOSTIC, PRESENTATION, 0, S_CUSTOM, "X2D Diagnostic");
    value(PAIR, V_STATUS, "0");
    value(CONFIRM, V_STATUS, "0");
    value(DIAGNOSTIC, V_VAR1, diagnostic_);
  }
  void dispatch(const Message &message) {
    const auto &m = message;
    if (m.node == NODE && m.child == SYSTEM && m.command == STREAM) {
      if (m.type == ota::CONFIG_REQUEST && !*m.payload) {
        char id[PAYLOAD_BYTES + 1];
        snprintf(id, sizeof(id), "ota_id:%s", device_id_);
        if (!ota_log(id)) return;
        uint8_t bytes[8]{};
        ota::put_u16(bytes, ota::FIRMWARE_TYPE);
        ota::put_u16(bytes + 2, ota::VERSION);
        char config[17];
        ota::encode_hex(bytes, sizeof(bytes), config);
        ota_stream(ota::CONFIG_REQUEST, config);
      } else ota_.receive(m.type, m.payload, now_);
      return;
    }
    if (m.command == INTERNAL && m.child == SYSTEM &&
        (m.node == 0 || m.node == NODE || m.node == 255)) {
      if (m.type == I_VERSION) send(m.node == 0 ? 0 : NODE, SYSTEM, INTERNAL, 0, I_VERSION, "2.3.2");
      else if (m.type == I_REBOOT && m.node == NODE && !*m.payload && ota_committed()) reboot_requested_ = true;
      else if (m.type == I_DISCOVER) send(NODE, SYSTEM, INTERNAL, 0, I_DISCOVER_RESPONSE, "0");
      else if (m.type == I_PRESENTATION) present();
      else if (m.type == I_HEARTBEAT_REQUEST) {
        char time[11];
        snprintf(time, sizeof(time), "%lu", static_cast<unsigned long>(now_));
        send(NODE, SYSTEM, INTERNAL, 0, I_HEARTBEAT_RESPONSE, time);
      }
      return;
    }
    if (m.node != NODE) return;
    const bool cover = m.child >= 1 && m.child <= MAX_SHUTTERS;
    const bool button = m.child == PAIR || m.child == CONFIRM;
    if (m.command == REQ) {
      // Always reply with SET; never request an actuator's persisted HA value.
      if (cover && controller_.paired(m.child) && (m.type == V_STATUS || m.type == V_UP || m.type == V_DOWN || m.type == V_STOP))
        value(m.child, m.type, m.type == V_STATUS ? state(m.child) : m.type == V_STOP ? "1" : "0", m.echo);
      else if (button && m.type == V_STATUS) value(m.child, V_STATUS, "0", m.echo);
      else if (m.child == DIAGNOSTIC && m.type == V_VAR1) value(DIAGNOSTIC, V_VAR1, diagnostic_, m.echo);
      else status("unsupported_read", m.child);
      return;
    }
    if (m.command != SET) return;
    if (updating()) { status("ota_busy", m.child); return; }
    const bool action = cover && (m.type == V_UP || m.type == V_DOWN || m.type == V_STOP);
    const bool valid = (action || (button && m.type == V_STATUS)) &&
                       (!strcmp(m.payload, "1") || !strcmp(m.payload, "0"));
    if (!valid) {
      status("unsupported_command", m.child);
      if (button) value(m.child, V_STATUS, "0");
      return;
    }
    // Receipt, not RF success. The controller publishes refusals and terminal
    // outcomes independently. An overflow never admits a new radio request.
    if (m.echo && !send(NODE, m.child, SET, 1, m.type, m.payload)) return;
    if (action) {
      if (!strcmp(m.payload, "1")) {
        const Action command = m.type == V_UP ? Action::open : m.type == V_DOWN ? Action::close : Action::stop;
        if (controller_.command(m.child, command, now_)) {
          if (command == Action::stop) positions_[m.child - 1] = 0;
          status("accepted", m.child);
        }
      }
      if (controller_.paired(m.child)) snapshot(m.child);
    } else {
      if (!strcmp(m.payload, "1")) {
        if (m.child == PAIR) controller_.associate(now_);
        else {
          const uint8_t slot = controller_.pending_slot();
          if (controller_.confirm()) present_cover(slot);
        }
      }
      value(m.child, V_STATUS, "0");
    }
  }

  Policy &policy_;
  Controller<Radio, Gateway> controller_;
  ota::Receiver<Gateway> ota_;
  LineFramer input_{LINE_BYTES};
  OutputBuffer output_;
  char device_id_[17]{}, diagnostic_[PAYLOAD_BYTES + 1]{};
  uint8_t positions_[MAX_SHUTTERS]{};  // 0 unknown, 1 assumed open, 2 assumed closed
  uint32_t now_ = 0;
  bool reboot_requested_ = false;
  bool connected_ = false, invalid_ = false, failed_ = false, stopped_ = false, terminal_unknown_ = false;
};

}  // namespace x2d::mysensors
