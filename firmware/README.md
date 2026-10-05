# Firmware YD-RP2040

## Capture passive autonome (debug RX)

Le sketch séparé [rx_debug/rx_debug.ino](rx_debug/rx_debug.ino) reçoit uniquement.
Version **rx-debug-0.2.2**, PIO/DMA. Il conserve le numéro série physique de la flash, avec le produit USB
**HA-X2D RX Debug** ; il ne correspond pas à la découverte HA du gateway v2.
SPI : MISO **16**, CS **17**, SCK **18**, MOSI **19** ; ajouter GDO0 → **GP20**,
GDO2 → **GP21**. Quartz **26 MHz supposé**. Au boot, la radio reste en IDLE ;
chaque fil GDO est testé séparément par sortie constante basse/haute puis restauré.
`gdo0_wiring_ok` / `gdo2_wiring_ok` sont les observations de ces tests, pas une
preuve de réception d'une télécommande. Aucun chemin STX, écriture FIFO TX ou
commande d'émission n'existe ; le contrôle radio par broches est désactivé.

Compiler avec le même profil et cache que le diagnostic :

```sh
devenv shell
python tools/build_firmware.py rx_debug --output-dir build/rx-debug
g++ -std=c++17 -Wall -Wextra -Werror \
  -I.devenv/state/arduino/data/internal/ArduinoJson_7.4.3_65bbd090d30b7927/ArduinoJson/src \
  -Ilib/x2d-core/src firmware/check_rx_debug.cpp -o build/rx-debug/check_rx_debug
build/rx-debug/check_rx_debug
```

UF2 : `build/rx-debug/rx_debug.ino.uf2`. Le profil debug `sketch.yaml` utilise Arduino-Pico **6.1.1**, ArduinoJson
**7.4.3** et le framer partagé ; le profil gateway MySensors ne charge pas JSON.
La flash physique observée est **4 MiB** ; ce build réserve la même zone de
journal que la passerelle, sans l’utiliser ni monter de système de fichiers.
`devenv tasks run firmware:rx-debug-build` vérifie aussi la protection UF2.
Le flashage et l'accès matériel restent une étape distincte de la compilation.

Collecter avec le Python de l'environnement existant (`serialx` déjà installé),
un seul propriétaire du port. Définir `X2D_DEVICE_ID` avec les 16 chiffres
hexadécimaux de l'identifiant USB de sa carte. Première session :

```sh
python tools/collect_rx_debug.py /dev/serial/by-id/LE_PORT_RX_DEBUG \
  --device-id "$X2D_DEVICE_ID" --mode fsk --frequency-hz 868350000 \
  --deviation-hz 38086 --duration 600 --output build/captures/stop-fsk-868350.jsonl
```

Attendre **Ready**, puis **trois appuis courts sur STOP**, espacés de **3 secondes**,
sur la télécommande d'origine, en notant A/B et les heures. Éviter l'appui long
et les boutons d'association. Le firmware est passif ; la télécommande peut
agir normalement sur le volet. Une session se termine à sa durée limite ou par
Ctrl-C, en conservant le JSONL partiel. Le port fermé arrête RX ; à la fin normale
le collecteur demande aussi `stop`. L'outil peut rester lancé pendant les échanges
avec l'utilisateur ; il imprime un état au plus toutes les 5 secondes.

Les profils sont des **candidats à tester**, sans revendication de support :
FSK ~40 kbit/s à **868350000**, **868950000**, **869034000** Hz ; OOK
~4,8227 kbit/s à **868300000** ou **868439941** Hz (`--mode ook --deviation-hz 0`).
Si la FSK est incohérente, essayer `--deviation-hz 19043` au lieu de 38086.
Fréquence admissible : 863–870 MHz ; déviation FSK : 1–200 kHz. Fréquence et
déviation sont arrondies aux valeurs matérielles disponibles et consignées.
Chaque changement est explicite, sans reflash ni balayage automatique.
Registres basés sur [TI SWRS061I](https://www.ti.com/lit/ds/symlink/cc1101.pdf),
avec réglages RX [DN022](https://www.ti.com/lit/an/swra215e/swra215e.pdf) et
profils candidats examinés indépendamment. RSSI ≈ brut signé / 2 − 74 dBm,
non calibré. Les 47 registres de configuration sont relus dans chaque état.

**Protocole debug distinct de la passerelle MySensors ci-dessous** : commandes ASCII
`status`, `stop`, `rx fsk 868350000 38086`, `rx ook 868439941 0`, suivies de LF
(CRLF accepté, **512 octets LF compris**, une commande à la fois). Le firmware
émet spontanément du JSONL, **4096 octets LF compris**, avec `debug:1`, `type`
(`status`, `config`, `samples`, `stopped`, `error`), `seq` et `config_id`.
`config` confirme seulement une configuration relue, calibration IDLE réussie
et entrée RX (`MARCSTATE=13`). Aucune émission USB spontanée avant une commande
valide de l'hôte : le collecteur ouvre le port en mode raw, demande `status`,
puis configure RX. Ensuite, un état arrive chaque seconde. Ce handshake évite
l'écho TTY de notre JSON pendant l'ouverture, observé dans le préfixe d'une
commande rejetée. Déconnexion = remise à zéro du handshake ; aucun délai ni rejeu.

La PIO boucle sur **une instruction `in pins,2` à 400 kéchantillons/s** et capture
GP20/21 simultanément, sans condition sur CS. Bit 0 = GDO0, bit 1 = GDO2/porteuse.
Elle décale à droite et pousse un mot de 32 bits tous les 16 échantillons. Deux
DMA chaînés alternent sur **deux buffers de 16 Kio**, alignés à 16 Kio et protégés
par un ring matériel d'écriture de 14 bits : 65 536 échantillons par bloc,
soit ~163,84 ms. Le buffer terminé doit être transféré avant sa réutilisation ;
le réarmement se fait à chaque IRQ de fin de bloc, sans IRQ par front.
EN est écrit via l'alias **`AL1_CTRL` sans trigger** ; seul le DMA A est démarré
explicitement, puis les chaînes alternent A/B. Écrire `CTRL_TRIG` pour activer
EN déclencherait les deux canaux et leur ferait partager incorrectement le FIFO.

Chaque `samples` contient `data_hex` (**1536 octets bruts maximum**), soit quatre
échantillons par octet. Les premiers échantillons sont les bits 1:0 puis 3:2,
5:4 et 7:6 ; les octets suivent l'ordre little-endian du mot DMA. Par exemple,
`e4` représente les niveaux **0, 1, 2, 3**, y compris les deux états CS faux.
La polarité est conservée. Le collecteur calcule les transitions sur l'hôte et
ne relie jamais des échantillons séparés par une discontinuité.

`chunk` est une séquence 32 bits globale ; `capture_epoch` change à chaque
démarrage ou reprise après anomalie ; `block` repart de zéro dans chaque époque.
`byte_offset`, `sample_index` et `sample_count` situent précisément le chunk :
`sample_index = block * 65536 + byte_offset * 4`. `epoch_start_us` est le temps
64 bits du MCU pris juste avant l'activation PIO ; `sample_start_us` est calculé
depuis l'index, `system_clock_hz` et `divider_256`. L'intervalle nominal est
`divider_256 / (256 * system_clock_hz)` secondes. `status.sample_hz` rapporte ce
débit ; la précision physique du quartz RP2040 reste à mesurer.

Avant et après chaque copie courte, sous masque IRQ, le firmware vérifie que le
DMA du buffer est inactif, son compteur courant vaut zéro et aucune completion
DMA n'est en attente. Écrire `TRANS_COUNT` règle le **RELOAD** ; sa lecture
expose le compteur courant, pas le RELOAD, conformément au
[SDK RP2040](https://www.raspberrypi.com/documentation/pico-sdk/hardware.html).
Une copie traversant la réutilisation DMA est rejetée (`copy_retries`), jamais
publiée comme intacte. `overrun_blocks` et `dropped_samples` comptent les blocs
ou restes non transmis ; `loss` signale un changement de pertes depuis le chunk
précédent. `gap_events`, `pio_stalls` et `dma_chain_stalls` signalent un arrêt
ou retard matériel : les deux DMA sont immédiatement arrêtés, les données
concernées sont rejetées, puis la PIO redémarre
avec une **nouvelle époque**. La durée et le nombre d'échantillons manqués dans
ces pauses ne sont pas connus ; `dropped_samples` compte seulement les pertes
connues, sans inclure une fraction de mot PIO ni le FIFO restant. Les changements
de profil, `stop` et déconnexions comptent aussi les buffers prêts abandonnés et
les mots partiels du DMA qui était actif, une seule fois. Aucun bloc complet
n'est inventé à partir d'un canal inactif à compteur zéro ; après un gap ambigu,
les mots partiels ne sont pas estimés.
Les deux canaux sont d'abord mis en pause via `AL1_CTRL.EN`, puis BUSY et
`TRANS_COUNT` sont sauvegardés **avant** `CHAN_ABORT`, qui efface le compteur
courant ([RP2040, section 2.5.5](https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf#page=98)).
Cette lecture compte seulement les mots confirmés avant abort ; les transferts
encore en vol peuvent rester exclus. Elle évite l'ajout fictif de 65 536
échantillons observé avec la lecture après abort en v0.2.1.
Les compteurs d'échantillons et temps sont 64 bits ; les séquences sont 32 bits.

`sampler_ready` confirme l'initialisation PIO/DMA complète. Un `invalid_command`
contient `command_length` et `command_bytes` (hex des **511 octets maximum** reçus,
hors LF et CR final). Les lectures série négatives ne passent pas au framer et
incrémentent `serial_read_errors`. Pas de délai arbitraire ni de rejeu de commande.

Le fichier JSONL conserve chaque ligne exacte dans `line`, avec `direction`,
heure UTC et temps monotone de l'hôte ; il journalise les commandes, paramètres
et un résumé final. Chaque ligne est flushée ; le fichier existant n'est jamais
écrasé. Sans `--output`, un nom UTC unique est créé dans `build/captures/`.

**Limite : mesure numérique démodulée à 400 kéchantillons/s**, pas une mesure RF
analogique. La capture ISR précédente perdait déjà plus de 100 000 fronts en
5 s de bruit FSK : elle est remplacée, sans compatibilité de format.

Essai matériel du **1er octobre 2026**, YD-RP2040 + CC1101 câblés comme ci-dessus :
USB, SPI et les deux fils GDO vérifiés ; firmware **0.2.2** installé. La capture
OOK à 868,35 MHz contient **163 502 080 échantillons** (~409 s), sans trou de
séquence ou d'échantillons, overrun ni erreur SPI/PIO/DMA déclarés. Le premier
chunk signale le compteur de pertes hérité de l'arrêt de l'essai précédent ;
ce compteur n'augmente pas pendant cette capture.
L'analyse hors ligne retrouve des trames biphase mark **compatibles X2D**, avec
sommes additives valides, deux identifiants distincts et le même code lors des
appuis STOP. Les six rafales du premier essai FSK sont décodées ; cinq sur six
le sont dans le second essai OOK (une rafale de A sans trame validée).
L'attribution A/B suit l'ordre des appuis rapporté par l'utilisateur.
Ces réglages du récepteur ne mesurent pas la modulation ni la fréquence centrale
des télécommandes. À la fin, la radio est en IDLE, RX et TX désactivés.
Ces essais valident la capture et le décodage hors ligne de STOP ; l'émission,
l'association et la commande effective d'un volet restent à tester.

## MySensors USB gateway — experimental RF

[ha_x2d/ha_x2d.ino](ha_x2d/ha_x2d.ino) uses the native MySensors 2.x serial
protocol at 115200 baud. Follow the [Home Assistant guide](../home_assistant/README.md)
to install the dongle and associate shutters.

```sh
git submodule update --init --recursive
devenv shell
devenv test
devenv tasks run firmware:build
```

The candidate firmware is `dist/ha_x2d-0.6.0-rc2-yd-rp2040-4mb.uf2`.
It includes the complete native MySensors lifecycle: adding, confirming, disabling,
replacing and retiring shutters at runtime. No private suffix, compiled slot or
counter flag is needed. Boot and USB reconnection transmit nothing. Its public
suffix `0x01` remains an unqualified hardware candidate.

The **yd-rp2040-4mb-ota** profile pins Arduino-Pico **6.1.1**. Always use
`tools/build_firmware.py` (or devenv tasks), which checks the actual UF2/binary.
The **64 KiB** journal remains at **0x101EF000**, before LittleFS staging. The
layout, bootloader and OTA boundaries are unchanged from 0.5.0. Wrong reservations
block journal access and updates. Follow the [update guide](../docs/FIRMWARE_UPDATE.md).

A valid old journal requires explicit **Initialiser** on the manager device.
Its associations/counters are discarded; only RF identity exclusions survive.
The reset is committed before old records are erased, and no v2 radio operation
is allowed before that finalization completes. Corrupt/unknown data is never
silently erased. Existing motors must be associated again through the new flow.

The journal v2 supports reusable physical slots and stable logical devices.
Replacing a motor keeps its HA device; retiring and adding one allocates a new
public ID. Each job captures the private radio incarnation, so an old queued
operation cannot follow a reused slot. RF identities are allocated without
repetition in the journal's life; consumed counters never roll back.

### Runtime association

Follow [the device-page workflow](../home_assistant/README.md#add-a-shutter).
Only one candidate exists at once. An explicit initial operation claims one
0/1 attempt. A separate **Nouvel essai** claims the only 2/3 retry; lost sessions
never restore emission permits. Human observation of the motor response and an
idle radio are required before confirmation. The existing two-phase 24-copy
sequence is preserved. No measured position is inferred from transmitter success.

The controller preserves STOP priority, frame-boundary cancellation, idle journal
maintenance and no automatic replay. The PIO/DMA waveform stays continuous with
one preamble and a nominal **208500 ns** chip duration. The CC1101 profile is
async OOK at 868.350 MHz with register checks before transmission. Verify and
calibrate these parameters during qualification; software tests are not RF proof.

On USB loss, pending bytes and requests are discarded, reservations stay consumed,
and the active burst stops at a full frame boundary. One input line is processed
per pass. Presentations are incremental so 16 devices do not fill the 4096-byte
response buffer. An actual overflow closes admission until USB is reopened.

See [OBSERVATIONS_RADIO.md](../docs/OBSERVATIONS_RADIO.md) for earlier evidence.
Qualify the public profile, physical STOP, unplugging, and reset/watchdog carrier
off on the board. Keep the GDO0 10 kΩ pull-down and verify carrier suppression.

## Wiring

| YD-RP2040 | CC1101 |
| --- | --- |
| GP16 | MISO / SO |
| GP17 | CSN |
| GP18 | SCLK |
| GP19 | MOSI / SI |
| GP20 | GDO0 |
| GP21 | GDO2 |
| 3V3 | VCC |
| GND | GND |

Le produit USB est `HA-X2D Gateway`, VID/PID `2E8A:800A`, avec un numéro série
stable issu de la flash. Le sketch RX debug utilise `HA-X2D RX Debug` et ne
correspond pas à la découverte HA. Aucun flashage n'est inclus dans les tâches
de compilation. Vérifier le matériel séparément avant toute qualification.

## USB power

La veille automatique USB doit être désactivée pour cette passerelle pendant
les transactions radio. Sur l'hôte Ubuntu de test, `power/control=auto`, délai
2 000 ms et réveil désactivé ont reproduit l'interruption d'une inscription
numérique sans RF. La bibliothèque USB signale alors un port non connecté ;
le firmware annule la transaction, conserve les compteurs réservés et ne la
rejoue pas. L’émission complète du premier essai RF de C n’a pas été établie.
La reprise supervisée et le cycle de C ont depuis été confirmés sur ce moteur,
comme décrit dans les [observations radio](../docs/OBSERVATIONS_RADIO.md).

Installer [99-ha-x2d-power.rules](99-ha-x2d-power.rules), qui cible uniquement
le VID/PID et les noms USB HA-X2D ; les autres périphériques restent inchangés :

```sh
sudo install -m 0644 firmware/99-ha-x2d-power.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
```

La règle s'applique à la prochaine reconnexion ; pour la carte déjà présente,
déclencher une action `change` sur son chemin exact `/sys/bus/usb/devices/…`.
Vérifier ensuite `power/control=on`. L'accès au port série relève de `dialout` ;
l'accès USB BOOTSEL peut être donné à `plugdev` par une règle VID/PID séparée.
Sur HA OS, vérifier cette condition lors de la qualification du lien USB ;
les commandes d'installation Ubuntu ci-dessus ne sont pas une procédure HA OS.

## Shared core

Arduino CLI profiles load the pinned `../../lib/x2d-core` submodule via `dir:`.
The core owns the codec, journal, STOP runtime, association controller and CC1101
register operations. Its public headers use `<x2d/...>` and namespace `x2d`.
This repository owns the MySensors protocol, bounded serial I/O, host simulator,
USB/SPI/GPIO/flash and PIO/DMA adapters, identity and entropy, timing calibration,
and explicit build-time permissions. The ESPHome adapter uses the same controller
but retains ownership of native API visibility, reboot-after-confirmation and OTA.

Host checks build from this repository root so the dongle's MySensors protocol
and the independent X2D library are both tested:

```sh
cmake -S . -B build/native
cmake --build build/native
ctest --test-dir build/native --output-on-failure
```

The resulting `build/native/mysensors_server` supplies the simulated USB
endpoint used by the native Home Assistant tests.
