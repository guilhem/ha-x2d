# Passerelle USB RP2040 + CC1101 pour volets Well’com / X2D et Home Assistant

**Dossier de transmission à un agent de développement — 27 septembre 2026**

## 1. Mission et périmètre

L’objectif de l’utilisateur est d’intégrer ses volets roulants France Fermetures à Home Assistant avec le matériel montré : **une carte RP2040 USB-C et un module radio CC1101 annoncé pour 868 MHz**. Le résultat recherché est l’équivalent d’une télécommande supplémentaire, associée normalement au moteur, commandable depuis Home Assistant. Les télécommandes physiques existantes doivent rester utilisables.

Ce dossier rassemble les observations, sources, résultats reproductibles et questions ouvertes. **Il ne prescrit ni framework, ni langage, ni découpage hôte/microcontrôleur, ni protocole USB, ni mode d’intégration Home Assistant.** Les propositions antérieures ESPHome, Arduino-Pico, RadioLib ou MQTT sont des possibilités, pas des décisions de l’utilisateur. L’agent reste responsable de ses choix et de leur justification.

Le besoin fonctionnel de base est : **montée, descente et véritable STOP pendant un mouvement**. La position en pourcentage, le retour d’état moteur, la découverte automatique et la gestion de plusieurs volets sont des extensions à évaluer, pas des capacités déjà démontrées.

Aucune modification du câblage secteur du volet, aucun effacement de ses fins de course et aucun clonage actif de l’identité d’une télécommande existante ne sont nécessaires au principe recherché. Le coût a motivé la recherche d’une alternative au RFPlayer, mais aucun budget ferme n’a été fixé.

### Niveaux de preuve employés

- **Observation utilisateur** : photo, référence ou comportement rapporté pendant cette conversation.
- **Source examinée** : fonction, constante ou documentation effectivement consultée ; cela ne signifie pas qu’elle fonctionne sur ce volet.
- **Calcul vérifié** : résultat reproductible hors ligne, avec script et données joints.
- **Hypothèse à valider** : interprétation plausible, sans mesure ni validation matérielle sur l’installation.

**État réel du projet :** aucun firmware RP2040 n’a été compilé ou flashé dans cette préparation ; aucune capture provenant de A ou B n’est disponible ; aucune émission CC1101, association de la passerelle ou intégration Home Assistant n’a été testée. Les tests exécutés ici sont uniquement des tests de calcul et de cohérence de données publiques.

## 2. Installation et historique utiles

### 2.1 Télécommandes

Les photos et les échanges donnent les informations suivantes :

| Élément | Observation |
|---|---|
| Marque en façade | France Fermetures |
| Commandes | Montée, STOP central, descente |
| Sélecteur | N / P |
| Voyant observé | Vert lors d’un appui STOP |
| Étiquette | `7175068`, `11-37`, `DCR_3Fx`, `M2_FF_`, `AB.02-A1.00` |
| Marquage de circuit | `18490CB` |
| Télécommandes disponibles | Deux commandes identiques, désignées A et B dans la conversation |

Ces indices correspondent fortement à la famille **Franciasoft MR / Well’com**. Ils ne constituent pas à eux seuls une identification certaine de la variante radio ni du moteur. Le nom commercial précis du moteur et la fréquence réellement mesurée restent inconnus. Ne pas déduire l’année exacte du matériel du seul marquage `11-37`.

Les photos jointes sont celles de l’utilisateur : `images/telecommande_face.jpg`, `images/telecommande_dos.jpg`.

### 2.2 Incident résolu à ne pas rediagnostiquer

B a temporairement cessé de commander le volet après des essais avec le sélecteur N/P. **Le retrait puis la remise de sa pile ont suffi à rétablir son fonctionnement.** Il ne fallait donc pas conclure qu’elle était désassociée.

L’utilisateur a ensuite maintenu STOP sur B et observé **deux petits aller-retour complets du moteur**. Le Well’book distingue un émetteur de base et un complémentaire par respectivement un et deux accusés mécaniques lors du test décrit : cela rend le rôle complémentaire de B probable. Le rôle exact de A n’a pas été explicitement confirmé après ce test. Les deux commandes sont à conserver. [S03, S04]

L’utilisateur a indiqué « tout est bon » avant de passer au sujet Home Assistant. Le réglage des courses n’est donc plus l’objet de cette mission. Les anciennes manipulations de réinitialisation/fin de course ne doivent pas devenir une étape automatique de l’intégration.

### 2.3 Fréquence annoncée et portée de cette information

Franciaflex annonce pour Well’com une fréquence de **868,3 MHz** et une codification qui change à chaque manœuvre. C’est un bon point de départ documentaire, pas une mesure de A ou B. [S02]

Le mot **Well’com** recouvre des générations et équipements différents. **X2D, X3D et X2DSHUTTER ne sont pas trois noms interchangeables pour une même pile radio.** La piste principale ici est la variante volet de X2D ; elle doit être confirmée par des captures de ce matériel.

## 3. Matériel ciblé et raccordement

### 3.1 Carte RP2040 USB-C

La photo `images/rp2040.jpg` montre une carte noire avec logo Raspberry Pi sur le circuit, marquage `RP2-B2`, connecteur USB-C, GPIO exposés, boutons BOOT/USR/RST et mémoire flash externe. Le fabricant et le modèle exacts de cette carte ne sont pas certifiés. Ne pas la déclarer automatiquement Pico officiel, WeAct ou Waveshare, et ne pas reprendre une taille de flash supposée.

Le **RP2040** dispose d’USB, SPI, PIO et de 264 Ko de SRAM : les ressources sont adaptées au traitement du protocole et au compteur 16 bits. Le contrôleur USB peut fournir un périphérique USB, par exemple CDC avec une pile adaptée. [S16, S19]

Aucune radio Wi-Fi n’est identifiable sur la photo. **Ne pas confondre cette carte avec un ESP32 ou un Pico W.** L’usage USB est cohérent avec le besoin ; une solution réseau directe nécessiterait des moyens supplémentaires explicitement choisis.

À vérifier avant compilation : carte/variant exact, organisation de la flash, stratégie de démarrage, broches utilisées par les LED et boutons embarqués, et compatibilité de la pile de développement retenue.

### 3.2 Module CC1101

La seconde image, `images/cc1101_annonce.jpg`, est une **image de module/annonce**, portant les indications CC1101 et 868 MHz, avec antenne hélicoïdale. Elle ne montre pas suffisamment le brochage physique. Ne pas supposer que le module a déjà été reçu, câblé ou vérifié électriquement.

Le CC1101 est un transceiver piloté en SPI. La bande 779–928 MHz comprend la zone visée ; il supporte notamment OOK/ASK et plusieurs modulations FSK. Il dispose de FIFO RX/TX de 64 octets et de modes série synchrones/asynchrones. Ces possibilités matérielles ne fournissent pas automatiquement X2D. [S17, S18]

**Alimentation de fonctionnement du circuit : 1,8 à 3,6 V. Utiliser ici 3,3 V et une masse commune ; ne pas appliquer 5 V sur VCC ni sur les signaux logiques.** Le brochage et l’éventuelle régulation de la carte module doivent être vérifiés, pas inférés d’une autre carte CC1101. [S18, p. 8]

L’accord RF du module et de son antenne compte : « puce CC1101 programmable » ne signifie pas que n’importe quel module matériel 433 MHz donnera de bonnes performances à 868 MHz.

### 3.3 Connexion des deux cartes : possibilité déjà proposée, non obligatoire

Le raccordement logique est : **hôte USB ↔ RP2040 ↔ SPI/GDO ↔ CC1101 ↔ antenne ↔ volet**. Le CC1101 de la photo n’a pas d’interface USB autonome et ne se raccorde pas au RP2040 par un deuxième câble USB.

Une affectation possible, à retenir ou remplacer par l’agent :

| Signal du module CC1101 | Broche logique RP2040 proposée |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SCK / SCLK | GPIO18 |
| SI / MOSI | GPIO19 |
| SO / MISO, parfois partagé avec GDO1 | GPIO16 |
| CSN / CS actif bas | GPIO17 |
| GDO0 | GPIO20 |
| GDO2 | GPIO21 |

L’affectation SPI GPIO16–19 est documentée pour les variantes Pico ; GPIO20/21 sont ici des choix proposés pour les signaux auxiliaires. **Ce tableau utilise des numéros GPIO, pas des numéros de positions dans un connecteur.** Il ne certifie ni l’ordre des trous du module vert ni les fonctions internes de la carte noire. [S20]

Le câblage physique exact reste à établir depuis les sérigraphies et la documentation du module. Prévoir des connexions stables, une alimentation propre, une antenne adaptée et un montage qui évite les courts-circuits ; vérifier ces éléments avant d’attribuer un défaut au protocole.

### 3.4 Ancienne clé USB hors périmètre

La clé déjà montrée auparavant est une **YS-UTR2 V1.1**, avec **CH340T** et résonateur **LR433T2**. Elle a été identifiée dans la conversation comme une solution 433 MHz. Elle ne doit pas être confondue avec un SDR large bande ni avec le CC1101 868 MHz de ce projet. Elle ne constitue pas le récepteur de référence prévu pour ces essais.

## 4. Cartographie des bonnes sources

### 4.1 `mr-sven/x3d-rfm-esp32` : calcul du compteur transformé

C’est la source la plus importante pour le problème du rolling code. Les fonctions C **`x3d_enc_msg_id()`** et **`x3d_dec_msg_id()`** sont dans `x3d-lib/x3d.c`. La table de substitution et la dérivation de clé y sont explicites. Le dépôt annonce une licence **Apache-2.0**. La référence de branche examinée est le commit `92d9927462b93a2574986aa9e5c5b63dc5997ef4`. [S05, S06]

Le projet complet vise **X3D sur ESP32/RFM69**, pas directement RP2040/CC1101 ni le moteur de l’utilisateur. Réutiliser son calcul ne justifie donc pas de reprendre son protocole d’association, sa fréquence ou son format de trame. Sa documentation `X3D-Protocol.md` aide à comprendre le compteur, mais ses autres propriétés restent celles des appareils X3D étudiés par l’auteur. [S07]

### 4.2 `diorcety/X2D` : trames, codage radio et base embarquée

Le dépôt rassemble outils Python et code C/C++ pour X2D/X3D. Les éléments utiles sont : [S08]

| Chemin | Ce que l’agent peut y chercher |
|---|---|
| `X2D.py` | Structure de trame, types de messages, `VariationCommand`, `Enrollment`, champ optionnel `rollingCode`, checksum |
| `X3D.py` | Transformation du compteur en Python, fonctions de même nom que la version C |
| `encoding.py` | Outils de transformation des flux de bits et de packetisation |
| `bluepill/src/encoding.c`, `.h` | Primitives embarquées de codage/décodage |
| `bluepill/src/x2d.c`, `.h` | Structures et traitement X2D en C |
| `esphome/components/espx2d/cc1101_x2d.cpp` | Configuration CC1101, réception, émission et appels de codage |
| `esphome/components/espx2d/x2d_encoding.c`, `.h` | Chaîne de codage X2D / biphase |
| `esphome/components/espx2d/x2d_actuator.cpp` | Construction d’association et commandes de chauffage |

**Limite fonctionnelle :** l’actionneur examiné est orienté chauffage. Il existe une méthode `associate()`, mais elle construit un message de type `Enrollment` avec une identité `USB_Key`, sans le champ de rolling utilisé par le volet. Ce n’est pas un appairage Well’com validé. [S13]

**Limite de portage :** ce code peut dépendre d’ESPHome, d’Arduino, des abstractions de GPIO/interruptions et des versions de RadioLib. Des sources disponibles ne sont pas une garantie de compilation immédiate sur RP2040.

**Licence :** aucune licence générale à la racine n’a été identifiée dans l’arborescence examinée. Des droits explicites sont à vérifier au niveau des fichiers avant réutilisation ou redistribution. « Public sur GitHub » ne doit pas être utilisé comme synonyme de « licence libre vérifiée ».

### 4.3 `SixK/CC1101-X2D-Heaters` : référence radio, pas solution volets

Le dépôt traite du chauffage X2D avec Arduino/CC1101, avec notamment `X2D_Heater_Messages.h` et un pilote CC1101 hérité de panStamp. Il est utile comme référence complémentaire de radio, de messages et de contraintes historiques. Il ne constitue pas une preuve de prise en charge du rolling des volets. [S14]

Le pilote `cc1101.cpp` examiné comporte une licence **LGPL-3.0-or-later** et des accès directs à des ports/macro matériels : ce code n’est pas un pilote RP2040 prêt à compiler. Examiner séparément les licences des autres fichiers. [S26]

### 4.4 Bibliothèques et intégrations possibles, non imposées

**RadioLib** annonce la prise en charge du CC1101 et du RP2040. Elle peut servir de couche matérielle, sans remplacer le codec X2D. **Arduino-Pico** documente USB CDC et TinyUSB. Le SDK Pico constitue une autre direction possible à évaluer selon les besoins temporels et de maintenance. [S15, S19]

**ESPHome** possède une plateforme RP2 et un composant CC1101, mais leur existence ne signifie ni « Well’com complet » ni « API Home Assistant native sur un câble USB série ». Les dépendances du composant externe et le transport effectif restent à décider. [S20, S21, S28]

### 4.5 Ce que les autres pistes n’apportent pas

Le profil `X2DSHUTTER` de l’intégration RFPlayer contient des commandes destinées au firmware du dongle. **Ce nom est une interface de commande du RFPlayer, pas le nom d’une bibliothèque libre qui produirait les trames sur CC1101.** Les limitations de STOP observées avec ce produit ne prouvent pas que STOP soit absent du protocole du volet. [S24]

Les passerelles commerciales, le pilotage électrique de B, et les wrappers RFPlayer/Domoticz/Yadoms expliquent les alternatives explorées ; ils ne sont pas la cible du matériel retenu. Des forks ont été examinés pendant la recherche précédente, mais aucune couverture exhaustive et actuelle de tous les dépôts et branches n’est revendiquée ici. Les résultats positifs ci-dessous reposent sur des fonctions et données identifiées, pas sur une affirmation d’exhaustivité.

## 5. Résultat principal : le calcul du rolling code

### 5.1 Données de référence publiques

Le message nº 9 du fil HACF référencé [S25], publié par **quenbo le 27 septembre 2025**, donne des valeurs exportées par RFPlayer pour quatre appuis STOP successifs sur une commande France Fermetures. **Ce ne sont pas des captures de A ou B, ni des enregistrements RF bruts.**

L’identifiant exporté est `4147220993`, soit `0xF7319201`. L’hypothèse testée consiste à utiliser ses 24 bits de poids fort, soit `0xF73192`, comme identifiant pour la transformation. Le rôle de l’octet final `0x01` n’a pas été démontré par cette seule opération ; il ne faut pas généraliser aveuglément ce découpage à d’autres exports.

| Appui | `d0` STOP | `d1` décimal | `d1` hex | `d2` décimal | `d2` hex |
|---|---:|---:|---|---:|---|
| 1 | 1058 | 58076 | E2DC | 50427 | C4FB |
| 2 | 1058 | 49274 | C07A | 18684 | 48FC |
| 3 | 1058 | 57874 | E212 | 36604 | 8EFC |
| 4 | 1058 | 4209 | 1071 | 509 | 01FD |

### 5.2 Transformation identifiée

La table est :

```text
S = [1, 0, C, 8, A, 9, E, 7, 3, 5, 4, B, 2, F, 6, D]
```

Pour l’identifiant 24 bits `D`, la clé 16 bits est :

```text
K = (D & 0xFF00) | ((D & 0xFF) XOR ((D >> 16) & 0xFF))
```

Pour `D = 0xF73192`, on obtient `K = 0x3165`.

Le compteur clair `C` est transformé pendant 32 tours. Au tour `i`, un bloc de quatre bits situé à la **position de bit `i % 13`** est remplacé via la table, puis le mot est XORé avec `K`. Ce sont des fenêtres qui se chevauchent, **pas seulement quatre nibbles disjoints**. Le résultat reste sur 16 bits.

La transformation inverse applique les tours de 31 à 0, avec XOR puis substitution. La table est involutive : `S[S[x]] = x`. Ces opérations sont explicites dans les sources C et Python examinées. [S06, S10]

**Attention à la sémantique des fonctions :** la version C de mr-sven incrémente d’abord le compteur pointé, saute zéro lors du débordement, puis le transforme. La version Python de diorcety transforme directement la valeur fournie, sans l’incrémenter. Le script joint sépare volontairement ces deux opérations. Éviter une incrémentation double ou un décalage d’un message. Le saut de zéro de l’amont concerne le compteur clair, pas nécessairement le résultat transformé. [S06, S10]

### 5.3 Résultat recalculé pour ce dossier

| Valeur transmise dans l’export | Compteur décodé | Réencodage de ce compteur |
|---|---:|---|
| `0xE2DC` | **6641** | `0xE2DC` |
| `0xC07A` | **6642** | `0xC07A` |
| `0xE212` | **6643** | `0xE212` |
| `0x1071` | **6644** | `0x1071` |

On retrouve quatre compteurs consécutifs. En partant de 6641 et en l’incrémentant, les quatre valeurs publiées sont reproduites exactement. C’est une **correspondance mathématique vérifiée**, suffisamment forte pour ne pas repartir de zéro sur l’algorithme.

Le script vérifie également l’aller-retour transformation/inverse sur **les 65 536 mots possibles pour cet identifiant**. Cela valide la cohérence de la traduction Python et de son inverse ; cela ne représente pas 65 536 captures matérielles ni une validation du comportement du moteur.

Le code emploie les termes `enc`/`dec`, mais aucune conclusion générale sur la robustesse cryptographique ou sur toutes les protections du moteur n’est déduite de ces quatre données.

### 5.4 Test livré

Depuis le dossier extrait :

```sh
python3 tests/verification_rolling_x2d.py
python3 tests/verification_rolling_x2d.py --exhaustive
```

Il n’y a aucune dépendance tierce. Le script ne lit aucun périphérique, ne contacte aucun service et **n’émet aucune trame**. Les références chiffrées sont dans `tests/captures_reference.json`, les résultats exécutés dans `tests/RESULTATS.txt`.

L’identité de référence appartient aux données publiques d’un tiers. **Elle ne doit pas être utilisée comme identité d’émission sur l’installation.** Le futur émetteur doit avoir une identité propre, acceptée lors d’une association normale.

## 6. Trame X2D et commandes utiles

### 6.1 Structure publique à étudier

Le parseur `X2D.py` expose schématiquement :

```text
house (16 bits)
source (2 bits d’ID + 6 bits de type)
recipient (flags + zone sur 4 bits)
transmitter (flags, dont enrollment_requested, + attribute)
control (flags, dont rolling_code et answer_request)
data (type de message + contenu)
rollingCode (16 bits, optionnel si le flag l’indique)
checksum (16 bits)
```

Le parseur choisit notamment big-endian pour `house`, `rollingCode` et le checksum. Ce sont les conventions de ce code ; **les octets RF bruts et les entiers exportés par RFPlayer doivent être rapprochés explicitement** avant de fabriquer une trame. Le `house` 16 bits n’est pas à lui seul l’identité 24 bits testée pour la clé. [S09]

Une hypothèse de rapprochement à tester est de lire les trois octets `F7 31 92` comme `house = 0xF731` et `source = 0x92`. Dans la définition du parseur, `0x92` se décomposerait en ID de source `2` et type `18` (`Tyxia_XXYY`). Ce rapprochement est cohérent avec les champs décrits, **mais l’export RFPlayer ne prouve pas à lui seul cette correspondance avec les octets sur l’air**. Il ne faut ni en déduire arbitrairement la structure de tous les identifiants, ni traiter la valeur `0x01` finale de l’export comme un numéro de volet certain.

### 6.2 Commandes volet candidates

Le type **`VariationCommand = 0x22`** et les valeurs suivantes figurent dans le code : [S09]

| Valeur de commande | Nom dans le parseur |
|---|---|
| `0x01` | More |
| `0x81` | ShortReleasedMore |
| `0x41` | LongReleasedMore |
| `0x02` | Less |
| `0x82` | ShortReleasedLess |
| `0x42` | LongReleasedLess |
| `0x04` | Stop |

Les observations publiques relient `d0 = 0x8122` à la montée, `0x8222` à la descente et `0x0422` à STOP. Cette correspondance renforce la piste `0x22` + code de commande. [S25]

**Ne pas envoyer littéralement les deux octets `81 22` parce que l’entier exporté est `0x8122`.** Le parseur place le type avant le contenu ; un export par mots peut renverser la lecture apparente. L’ordre des octets et celui des bits sont deux problèmes distincts.

Autre détail à auditer : `VariationCommandMessage` contient `command`, `dummy1` et `dummy2`, tandis que `rollingCode` est aussi décrit séparément. La structure est entourée de mécanismes optionnels/de délimitation. Il faut vérifier la longueur réelle et la consommation de chaque octet sur de vraies captures, **pas ajouter deux zéros arbitraires** pour faire entrer les données dans le parseur.

Les noms « relâchement court/long » suggèrent plusieurs événements pour un appui. Le nombre et la séquence des trames d’un véritable appui restent à observer sur A/B ; un seul type de trame capturé ne suffit pas à certifier toute la gestuelle.

### 6.3 Checksum : ne pas le confondre avec un second rolling code

La fonction appelée `x2d_crc()` dans le parseur calcule en fait :

```text
checksum = (- somme_des_octets_du_corps) modulo 65536
```

**Ce n’est pas un CRC polynomial**, malgré le nom de fonction. Ne pas le remplacer par le CRC16 de X3D ni par le CRC matériel par défaut du CC1101. [S09]

Pour les quatre exports STOP, permuter les octets de `d2` donne respectivement `FBC4`, `FC48`, `FC8E`, `FD01`. Le calcul suivant reste constant :

```text
(swap16(d2) + octet_haut(d1) + octet_bas(d1)) & 0xFFFF = 0xFD82
```

C’est exactement le type de compensation attendu lorsque seuls les deux octets du rolling changent dans une somme de contrôle additive. **Interprétation forte : `d2` est probablement le checksum exporté avec une convention d’octets différente, pas un deuxième compteur indépendant.**

Mais l’export ne fournit pas le corps brut complet : on a vérifié un invariant entre variations, **pas le checksum d’une trame radio intégralement capturée**. Le découpage et la couverture de la somme restent à confirmer. Le script affiche explicitement cette limite.

## 7. Couche radio : pistes concrètes et pièges

### 7.1 Trois jeux de paramètres à ne pas fusionner

| Origine | Paramètres | Statut pour le volet utilisateur |
|---|---|---|
| Documentation commerciale Well’com | 868,3 MHz | Référence constructeur, non mesurée ici |
| `cc1101_x2d.cpp` de diorcety | 868,439941 MHz ; OOK ; débit configuré 4,82273 kbit/s ; bande RX 203,125 kHz | Configuration source X2D, **candidate seulement** |
| Projet X3D de mr-sven | 868,95 MHz ; FSK ; 40 kbit/s ; autres mécanismes de packetisation | **Ne pas reprendre pour X2D sur la seule base du rolling commun** |

Les deux premières fréquences diffèrent sensiblement. Il faut documenter ce qui est effectivement reçu depuis A/B avant de conclure qu’une configuration est correcte. Un RSSI seul n’est pas une validation de trame. [S02, S07, S11]

### 7.2 Éléments exacts du pilote X2D examiné

Le code de diorcety initialise RadioLib avec `begin(868.439941, 4.82273, 39.55, 203.125000, 0, 32)`, puis sélectionne OOK, désactive le filtrage CRC, désactive l’encodage matériel et le filtrage d’adresse. Il utilise aussi des réglages de synchronisation/longueur et des signaux GDO. [S11]

À la réception, il configure notamment `0x2A, 0xAB` comme mot de synchronisation, puis reconstitue un en-tête `33 33 2A AB` avant décodage. À l’émission, le buffer démarre par `FF FF` et passe par la fonction d’encodage X2D. Ces valeurs sont des **indices d’implémentation à comparer aux captures**, pas une spécification complète du protocole.

La chaîne `x2d_encoding.c` fait apparaître : conversion du buffer en bits, encodage de trame X2D, **biphase mark**, puis conversion vers le buffer de sortie. La réception applique la chaîne inverse. Ne pas assimiler automatiquement biphase mark à « activer Manchester matériel ». Ne pas appliquer deux fois un encodage déjà effectué en logiciel. [S12]

La fréquence d’horloge du quartz du module CC1101 doit être connue pour interpréter correctement les réglages du synthétiseur et du débit. L’antenne 868 MHz et le circuit d’adaptation RF du module doivent correspondre au matériel finalement utilisé. [S18]

### 7.3 Deux familles d’implémentation radio laissées ouvertes

Le CC1101 peut être employé avec sa gestion de paquets/FIFO, ou dans un mode de données directes avec traitement temporel par le microcontrôleur. Le RP2040 possède des PIO qui peuvent aider à la seconde approche. **Aucune de ces deux voies n’est imposée.** Les critères de décision sont la fidélité aux captures, la gestion des rafales et la fiabilité mesurée, plutôt que la simplicité apparente d’un exemple. [S16, S18]

Le pilote public examiné mérite un audit : il configure des longueurs autour de 255 octets, manipule des buffers de 256 octets et construit des répétitions, alors que les FIFO matérielles font 64 octets. La prise en charge d’un éventuel streaming par la version de bibliothèque doit être vérifiée. Ce constat n’établit pas un bug certain : il interdit seulement de supposer que toutes les combinaisons de versions géreront ces longueurs correctement. [S11, S18]

Il est préférable de conserver les données à plusieurs niveaux pendant les essais : impulsions ou flux brut disponibles, bits démodulés, octets de trame, champs interprétés. Une mauvaise inversion de niveau, un décalage de bit, un ordre LSB/MSB incorrect ou un préambule mal traité ne doit pas être masqué en adaptant artificiellement le parseur.

## 8. Association : mécanisme attendu et inconnues

L’association recherchée est **l’ajout d’un émetteur complémentaire autonome**, pas la réinitialisation du moteur. Les commandes physiques existantes doivent être conservées ; les réglages réservés à l’émetteur de base ne doivent pas être confondus avec l’usage d’un émetteur complémentaire.

La procédure documentaire identifiée pour cette famille utilise la commande existante pour ouvrir une fenêtre d’association, puis la nouvelle commande pour s’inscrire ; la fenêtre annoncée est d’une minute. Dans les échanges, l’appui maintenu STOP sur une commande existante a effectivement provoqué l’accusé mécanique. La séquence physique habituelle de la nouvelle commande est l’appui simultané montée + descente, en fonctionnement normal N ; le détail des trames correspondantes n’a pas été capturé ici. [S03]

L’allumage du voyant d’une télécommande n’est pas une preuve d’association. Le mouvement du produit constitue la confirmation utilisateur décrite par la notice. Un mouvement mécanique d’accusé n’établit pas en lui-même qu’un acquittement radio exploitable existe.

Les inconnues que le logiciel doit résoudre sont notamment :

- Type exact du message de nouvelle association ; combinaison de `Enrollment`, de flags et d’éventuelles commandes spécifiques.
- Identité réellement inscrite par le moteur : structure, type d’émetteur, zone, éventuel regroupement.
- Présence, valeur initiale et évolution du rolling pendant l’inscription et la première commande.
- Nombre de répétitions, durée, ordre des événements et éventuelles réponses radio.
- Possibilité de s’inscrire avec une identité synthétique sans reproduire tous les attributs matériels de B.

La fonction `associate()` orientée chauffage, qui utilise le type `USB_Key` et le code message `Enrollment = 0`, est une piste à lire, **pas une séquence d’émission à déclarer correcte sans comparaison**. [S09, S13]

Capturer une séquence d’association normale est potentiellement utile, mais ne pas désassocier A/B ni effacer le moteur uniquement pour fabriquer un jeu de données. Toute manipulation qui modifierait leurs associations doit être validée explicitement avec l’utilisateur et disposer d’une voie de récupération.

## 9. État persistant, répétitions et fonctionnement fiable

Cette section décrit des problèmes de conception à traiter ; elle ne fixe pas de solution.

### 9.1 Identité propre et état du compteur

Une passerelle qui utilise l’identité de B mais avance son compteur indépendamment pourrait créer des conflits de synchronisation. La cible est donc un **nouvel émetteur**, avec identité et état de compteur distincts de ceux de A/B. Le découpage exact entre identité, maison, type et zone reste à confirmer dans le protocole.

L’état doit survivre aux redémarrages du RP2040, de l’hôte et de Home Assistant. L’agent doit choisir le propriétaire de cet état et traiter l’atomicité, les commandes en vol, la restauration d’une sauvegarde ancienne, la réinstallation du firmware et la perte d’alimentation pendant une écriture.

Ne pas réserver arbitrairement de grands blocs de compteurs pour économiser des écritures flash : le moteur peut imposer une fenêtre d’acceptation inconnue. Le projet X3D décrit sa propre politique d’acceptation ; **elle n’a pas été démontrée sur les moteurs Well’com X2D**. [S07]

### 9.2 Répétition radio versus nouvelle commande logique

Il faut distinguer une nouvelle action utilisateur, les copies radio d’une même action, les événements appui/relâchement, et une relance logicielle après une erreur USB. On ne sait pas encore exactement lesquels consomment un compteur sur cette télécommande.

L’absence d’acquittement ne prouve pas qu’un ordre n’a pas été exécuté. Inversement, le succès de l’appel SPI/TX signifie que la radio a accepté l’émission, pas que le volet a bougé. Les files de commandes et les relances doivent éviter de réexécuter des mouvements périmés. STOP mérite un traitement qui ne le laisse pas attendre derrière une longue file de commandes devenues inutiles.

La logique périodique du chauffage ne doit pas être copiée par défaut dans un actionneur volet : une demande de mouvement n’est pas une consigne de température à rafraîchir indéfiniment.

### 9.3 Perte de liaison, sûreté et diagnostic

Le démarrage, la reconnexion USB et la reprise après erreur ne doivent pas provoquer d’émission spontanée de mouvement ou d’association. Prévoir un état sans émission tant que l’identité et le compteur persistants ne sont pas considérés valides.

Pendant les essais : réception seule avant émission, essais surveillés, STOP physique disponible, pas de séquences de mouvement longues destinées seulement à multiplier les captures. Les raccordements électriques du volet ne font pas partie du banc basse tension.

La configuration finale doit prendre en compte les limites radio applicables au pays d’usage : sous-bande, puissance, temps d’émission et éventuel mécanisme d’accès. Aucune autorisation d’émettre à puissance maximale ni valeur réglementaire chiffrée n’est déduite de la seule capacité de la puce.

## 10. Home Assistant : besoin fonctionnel, choix ouverts

### 10.1 USB n’est pas automatiquement une intégration Home Assistant

Une architecture USB doit fournir à la fois un protocole côté périphérique et un consommateur côté hôte. Un port CDC de logs ne suffit pas. **Flasher ESPHome sur un RP2040 sans réseau ne donne pas automatiquement l’API native Home Assistant via USB** : l’API native documentée est un protocole réseau. [S19–S21]

Les pistes possibles comprennent un service USB–MQTT, une intégration Home Assistant qui gère directement le périphérique, une émulation volontaire d’un protocole de passerelle déjà pris en charge, ou un autre transport documenté. Le format des messages peut être binaire ou textuel ; aucun exemple antérieur de YAML, de topic ou de classe C++ ne constitue une interface contractuelle.

Le choix doit intégrer le type réel d’installation Home Assistant. Ne pas reprendre les versions, l’architecture, les adresses IP ou les add-ons du **forum de captures** comme s’il s’agissait de la machine de l’utilisateur.

### 10.2 Entité `cover` et fidélité de l’état

L’entité visée est un volet avec ouverture, fermeture et arrêt. Home Assistant distingue ces fonctions et le positionnement. **Ne pas annoncer `SET_POSITION` ni une position réelle si le matériel/protocole n’a pas fourni de moyen fiable de les connaître.** [S23]

MQTT Cover, s’il est retenu, fournit commandes, disponibilité et états/positions ; sans retour d’état, son mode optimiste ne mesure rien. Une estimation temporelle demeure une estimation. [S22]

La réception des commandes de A/B peut aider à suivre les mouvements, sans garantir que le moteur a exécuté l’ordre : perte radio, obstacle ou arrêt local restent possibles. Au redémarrage, une position inconnue est plus honnête qu’une position supposée exacte. Ne pas publier un volet « fermé » uniquement parce qu’une émission de fermeture a réussi.

### 10.3 Points d’intégration à arbitrer

Identifiants stables, déconnexion/reconnexion USB, version du protocole, séparation logs/données, gestion d’un seul propriétaire du port, remontée des erreurs et disponibilité doivent être prévus. Avec MQTT, traiter la découverte et les doublons/reconnexions sans rejouer involontairement de vieilles commandes ; le maintien en mémoire d’une commande de mouvement doit être choisi en connaissance de ses conséquences. [S22, S29]

Pour un transport USB série, distinguer l’identifiant matériel stable, le périphérique de bootloader et le périphérique applicatif ; les noms de port transitoires ne doivent pas être considérés comme une identité persistante. Le débit déclaré d’un port CDC et le débit radio CC1101 sont deux paramètres distincts. Avec MQTT, examiner explicitement les messages de commande conservés (`retain`) et les redélivrances liées au QoS : une reconnexion ne doit pas devenir une nouvelle action moteur par accident.

Un boîtier USB peut représenter plusieurs identités d’émetteurs en logiciel ; ce n’est cependant pas une capacité validée sur ces moteurs. La portée, les associations de groupe et la gestion multi-volet restent des sujets indépendants du calcul de rolling.

## 11. Validations utiles et critères de preuve

L’ordre et l’outillage restent à l’agent. Les résultats suivants permettraient de distinguer clairement les couches validées :

| Domaine | Preuve utile |
|---|---|
| Matériel | Brochage exact documenté ; alimentation correcte ; communication SPI avec le composant identifié |
| Calcul | Quatre vecteurs publics reproduits ; tests de limites, d’inverse et de sémantique d’incrément |
| Réception réelle | Captures de A/B avec paramètres radio consignés et octets reproductibles |
| Structure X2D | Longueurs, flags, ordre des bits/octets et checksum validés sur les trames complètes |
| Compteur de A/B | Compteurs cohérents sur des appuis successifs, sans mélanger répétitions et nouveaux événements |
| Émission | Trame émise observable et conforme au format attendu, pas seulement un retour de fonction « success » |
| Association | Nouvelle identité acceptée sans supprimer A/B ni modifier les courses |
| Commandes | Montée, descente et STOP exécutés par le volet, avec distinction des succès et échecs |
| Persistance | Redémarrages et coupures sans régression du compteur ni commande spontanée |
| Home Assistant | Entité disponible/indisponible cohérente, commandes utiles, état non mensonger |

Un corpus utile conserverait, pour chaque événement : commande physique, télécommande utilisée, appui court/long, heure monotone, configuration radio, données brutes disponibles, représentation décodée, résultat du checksum et groupe de répétitions. Conserver les captures invalides pour le diagnostic, tout en évitant de les utiliser comme commandes valides.

Des appuis STOP peuvent limiter les mouvements durant la capture, mais un appui prolongé peut ouvrir l’association : distinguer explicitement les gestes. Aucun volume fixe de mouvements n’est requis pour satisfaire ces critères.

## 12. Incertitudes et raccourcis à éviter

Les inconnues les plus importantes sont le brochage du module exact, les réglages radio réellement adaptés à A/B, le format d’association, les champs d’identité, la politique du compteur et le comportement d’acquittement du moteur.

Les éléments suivants **ne sont pas acquis** :

| Raccourci | État correct |
|---|---|
| « Le modèle de moteur est connu avec certitude » | Famille fortement probable, référence exacte non disponible |
| « Tout Well’com utilise la même pile » | La génération doit être confirmée par les captures |
| « Le rolling reste à découvrir » | Une transformation reproduit déjà quatre données publiques |
| « Le rolling est validé sur B » | Aucun signal de B n’a encore été capturé ici |
| « X3D et X2D partagent donc tout le protocole » | Seule la correspondance de transformation est démontrée sur ce corpus |
| « `d1` et `d2` sont deux rolling codes » | `d1` correspond au compteur transformé ; `d2` est cohérent avec un checksum |
| « Le CRC X2D est CRC16-CCITT » | Le parseur examiné utilise une somme additive négative |
| « Le 868,44 MHz de l’exemple est la fréquence certifiée du volet » | Réglage d’un dépôt, différent du 868,3 MHz annoncé par Franciaflex |
| « Le CC1101 supporte X2D dans son firmware » | C’est une radio configurable ; le protocole reste logiciel |
| « Deux octets `dummy` doivent être ajoutés » | Leur signification et la longueur réelle restent à confirmer |
| « Association chauffage = association volet » | Non démontré |
| « Une API ESPHome passe nativement sur un port de logs USB » | Non établi ; un transport effectif est nécessaire |
| « STOP impossible parce que RFPlayer ne l’expose pas » | Des captures et constantes candidates existent |
| « Les dépôts publics sont tous réutilisables sans formalité » | Vérifier les licences et provenances fichier par fichier |

## 13. Ce que contient le paquet transmis

`DOSSIER_TECHNIQUE.md` est la synthèse autonome ; `tests/` contient les données, la vérification hors ligne et ses résultats. `images/` contient les photos de référence. Le script de test est accompagné d’une attribution et du texte Apache-2.0 applicable à sa partie adaptée de mr-sven.

Les sources des dépôts ne sont **pas** vendues comme un firmware prêt à l’emploi et ne sont pas recopiées intégralement dans le paquet. Les références ci-dessous permettent à l’agent de vérifier leur évolution, les dépendances et les licences avant de décider comment les utiliser.

Pour son propre livrable, l’agent pourra retenir le format qu’il juge pertinent. Il est souhaitable de conserver les captures, les tests de référence, les versions de dépendances, les commandes reproductibles de build/flash et la frontière entre vérifications logicielles et résultats sur matériel. Ce sont des éléments de preuve, pas une architecture imposée.

**Conclusion opérationnelle : le meilleur point de départ n’est ni un enregistrement/rejeu naïf de B ni une réinvention complète de X2D. Il existe une base de trames/radio et une transformation de compteur vérifiée sur des captures publiques. Le travail décisif reste la confrontation à A/B, l’association d’une identité propre et la robustesse de la chaîne jusqu’à Home Assistant.**

---

## Références consultées

Les liens suivent l’état consulté pour ce dossier. Les SHA de blobs de code significatifs figurent dans `SOURCES.json` ; un SHA de blob identifie un fichier, pas un commit de tout le dépôt.

**[S02] Franciaflex — Automatismes / Well’com.** Fréquence annoncée, codification changeante, distinction entre commandes individuelles et de groupe. https://www.franciaflex.com/page/automatismes-2

**[S03] Well’book — Ajouter un émetteur complémentaire, p. 87.** https://pro.franciaflex.com/medias/FX-ex/book/wellbook/87-Ajouter-un-emetteur-complementaire.html

**[S04] Well’book — Reconnaître un émetteur de base / complémentaire, p. 89.** https://pro.franciaflex.com/medias/FX-ex/book/wellbook/89-Reconnaitre-un-emetteur-de-base-complementaire.html

**[S05] mr-sven/x3d-rfm-esp32.** https://github.com/mr-sven/x3d-rfm-esp32

**[S06] Algorithme C et incrémentation, version de référence.** https://github.com/mr-sven/x3d-rfm-esp32/blob/92d9927462b93a2574986aa9e5c5b63dc5997ef4/x3d-lib/x3d.c

**[S07] Analyse X3D — ne pas confondre avec la couche radio X2D.** https://github.com/mr-sven/x3d-rfm-esp32/blob/92d9927462b93a2574986aa9e5c5b63dc5997ef4/X3D-Protocol.md

**[S08] diorcety/X2D — dépôt et organisation.** https://github.com/diorcety/X2D

**[S09] Structures X2D, commandes, rollingCode et checksum.** https://github.com/diorcety/X2D/blob/master/X2D.py

**[S10] Transformation du compteur en Python.** https://github.com/diorcety/X2D/blob/master/X3D.py

**[S11] Radio CC1101 X2D et paramètres effectifs du code.** https://github.com/diorcety/X2D/blob/master/esphome/components/espx2d/cc1101_x2d.cpp

**[S12] Codage embarqué X2D / biphase mark.** https://github.com/diorcety/X2D/blob/master/esphome/components/espx2d/x2d_encoding.c

**[S13] Actionneur ESPHome orienté chauffage et Enrollment.** https://github.com/diorcety/X2D/blob/master/esphome/components/espx2d/x2d_actuator.cpp

**[S14] SixK/CC1101-X2D-Heaters.** https://github.com/SixK/CC1101-X2D-Heaters

**[S15] RadioLib — prise en charge radio et plateformes.** https://github.com/jgromes/RadioLib

**[S16] Raspberry Pi — documentation des microcontrôleurs.** https://www.raspberrypi.com/documentation/microcontrollers/microcontroller-chips.html

**[S17] Texas Instruments — CC1101.** https://www.ti.com/product/CC1101

**[S18] Texas Instruments — datasheet CC1101 SWRS061I.** Alimentation p. 8 ; interface SPI, FIFO et modes radio dans les chapitres correspondants. https://www.ti.com/lit/ds/symlink/cc1101.pdf

**[S19] Arduino-Pico — USB et TinyUSB.** https://arduino-pico.readthedocs.io/en/latest/usb.html

**[S20] ESPHome — plateforme RP2, GPIO et logs USB.** L’ancien lien `rp2040` redirige vers cette page au moment de la consultation. https://esphome.io/components/rp2/

**[S21] ESPHome — API native réseau.** https://esphome.io/components/api/

**[S22] Home Assistant — MQTT Cover.** https://www.home-assistant.io/integrations/cover.mqtt/

**[S23] Home Assistant Developer Docs — Cover entity.** https://developers.home-assistant.io/docs/core/entity/cover/

**[S24] Profil X2DSHUTTER de l’intégration RFPlayer.** https://github.com/gce-electronics/HA_RFPlayer/blob/main/custom_components/rfplayer/device-profiles.yaml

**[S25] Corpus public HACF, message nº 9 de quenbo, 27 septembre 2025.** Données de première main rapportées par leur auteur ; interprétations du forum non reprises automatiquement. https://forum.hacf.fr/t/rf-player-nemet-plus-apres-quelques-trames-sature/64980/9

**[S26] Pilote CC1101 SixK / panStamp et en-tête de licence.** https://github.com/SixK/CC1101-X2D-Heaters/blob/master/cc1101.cpp

**[S28] ESPHome — composant matériel CC1101.** https://esphome.io/components/cc1101/

**[S29] Home Assistant — MQTT, découverte et disponibilité.** https://www.home-assistant.io/integrations/mqtt/
