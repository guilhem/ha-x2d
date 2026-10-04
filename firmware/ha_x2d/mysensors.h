#pragma once

#include <stdio.h>
#include <string_view>
#include <x2d/controller.h>
#include "serial.h"
#include "ota.h"

// MySensors 2.x serial API: a manager and one virtual node per logical shutter.
// No MySensors radio network, state restoration, SmartSleep or JSON.
namespace x2d::mysensors {

constexpr uint8_t NODE = 1, ADD = 17, DIAGNOSTIC = 19, INITIALIZE = 20, SYSTEM = 255;
constexpr uint8_t COVER = 1, SERVICE = 2, ASSOCIATION = 3, RETRY = 4, RETIRE = 5, STATE = 6;
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

// Policy supplies random_u32() and firmware(). Everything belongs to the main
// loop. Presentations are streamed one line per tick so 16 device pages cannot
// fill the bounded output buffer or delay the radio. No cached HA actuator value
// is ever requested or used to recreate an operation.
template<class Radio, class Policy> class Gateway {
 public:
  Gateway(journal::Journal &journal, Radio &radio, Policy &policy, const char *device_id,
          ota::Storage *storage = nullptr)
      : journal_(journal), policy_(policy), controller_(journal, radio, *this), ota_(storage, *this) {
    invalid_ = !hex16(device_id);
    if (!invalid_) memcpy(device_id_, device_id, sizeof(device_id_));
  }
  bool begin(bool transmit, bool enrollment, uint32_t chip_ns, EnrollmentProfile profile = {}) {
    return controller_.begin(transmit && !invalid_, enrollment, chip_ns, profile);
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
    connected_ = false;
    ota_.cancel();
    controller_.pause();
    input_.reset();
    output_.clear();
    presentation_ = updates_ = discoveries_ = heartbeats_ = 0;
    present_slot_ = SYSTEM;
    failed_ = stopped_ = false;
    memset(positions_, 0, sizeof(positions_));
    status("session_disconnected", 0);
  }
  void tick(uint32_t now) {
    now_ = now;
    if (failed_ && !stopped_) {
      stopped_ = true;
      controller_.pause();
    }
    controller_.tick(now);
    ota_.tick(now, connected_ && !failed());
    if (!connected_ || failed() || ota_committed()) return;
    // Reserve room for a complete state snapshot and a command receipt. A slow
    // reader defers discovery; malformed/request floods still fail closed.
    if (OutputBuffer::CAPACITY - output_.size() < SNAPSHOT_HEADROOM) return;
    if (updates_) { update_step(); return; }
    if (updating()) return;
    if (discoveries_) { internal_step(discoveries_, I_DISCOVER_RESPONSE); return; }
    if (heartbeats_) { internal_step(heartbeats_, I_HEARTBEAT_RESPONSE); return; }
    presentation_step();
  }
  size_t feed(const char *bytes, size_t length) {
    if (!connected_ || failed() || !bytes) return 0;
    for (size_t i = 0; i < length; ++i) {
      const auto event = input_.feed(bytes[i]);
      if (event == LineFramer::Event::none) continue;
      Message message;
      if (event == LineFramer::Event::too_long || !decode(input_.data(), input_.length, message)) {
        ota_.invalid_message();
        status("invalid_message", 0);
      } else dispatch(message);
      return i + 1;  // service RF between every incoming request
    }
    return length;
  }
  bool failed() const { return invalid_ || failed_; }
  bool updating() const { return ota_.updating(); }
  bool ota_committed() const { return ota_.committed(); }
  bool reboot_requested() const { return ota_committed() && reboot_requested_; }
  bool presentation_pending() const {
    return presentation_ || updates_ || discoveries_ || heartbeats_ || present_slot_ != SYSTEM;
  }
  size_t output_size() const { return output_.size(); }
  size_t output_contiguous() const { return output_.contiguous(); }
  const char *output_data() const { return output_.data(); }
  void consume_output(size_t length) { output_.consume(length); }

  uint32_t random_u32() { return policy_.random_u32(); }
  void status(const char *message, uint8_t slot) {
    if (drop_event_status_) { drop_event_status_ = false; return; }
    if (slot > MAX_SHUTTERS) slot = 0;
    snprintf(reasons_[slot], sizeof(reasons_[slot]), "%s", message);
    const auto shutter = journal_.shutter(slot);
    const uint8_t node = shutter.logical_id ? shutter.logical_id : command_node_;
    const char *text = cause(message);
    if (node > NODE) snprintf(diagnostic_, sizeof(diagnostic_), "V%u: %.19s", node, text);
    else snprintf(diagnostic_, sizeof(diagnostic_), "%s", text);
    updates_ |= 1u | (slot ? 1u << slot : 0);
  }
  void tx_result(const radio::TxEvent &event) {
    const uint8_t slot = event.job.shutter_id;
    if (!slot || slot > MAX_SHUTTERS) return;
    if (event.job.incarnation != journal_.incarnation(slot, event.job.enrollment)) {
      drop_event_status_ = true;
      return;
    }
    if (!event.job.enrollment) {
      if (!strcmp(event.outcome, "emitted"))
        positions_[slot - 1] = event.job.action == Action::open ? 1 : event.job.action == Action::close ? 2 : 0;
      else if (strcmp(event.outcome, "rejected") || event.completed_copies)
        positions_[slot - 1] = 0;
    }
    updates_ |= 1u << slot;
  }

 private:
  static constexpr size_t SNAPSHOT_HEADROOM = 512;
  friend class ota::Receiver<Gateway>;
  bool ota_stream(uint8_t type, const char *payload) { return send(NODE, SYSTEM, STREAM, 0, type, payload); }
  bool ota_log(const char *payload) { return send(NODE, SYSTEM, INTERNAL, 0, I_LOG_MESSAGE, payload); }
  void ota_pause() { controller_.pause("ota_busy"); }
  void ota_resume() { if (connected_ && !failed()) { controller_.resume(); present(); } }
  bool ota_quiescent() const { return !controller_.busy(); }

  static const char *cause(std::string_view reason) {
    if (reason == "ready" || reason == "initialized") return "Pret";
    if (reason == "initializing") return "Initialisation en cours";
    if (reason == "legacy_storage" || reason == "initialization_required" || reason == "storage_legacy") return "Initialisation requise";
    if (reason == "associating" || reason == "retrying") return "Association en cours";
    if (reason == "awaiting_confirmation") return "Mouvement a confirmer";
    if (reason == "association_pending") return "Reprise requise";
    if (reason == "paired" || reason == "activated" || reason == "service_enabled") return "Actif, position inconnue";
    if (reason == "disabled" || reason == "service_disabled") return "Desactive";
    if (reason == "retired" || reason == "association_cancelled") return "Retire ou annule";
    if (reason == "cancelled" || reason == "session_disconnected" || reason == "queue_cancelled") return "Interrompu, pos. inconnue";
    if (reason == "accepted" || reason == "emitted") return "Position non mesuree";
    if (reason == "radio_busy") return "Radio occupee";
    if (reason == "association_busy" || reason == "multiple_pending_associations") return "Association deja ouverte";
    if (reason == "no_pending_association" || reason == "no_candidate") return "Aucune association";
    if (reason == "no_association_attempt") return "Aucun essai termine";
    if (reason == "association_counter_mismatch" || reason == "retry_exhausted" || reason == "attempt_exhausted" || reason == "retry_unavailable") return "Essais epuises: annuler";
    if (reason == "not_in_service" || reason == "shutter_disabled" || reason == "not_paired") return "Volet hors service";
    if (reason == "service_required_off" || reason == "in_service" || reason == "disable_first") return "Desactiver le volet";
    if (reason == "candidate_pending") return "Annuler association";
    if (reason == "inventory_full") return "16 volets enregistres";
    if (reason == "logical_ids_exhausted" || reason == "identity_exhausted" || reason == "counter_exhausted") return "Capacite epuisee";
    if (reason == "storage_corrupt") return "Erreur memoire";
    if (reason == "storage_io_error") return "Erreur ecriture memoire";
    if (reason == "storage_full" || reason == "storage_maintenance" || reason == "maintenance_pending") return "Memoire occupee";
    if (reason == "transmission_disabled" || reason == "tx_disabled") return "Emission desactivee";
    if (reason == "association_disabled" || reason == "association_profile_unqualified" || reason == "unqualified_profile") return "Association indisponible";
    if (reason == "radio_unavailable" || reason == "radio_fault" || reason == "incomplete_burst") return "Erreur radio";
    if (reason == "ota_busy") return "Mise a jour en cours";
    if (reason == "unknown_shutter" || reason == "unknown_node") return "Volet absent ou retire";
    if (reason == "invalid_message" || reason == "unsupported_command" || reason == "unsupported_read") return "Commande non supportee";
    if (reason == "already_initialized") return "Deja initialise";
    if (reason == "stop_preempted") return "Interrompu par STOP";
    if (reason == "deadline_expired" || reason == "expired") return "Commande expiree";
    return "Operation refusee";
  }
  bool send(uint8_t node, uint8_t child, uint8_t command, uint8_t echo,
            uint8_t type, const char *payload) {
    if (!connected_ || failed()) return false;
    char line[LINE_BYTES];
    const int size = snprintf(line, sizeof(line), "%u;%u;%u;%u;%u;%s\n", node, child, command, echo, type, payload);
    if (!valid_payload(command, payload, strlen(payload)) || size <= 0 || static_cast<size_t>(size) >= sizeof(line) ||
        !output_.append(line, static_cast<size_t>(size))) {
      failed_ = true;
      output_.clear();
      constexpr char fault[] = "1;19;1;0;24;serial_overflow\n";
      output_.append(fault, sizeof(fault) - 1);
      return false;
    }
    return true;
  }
  bool value(uint8_t node, uint8_t child, uint8_t type, const char *payload, uint8_t echo = 0) {
    return send(node, child, SET, echo, type, payload);
  }
  uint8_t slot_for(uint8_t node) const {
    if (node <= NODE || node == SYSTEM) return 0;
    for (uint8_t slot = 1; slot <= MAX_SHUTTERS; ++slot)
      if (journal_.shutter(slot).logical_id == node) return slot;
    return 0;
  }
  bool adding() const {
    const uint8_t slot = controller_.pending_slot();
    return slot && slot <= MAX_SHUTTERS && !journal_.shutter(slot).replacement;
  }
  const char *cover_state(uint8_t slot) const { return positions_[slot - 1] == 2 ? "0" : "1"; }
  const char *shutter_state(uint8_t slot) const {
    const auto s = journal_.shutter(slot);
    const std::string_view reason{reasons_[slot]};
    // A refused lifecycle operation must be understandable on this device
    // page, even though its authoritative switches keep their previous state.
    if (!reason.empty() && reason != "ready" && reason != "paired" &&
        reason != "service_enabled" && reason != "service_disabled" &&
        reason != "association_cancelled" && reason != "association_pending" &&
        reason != "associating" && reason != "retrying" &&
        reason != "awaiting_confirmation" && reason != "accepted" && reason != "emitted")
      return cause(reason);
    if (s.has_candidate) {
      if (reason == "awaiting_confirmation") return "Mouvement a confirmer";
      if (reason == "associating" || reason == "retrying") return "Association en cours";
      if (reason == "radio_fault" || reason == "incomplete_burst") return "Erreur radio: reessayer";
      return "Reprise ou confirmation";
    }
    return s.in_service ? "Actif, position inconnue" : "Desactive";
  }
  void cover_snapshot(uint8_t slot) {
    const uint8_t node = journal_.shutter(slot).logical_id;
    if (!node || !controller_.paired(slot)) return;
    // STOP precedes command echoes: an UP/DOWN echo never proves measured motion.
    value(node, COVER, V_STOP, "1");
    value(node, COVER, V_UP, "0");
    value(node, COVER, V_DOWN, "0");
    value(node, COVER, V_STATUS, cover_state(slot));
  }
  const char *switch_state(uint8_t slot, uint8_t child) const {
    const auto s = journal_.shutter(slot);
    return child == SERVICE ? s.in_service ? "1" : "0" :
           child == ASSOCIATION ? s.has_candidate ? "1" : "0" : "0";
  }
  void retired_snapshot(uint8_t node) {
    value(node, SERVICE, V_STATUS, "0");
    value(node, ASSOCIATION, V_STATUS, "0");
    value(node, RETRY, V_STATUS, "0");
    value(node, RETIRE, V_STATUS, "1");
    value(node, STATE, V_VAR1, "Retire");
  }
  static uint8_t take(uint32_t &mask) {
    for (uint8_t bit = 0; bit <= MAX_SHUTTERS; ++bit) {
      if (!(mask & (1u << bit))) continue;
      mask &= ~(1u << bit);
      return bit;
    }
    return SYSTEM;
  }
  uint32_t owned_mask(uint8_t node) const {
    if (node == NODE) return 1;
    if (node != 0 && node != SYSTEM) {
      const uint8_t slot = slot_for(node);
      return slot ? 1u << slot : 0;
    }
    uint32_t mask = 1;
    for (uint8_t slot = 1; slot <= MAX_SHUTTERS; ++slot)
      if (journal_.shutter(slot).logical_id) mask |= 1u << slot;
    return mask;
  }
  void present(uint8_t node = 0) { presentation_ |= owned_mask(node); }
  void update_step() {
    const uint8_t slot = take(updates_);
    if (!slot) {
      value(NODE, ADD, V_STATUS, adding() ? "1" : "0");
      value(NODE, DIAGNOSTIC, V_VAR1, diagnostic_);
      value(NODE, INITIALIZE, V_STATUS, "0");
      return;
    }
    const auto s = journal_.shutter(slot);
    if (!s.logical_id) return;
    cover_snapshot(slot);
    for (uint8_t child = SERVICE; child <= RETIRE; ++child)
      value(s.logical_id, child, V_STATUS, switch_state(slot, child));
    value(s.logical_id, STATE, V_VAR1, shutter_state(slot));
  }
  void internal_step(uint32_t &mask, uint8_t type) {
    const uint8_t slot = take(mask);
    const uint8_t node = slot ? journal_.shutter(slot).logical_id : NODE;
    if (!node) return;
    char time[11];
    snprintf(time, sizeof(time), "%lu", static_cast<unsigned long>(now_));
    send(node, SYSTEM, INTERNAL, 0, type, type == I_DISCOVER_RESPONSE ? "0" : time);
  }
  void presentation_step() {
    if (present_slot_ == SYSTEM) {
      if (!presentation_) return;
      present_slot_ = take(presentation_);
      present_node_ = present_slot_ ? journal_.shutter(present_slot_).logical_id : NODE;
      present_phase_ = 0;
    }
    if (!present_node_ || (present_slot_ && journal_.shutter(present_slot_).logical_id != present_node_)) {
      present_slot_ = SYSTEM;
      return;
    }
    const uint8_t phase = present_phase_++;
    if (!present_slot_) {
      switch (phase) {
        case 0: send(NODE, SYSTEM, PRESENTATION, 0, S_NODE, "2.3.2"); break;
        case 1: send(NODE, SYSTEM, INTERNAL, 0, I_SKETCH_NAME, "X2D USB"); break;
        case 2: send(NODE, SYSTEM, INTERNAL, 0, I_SKETCH_VERSION, policy_.firmware()); break;
        case 3: send(NODE, ADD, PRESENTATION, 0, S_BINARY, "Mode ajout"); break;
        case 4: send(NODE, DIAGNOSTIC, PRESENTATION, 0, S_CUSTOM, "Etat dongle"); break;
        case 5: send(NODE, INITIALIZE, PRESENTATION, 0, S_BINARY, "Initialiser"); break;
        case 6: value(NODE, ADD, V_STATUS, adding() ? "1" : "0"); break;
        case 7: value(NODE, DIAGNOSTIC, V_VAR1, diagnostic_); break;
        case 8: value(NODE, INITIALIZE, V_STATUS, "0"); break;
      }
      if (present_phase_ == 9) present_slot_ = SYSTEM;
      return;
    }
    char name[PAYLOAD_BYTES + 1];
    if (phase == 0) send(present_node_, SYSTEM, PRESENTATION, 0, S_NODE, "2.3.2");
    else if (phase == 1) send(present_node_, SYSTEM, INTERNAL, 0, I_SKETCH_NAME, "Volet X2D");
    else if (phase == 2) send(present_node_, SYSTEM, INTERNAL, 0, I_SKETCH_VERSION, policy_.firmware());
    else if (phase == 3 && controller_.paired(present_slot_)) {
      snprintf(name, sizeof(name), "X2D Volet %u Commandes", present_node_);
      send(present_node_, COVER, PRESENTATION, 0, S_COVER, name);
    } else if (phase >= 4 && phase <= 7 && controller_.paired(present_slot_)) {
      const uint8_t type = phase == 4 ? V_STOP : phase == 5 ? V_UP : phase == 6 ? V_DOWN : V_STATUS;
      value(present_node_, COVER, type, phase == 4 ? "1" : phase == 7 ? cover_state(present_slot_) : "0");
    } else if (phase >= 8 && phase <= 11) {
      const uint8_t child = static_cast<uint8_t>(SERVICE + phase - 8);
      const char *label = child == SERVICE ? "En service" : child == ASSOCIATION ? "Association" : child == RETRY ? "Nouvel essai" : "Retire";
      snprintf(name, sizeof(name), "X2D %u %s", present_node_, label);
      send(present_node_, child, PRESENTATION, 0, S_BINARY, name);
    } else if (phase == 12) {
      snprintf(name, sizeof(name), "X2D %u Etat", present_node_);
      send(present_node_, STATE, PRESENTATION, 0, S_CUSTOM, name);
    } else if (phase >= 13 && phase <= 16) {
      const uint8_t child = static_cast<uint8_t>(SERVICE + phase - 13);
      value(present_node_, child, V_STATUS, switch_state(present_slot_, child));
    } else if (phase == 17) value(present_node_, STATE, V_VAR1, shutter_state(present_slot_));
    if (present_phase_ == 18) present_slot_ = SYSTEM;
  }
  void dispatch(const Message &m) {
    command_node_ = m.node;
    dispatch_message(m);
    command_node_ = 0;
  }
  void dispatch_message(const Message &m) {
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
    if (m.command == INTERNAL && m.child == SYSTEM && owned_mask(m.node)) {
      if (m.type == I_VERSION) send(m.node == SYSTEM ? NODE : m.node, SYSTEM, INTERNAL, 0, I_VERSION, "2.3.2");
      else if (m.type == I_REBOOT && m.node == NODE && !*m.payload && ota_committed()) reboot_requested_ = true;
      else if (m.type == I_DISCOVER) discoveries_ |= owned_mask(m.node);
      else if (m.type == I_PRESENTATION) present(m.node);
      else if (m.type == I_HEARTBEAT_REQUEST) heartbeats_ |= owned_mask(m.node);
      return;
    }
    if (m.command != REQ && m.command != SET) return;
    const uint8_t slot = m.node == NODE ? 0 : slot_for(m.node);
    if (m.node != NODE && !slot) { status("unknown_node", 0); return; }
    const bool manager_switch = m.node == NODE && (m.child == ADD || m.child == INITIALIZE);
    const bool shutter_switch = slot && m.child >= SERVICE && m.child <= RETIRE;
    const bool cover = slot && m.child == COVER && controller_.paired(slot);
    if (m.command == REQ) {
      if (manager_switch && m.type == V_STATUS)
        value(NODE, m.child, V_STATUS, m.child == ADD && adding() ? "1" : "0", m.echo);
      else if (shutter_switch && m.type == V_STATUS)
        value(m.node, m.child, V_STATUS, switch_state(slot, m.child), m.echo);
      else if (m.node == NODE && m.child == DIAGNOSTIC && m.type == V_VAR1)
        value(NODE, DIAGNOSTIC, V_VAR1, diagnostic_, m.echo);
      else if (slot && m.child == STATE && m.type == V_VAR1)
        value(m.node, STATE, V_VAR1, shutter_state(slot), m.echo);
      else if (cover && (m.type == V_STATUS || m.type == V_UP || m.type == V_DOWN || m.type == V_STOP))
        value(m.node, COVER, m.type, m.type == V_STATUS ? cover_state(slot) : m.type == V_STOP ? "1" : "0", m.echo);
      else status("unsupported_read", slot);
      return;
    }
    const bool action = cover && (m.type == V_UP || m.type == V_DOWN || m.type == V_STOP);
    const bool binary = !strcmp(m.payload, "1") || !strcmp(m.payload, "0");
    if (!binary || !(action || ((manager_switch || shutter_switch) && m.type == V_STATUS))) {
      status("unsupported_command", slot);
      return;
    }
    if (updating()) { status("ota_busy", slot); return; }
    if (action) cover_snapshot(slot);
    if (failed()) return;
    // Echo acknowledges receipt only; firmware values follow even after refusal.
    if (m.echo && !send(m.node, m.child, SET, 1, m.type, m.payload)) return;
    const bool on = !strcmp(m.payload, "1");
    if (action) {
      if (on) {
        const Action command = m.type == V_UP ? Action::open : m.type == V_DOWN ? Action::close : Action::stop;
        if (controller_.command(slot, command, now_)) {
          if (command == Action::stop) positions_[slot - 1] = 0;
          status("accepted", slot);
        }
      }
      updates_ |= 1u << slot;
      return;
    }
    if (manager_switch) {
      if (m.child == INITIALIZE) { if (on) controller_.initialize(); }
      else if (on) {
        const uint8_t pending = controller_.pending_slot();
        if (!pending) {
          if (controller_.associate(now_)) present();
        } else if (!adding()) status("association_busy", pending);
      } else if (adding()) {
        const uint8_t pending = controller_.pending_slot();
        const uint8_t node = journal_.shutter(pending).logical_id;
        if (controller_.cancel(pending)) retired_snapshot(node);
      }
      updates_ |= 1;
      return;
    }
    const auto before = journal_.shutter(slot);
    bool changed = false;
    if (m.child == SERVICE) {
      changed = on && before.has_candidate ? controller_.confirm(slot) : controller_.service(slot, on);
      if (changed) positions_[slot - 1] = 0;
    } else if (m.child == ASSOCIATION) {
      if (on && !before.has_candidate) changed = controller_.replace(slot, now_);
      else if (!on && before.has_candidate) changed = controller_.cancel(slot);
    } else if (m.child == RETRY) { if (on) changed = controller_.retry(slot, now_); }
    else if (m.child == RETIRE && on) changed = controller_.retire(slot);
    if (changed && !journal_.shutter(slot).logical_id) {
      positions_[slot - 1] = 0;
      retired_snapshot(m.node);
    } else if (changed && (m.child == SERVICE || m.child == ASSOCIATION)) present(m.node);
    updates_ |= 1u | (1u << slot);
  }

  journal::Journal &journal_;
  Policy &policy_;
  Controller<Radio, Gateway> controller_;
  ota::Receiver<Gateway> ota_;
  LineFramer input_{LINE_BYTES};
  OutputBuffer output_;
  char device_id_[17]{}, diagnostic_[PAYLOAD_BYTES + 1]{}, reasons_[MAX_SHUTTERS + 1][41]{};
  uint8_t positions_[MAX_SHUTTERS]{};
  uint32_t now_ = 0, presentation_ = 0, updates_ = 0, discoveries_ = 0, heartbeats_ = 0;
  uint8_t present_slot_ = SYSTEM, present_node_ = 0, present_phase_ = 0, command_node_ = 0;
  bool reboot_requested_ = false, connected_ = false, invalid_ = false, failed_ = false,
       stopped_ = false, drop_event_status_ = false;
};

}  // namespace x2d::mysensors
