# ha-x2d

Étude d'une passerelle **USB YD-RP2040 + CC1101 SPI** pour commander des volets
France Fermetures / Well’com depuis Home Assistant. Le périmètre demandé est
prioritairement X2D ; la génération des volets reste à identifier, avec une
piste X3D sérieuse pour Well’com.

**État au 27 septembre 2026 : premier jalon logiciel, diagnostic USB/SPI.**
Les trois parties sont séparées :

- [firmware/](firmware/README.md) : YD-RP2040, USB CDC et contrôle SPI du CC1101.
- [python/](python/README.md) : client asynchrone et CLI, indépendants de HA.
- [home_assistant/](home_assistant/README.md) : configuration native, appareil et diagnostics.

Le [contrat USB v1](docs/USB_PROTOCOL.md) relie ces trois composants. Le firmware
se compile et l'archive du composant HA se construit localement. La réception RF,
l'association et le pilotage des volets restent à implémenter après identification
du protocole. Aucun essai sur le matériel de l'installation n'a été effectué.

L'environnement de développement utilise [devenv 2.4.0](https://github.com/cachix/devenv/releases/tag/v2.4.0)
et Nix, sur Linux avec glibc. Il fournit Python 3.14, uv, un compilateur C++
et Arduino CLI natif. Les outils Arduino téléchargés utilisent le chargeur
dynamique du système ; NixOS sans couche de compatibilité FHS n'est pas couvert.
Depuis la racine du projet :

```sh
devenv shell                         # installe les dépendances et ouvre le shell
devenv test                          # tests Python/HA et contrôle C++ natif
devenv tasks run firmware:build       # compilation croisée RP2040
devenv tasks run ha:package           # archive Home Assistant
```

Les tâches se lancent aussi directement hors du shell. La première utilisation
télécharge les outils et dépendances. Les versions sont verrouillées dans
`devenv.lock` (Nix), `uv.lock` (Python, dont HA 2026.9.4) et
`firmware/ha_x2d/sketch.yaml` (Arduino-Pico et ArduinoJson).
Ces fichiers doivent être versionnés ; `.devenv/`, `build/` et `dist/` sont
des fichiers locaux ignorés. Le client `python/` est installé en mode éditable
dans l'environnement commun, tout en restant un paquet indépendant.

Le tag officiel 2.4.0 déclare encore `2.3.1` dans son
[fichier `latest-version`](https://github.com/cachix/devenv/blob/v2.4.0/src/modules/latest-version).
Cela explique l'avertissement `newer than devenv input (2.3.1)` à l'entrée du
shell, malgré le tag 2.4.0 correctement verrouillé.

Pour une mise à jour volontaire, utiliser `devenv update nixpkgs` pour les outils,
et `uv lock --upgrade` depuis le shell pour les dépendances Python, puis relancer
les validations. Les versions explicitement fixées se changent dans
`devenv.yaml`, `pyproject.toml` ou `sketch.yaml` avant de régénérer les verrous.

Les tests utilisent des pseudo-ports série Linux et Home Assistant local ;
ils n'ouvrent aucun périphérique matériel. Le contrôle C++ et les instructions
de compilation sont dans `firmware/README.md`. L'archive distribuable est
`dist/x2d-0.1.0.zip` ; aucune installation sur votre HA n'est automatique.

**Hypothèse radio à vérifier :** la documentation Franciaflex décrit Well’com comme **X3D**,
à 868,35 MHz. Des données publiques sont cohérentes avec cette piste. Cela ne
certifie pas la génération des télécommandes de l'installation : la première
capture doit départager X2D/OOK et X3D/FSK avant de figer le codec.

La direction recommandée est une **intégration locale USB directe**, avec une
bibliothèque Python indépendante de Home Assistant et un firmware chargé des
temporisations radio. MQTT reste une alternative valable, mais il faudrait
tout de même un service hôte pour relier le port USB au broker.

Deux points doivent être distingués :

- Home Assistant permet déjà une intégration X2D avec configuration graphique,
  passerelle, appareils, volets et diagnostics.
- Son menu **Connectivité** contient des entrées prédéfinies. La plateforme
  native `radio_frequency` offre une entrée RF réutilisable **si une variante
  OOK est confirmée** ; elle ne transporte pas actuellement X3D/FSK.
  Elle ne fournit pas le protocole
  X2D, sa réception, son association ou la persistance des compteurs.

Le partage précis du protocole et du compteur entre Python et RP2040 reste
un arbitrage expérimental. La préférence initiale est de conserver l'identité
d'émission et son compteur dans le dongle ; l'option d'un transport RF générique
avec protocole côté hôte mérite un essai comparatif avant de figer ce choix.
Le modem USB de paquets reste une autre possibilité pour X3D/FSK, avec sa propre
API et les écrans d'intégration HA.

Lire [l'étude et les expériences proposées](docs/EXPLORATION.md) pour les
comparaisons, les interfaces envisagées, les sources et les conditions de
changement d'architecture.

Contrôle reproductible des données publiques, sans dépendance tierce :

```sh
python3 research/verify_public_samples.py
```

Il vérifie les quatre exports HACF, six trames X3D déjà décodées et l'inverse
du calcul de compteur sur 65 536 valeurs. Il ne prouve aucune compatibilité
matérielle. La transformation adaptée de mr-sven est attribuée dans le script
et accompagnée de sa licence Apache-2.0 dans `research/LICENSE-APACHE`.

Le [dossier technique reçu](docs/DOSSIER_TECHNIQUE.md) est conservé sans
modification comme référence. Ses propositions ne sont pas des décisions
de projet. Les images, scripts de test et `SOURCES.json` qu'il mentionne
n'étaient pas présents dans la pièce jointe reçue.
