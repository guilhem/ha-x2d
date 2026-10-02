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
arduino-cli compile --profile yd-rp2040-4mb-journal \
  --build-path "$PWD/build/rx-debug/build" --output-dir "$PWD/build/rx-debug" \
  firmware/rx_debug
g++ -std=c++17 -Wall -Wextra -Werror \
  -I.devenv/state/arduino/data/internal/ArduinoJson_7.4.3_65bbd090d30b7927/ArduinoJson/src \
  firmware/check_rx_debug.cpp -o build/rx-debug/check_rx_debug
build/rx-debug/check_rx_debug
```

UF2 : `build/rx-debug/rx_debug.ino.uf2`. Les liens `sketch.yaml` et `protocol.h`
réutilisent Arduino-Pico **6.1.1**, ArduinoJson **7.4.3** et le framer existants.
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

**Protocole debug distinct du contrat USB v2 ci-dessous** : commandes ASCII
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

## Passerelle USB v2 — qualification radio incomplète

Le sketch [ha_x2d/ha_x2d.ino](ha_x2d/ha_x2d.ino) remplace le diagnostic v1,
sans compatibilité. Le [contrat USB v2](../docs/USB_PROTOCOL.md) définit les
réponses, événements et erreurs des trois composants.

Le build **0.3.0** annonce uniquement `status` et `shutters`. Il identifie la
carte, son CC1101 et l'état du journal. Il lit les slots existants ; toutes les
nouvelles allocations et émissions renvoient `profile_unverified`. Le format
d'identité accepté, le compteur initial et les trames d'association restent
à établir. Aucun compteur inventé, formatage ni émission au démarrage.
Le sketch RX debug demeure l'outil de capture pendant cette qualification.

```sh
devenv test
devenv tasks run firmware:build
devenv tasks run ha:package
```

Profil matériel `yd-rp2040-4mb-journal` : YD-RP2040, flash **4 MiB observée**,
Arduino-Pico **6.1.1**, ArduinoJson **7.4.3**. La réservation linker de 2 MiB
pour FS empêche le sketch d'occuper le journal brut de **64 KiB**, à l'adresse
`0x101FF000` (`_FS_start`). **Ne jamais monter/formater LittleFS** sur cette
zone. Le firmware refuse un emplacement linker différent. Le build vérifie
chaque adresse de l'UF2 avec `tools/check_uf2_layout.py`, puis produit
`dist/ha_x2d-0.3.0-yd-rp2040-4mb-UNQUALIFIED-RADIO.uf2`.
Les mises à jour doivent utiliser ce profil et passer cette vérification ;
un effacement total de flash ou un autre layout détruirait les associations.

[ha_x2d/journal.h](ha_x2d/journal.h) réserve durablement un compteur par
commande logique et interdit son rebouclage. Le backend matériel protège les
écritures flash par exclusion des IRQ et de l'autre cœur. Les tests natifs
injectent coupures et corruption, vérifient deux compteurs indépendants et
la file STOP. Ils ne constituent pas des essais de coupure sur la carte.
Le codec et ses temporisations sont comparés hors ligne ; aucun scheduler
PIO d'émission n'est activé sans qualification du profil et de l'association.

Le scheduler [radio_runtime.h](ha_x2d/radio_runtime.h) est raccordé au journal,
au pilote [radio_tx.h](ha_x2d/radio_tx.h) et aux opérations USB. Ses deux gates
restent désactivés dans le build distribué. Les actions du cycle B observé
sont `81` (montée), `82` (descente) et `04` (STOP). Chaque réservation précède
la construction du corps et toutes les copies utilisent le même compteur.
STOP intervient après la durée complète du dernier chip d'une trame ; les
copies utilisent une seule rafale DMA continue, vérifiée numériquement sur
la carte au repos et sous trafic USB.
Les résultats indiquent les copies complètes et les erreurs après acceptation.
L'ACK USB est mis en file avant toute émission ; les événements d'une ancienne
connexion sont ignorés après reconnexion. L'USB utilise une file de sortie
bornée et lit au plus une requête par tour pour servir la radio régulièrement.
Une saturation ferme les admissions et annule les travaux à la frontière sûre.

Le candidat TX configure l'OOK asynchrone à 868,350 MHz, deux niveaux PATABLE
avec porteuse coupée pour zéro, et vérifie les registres avant de prendre GP20.
Ces réglages suivent le [datasheet CC1101](https://www.ti.com/lit/ds/symlink/cc1101.pdf),
sections 24 et 27. Une compilation ou une mesure numérique ne confirme ni la
modulation reçue par le moteur ni son acceptation d'une nouvelle identité.

### Essai supervisé de C

Le build distribué garde la radio désactivée. Un binaire privé d'essai active
`HA_X2D_SUPERVISED_TX=1` et impose `HA_X2D_TRIAL_SUFFIX`, l'octet de groupe
d'identité observé localement. Il ne doit pas être distribué comme un profil
qualifié. `provision` crée seulement le slot 1 avec un préfixe aléatoire frais
et persiste son identité ; avant `pair`, comparer en privé l'identité du
journal lu sur la carte à toutes les identités A/B capturées. En cas de
collision, arrêter cet essai sans effacer le journal ni reprovisionner ;
une nouvelle génération demande une procédure séparée. Le seed 0 est une
hypothèse expérimentale fixe.

L'utilisateur ouvre manuellement la fenêtre d'association avec STOP sur A
en N jusqu'au va-et-vient, puis relâche. Après sa confirmation, dans la minute,
une seule opération USB `pair` envoie les deux phases observées du geste B :
24 copies de `22 02`, puis 24 de `22 20 07`, départs séparés de 2 001 ms.
Les deux compteurs consécutifs sont réservés durablement avant tout RF.
Par défaut, `HA_X2D_TRIAL_EXPECTED_NEXT_COUNTER=0` exige le prochain compteur
égal à zéro ; après consommation de 0/1, ce binaire refuse un nouvel essai,
même après redémarrage ou mise à jour. Seules les valeurs 0 et 2 sont admises.
Le binaire privé avec la valeur 2 permet une seule reprise de la même identité
C déjà persistée, aux compteurs 2/3, puis refuse dès que le prochain compteur
vaut 4, y compris après redémarrage. Il refuse un slot encore au compteur 0.
Cette reprise exige une interruption USB établie, le keep-awake Linux effectif,
une preuve numérique sans RF de 48 copies en attente silencieuse, une relecture
privée de l'identité et de la génération C originales, puis une nouvelle fenêtre
manuelle confirmée par l'utilisateur. Elle ne change ni l'identité ni le seed.
Il n'y a ni émission au boot, ni reprise, reprovisionnement ou réinitialisation
automatiques du compteur. Le fail-stop USB reste actif ; HA OS et l'acceptation
moteur restent à qualifier.

Un résultat USB `emitted` confirme uniquement la sortie de la clé. Sans
va-et-vient distinct après C, arrêter et conserver le slot et ses compteurs ;
ne pas confirmer l'association, changer de seed ou reprovisionner. Après
accusé humain, `confirm` persiste le slot ; tester STOP, puis montée/STOP et
descente/STOP sous surveillance, avec A/B disponibles, et vérifier ensuite
A/B. Le profil RF, les coupures et la latence matérielle restent à qualifier.

FREND0 sélectionne PATABLE[1] pour le niveau haut OOK ; les deux niveaux sont
relus. IOCFG0 et PKTCTRL0 sont relus avant chaque prise de GP20. Un défaut
numérique garde GP20 bas jusqu'à confirmation IDLE ; si IDLE n'est pas
confirmé, l'émission reste désactivée avec GP20 bas.

### Commander C déjà associée

L’utilisateur a confirmé montée/STOP et descente/STOP par C, puis le maintien
du fonctionnement de A/B. Un build séparé conserve cette association sans
permettre une nouvelle inscription :

```sh
devenv tasks run firmware:commands-build
```

`dist/ha_x2d-0.3.0-yd-rp2040-4mb-COMMANDS-ONLY-UNQUALIFIED-RADIO.uf2` annonce
**0.3.0-commands** et les capacités `status`, `shutters`, `command`.
`HA_X2D_COMMANDS_TX=1` active le profil de commande observé pour les slots
déjà associés ; `pair`, `confirm` et la création d’un slot restent refusés.
Il ne contient aucun suffixe personnel. Le journal, son identité C, sa
génération et ses compteurs doivent être identiques avant et après flashage.
Une clé vierge reste inerte. La veille USB de l’hôte HA OS doit être vérifiée
séparément avant usage. Ce build n’étend pas la qualification à d’autres moteurs
ni à une inscription initiale sans la reprise supervisée.

## Câblage matériel utilisé

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

## Veille USB sous Linux

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
