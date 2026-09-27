# Firmware YD-RP2040

Premier jalon : USB CDC avec `hello` et `status`, et vérification SPI du CC1101.
Aucune émission ni capture radio. `hello` ne touche pas au CC1101 ; `status`
lit ses registres de diagnostic. Le logiciel ne configure aucune fréquence.

## Câblage proposé (à vérifier sur le module réel)

| YD-RP2040 | CC1101 |
| --- | --- |
| GP16 (SPI0 RX) | MISO / SO |
| GP17 | CSN |
| GP18 (SPI0 SCK) | SCK |
| GP19 (SPI0 TX) | MOSI / SI |
| 3V3 | VCC 3,3 V |
| GND | GND |

Les broches GDO restent libres. Vérifier l'orientation du module, son alimentation
3,3 V et la masse commune avant branchement. La capacité flash exacte de la carte
YD-RP2040 reste à relever : sélectionner **explicitement** son option de flash
dans Arduino-Pico, sans supposer 16 Mo. Aucun flashage n'est inclus ici.

## Compilation

Depuis la racine du dépôt, utiliser l'environnement devenv :

```sh
devenv tasks run firmware:check
devenv tasks run firmware:build
```

`firmware:check` exécute le contrôle C++ sur l'ordinateur ; `firmware:build`
produit le firmware RP2040. `devenv test` inclut le contrôle C++ et les tests
Python ; la compilation croisée reste une commande explicite.

Le profil `yd-rp2040-2mb` dans [ha_x2d/sketch.yaml](ha_x2d/sketch.yaml) fait
autorité pour les versions : Arduino-Pico **6.1.1**, ArduinoJson **7.4.3** et
carte `vccgnd_yd_rp2040` avec `flash=2097152_0` (2 Mo sans système de fichiers).
C'est un **profil de vérification de compilation**, pas une validation de la
carte achetée. Aucun profil par défaut ni port de flashage n'est configuré.
Confirmer la flash réelle avant de créer un profil destiné au matériel.

[Arduino CLI 1.5.1](https://arduino.github.io/arduino-cli/1.5/sketch-project-file/)
installe automatiquement les versions du profil dans son cache isolé ; les
cœurs et bibliothèques installés globalement ne sont pas utilisés. Le premier
build nécessite un accès réseau. Dans devenv, les répertoires Arduino sont
placés sous `$DEVENV_STATE/arduino`.

Sans devenv, avec Arduino CLI 1.5.1 déjà installé, la même compilation se lance
avec des répertoires locaux au projet :

```sh
ARDUINO_ROOT="$PWD/build/arduino-standalone"
mkdir -p "$ARDUINO_ROOT/tmp" "$ARDUINO_ROOT/cache"
ARDUINO_DIRECTORIES_DATA="$ARDUINO_ROOT/data" \
ARDUINO_DIRECTORIES_DOWNLOADS="$ARDUINO_ROOT/downloads" \
ARDUINO_DIRECTORIES_USER="$ARDUINO_ROOT/user" \
TMPDIR="$ARDUINO_ROOT/tmp" XDG_CACHE_HOME="$ARDUINO_ROOT/cache" \
arduino-cli --config-dir "$ARDUINO_ROOT" compile --profile yd-rp2040-2mb \
  --build-path "$ARDUINO_ROOT/build" --output-dir "$ARDUINO_ROOT/out" \
  firmware/ha_x2d
```

L'UF2 de cette commande sort dans `build/arduino-standalone/out/ha_x2d.ino.uf2`.
Utiliser la pile USB Pico SDK par défaut. Le VID/PID reste celui de la carte
Arduino-Pico (2E8A:800A) ; la découverte ciblée se fait par les descripteurs
`ha-x2d` / `HA-X2D Gateway` et le numéro série physique de la flash.

## Contrat USB v1

Une ligne JSON UTF-8 par commande/réponse, terminée par LF ; CR avant LF est
accepté. Longueur maximale : **512 octets, LF compris**. Une seule demande en
attente côté hôte. Aucune émission spontanée. La ligne en dépassement est
ignorée jusqu'au LF, puis produit `line_too_long` avec `id:null`.

Demandes : `{"v":1,"id":1,"op":"hello"}` et
`{"v":1,"id":2,"op":"status"}` ; exactement ces champs, avec `id` entier
de 1 à 2147483647. Réponse valide : `{"v":1,"id":1,"ok":true,"result":{...}}`.
Erreur : `{"v":1,"id":null,"ok":false,"error":"invalid_request"}` ;
`unsupported_operation` est renvoyé pour une opération inconnue avec id valide.

`hello.result` contient `product`, `firmware`, `device_id`, `session`,
`max_line_bytes` et `capabilities`. `device_id` est l'ID flash stable sur 16
chiffres hexadécimaux majuscules ; `session` est un nouvel identifiant aléatoire
de démarrage au même format. `status.result` contient `uptime_ms`, `radio`
(`detected`, `partnum`, `version`, `marcstate`) et `tx_enabled:false`.
Quand `detected` est faux, les trois valeurs de registre sont `null`.

Le diagnostic accepte uniquement PARTNUM 0, VERSION 0x04/0x14 et l'état IDLE
0x01 après reset. Un autre module ou clone peut donc être marqué absent ;
réévaluer avec ses relevés réels. Lorsque `!Serial` est observé, le firmware
efface sa ligne partielle et les octets alors accessibles dans le tampon CDC.
Le comportement d'une reconnexion DTR très rapide et des octets déjà tamponnés
reste à vérifier sur le matériel ; l'isolation parfaite des sessions USB n'est
pas encore démontrée.
