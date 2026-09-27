# Observations hors ligne sur les données publiques

27 septembre 2026. Aucune donnée ne provient des télécommandes A/B de
l'installation. Aucun matériel n'a été utilisé pour ces vérifications.

## Export RFPlayer publié sur HACF

Source : [message 9 de quenbo, 27 septembre 2025](https://forum.hacf.fr/t/rf-player-nemet-plus-apres-quelques-trames-sature/64980/9),
valeurs également fournies dans le dossier joint. Identifiant exporté
`F7319201`, `d0 = 0422` pour les quatre lignes.

| `d1` exporté | Compteur transformé en clair | `d2` exporté | Checksum de l'en-tête candidat |
|---|---:|---|---|
| E2DC | 6641 | C4FB | FBC4 |
| C07A | 6642 | 48FC | FC48 |
| E212 | 6643 | 8EFC | FC8E |
| 1071 | 6644 | 01FD | FD01 |

En-tête candidat : `92 31 F7 01 05 98 22 04 00` puis les deux octets du rolling
en little-endian. Sa somme additive négative donne les valeurs ci-dessus.
Cette reconstruction n'est pas unique : une somme ne prouve pas l'ordre des
octets. Le CRC de fin de trame n'est pas disponible dans ces exports.

## Flux démodulé tiers de diorcety

Source : [raw_x3d.bin](https://github.com/diorcety/X2D/blob/4d724089425481c50c14115fbbded076c1a848e2/raw_x3d.bin),
commit `4d724089425481c50c14115fbbded076c1a848e2`.

SHA-256 du fichier analysé :

```text
6dfd0b21fc3bb5db3e2e7fe9977aca04b0ff36ba4821a54742fd65f02453b711
```

L'extraction exploratoire a utilisé un script standard-library local :
regroupement des niveaux 0/1, hypothèse d'échantillonnage 400 kHz et de débit
40 kbit/s d'après `main_x3d.py`, recherche de synchronisation `8169967E`,
déblanchiment puis contrôle de longueur, checksum d'en-tête et CRC16.
Le découpage heuristique a trouvé 39 segments, dont 30 trames valides et
9 segments sans synchronisation. Ce ne sont pas 39 appuis identifiés.

Une ligne par groupe de cinq copies identiques, après déblanchiment :

| Début estimé (ms) | Identité publique | Compteur calculé | Trame |
|---:|---|---:|---|
| 1629,5 | 186054 | 140 | `14ff8b000e5460180105982282006a11fd77f812` |
| 3952,3 | 186054 | 141 | `14ff8c000e546018010598228400e80bfcfdaa6d` |
| 5695,4 | 186054 | 142 | `14ff8d000e5460180105982281004219fd98e1a4` |
| 9810,5 | 186091 | 3032 | `14ffd7000e916018010598228200a219fcfa850b` |
| 11762,1 | 186091 | 3033 | `14ffd8000e916018010598228400b0ebfc18d761` |
| 13150,2 | 186091 | 3034 | `14ffd9000e9160180105982281001e19fd7fd62a` |

Exemple des cinq débuts du premier groupe : 1629,5 / 1673,7 / 1733,8 /
1793,8 / 1853,9 ms. Le checksum d'en-tête et le CRC sont corrects pour
les 30 trames examinées. Aucun acquittement moteur ni aucune association
n'ont été démontrés par cette extraction.

La fréquence et la modulation sont absentes du fichier. La FSK à 40 kbit/s
vient des paramètres du code radio de l'auteur. Le nom Well’com vient du
rapprochement avec `X3DWellcomRemote`, pas d'une identification des télécommandes
qui ont produit le fichier. STOP contient `84` ici et `04` dans HACF : cette
différence reste à expliquer.

## Vérification conservée dans le projet

Depuis la racine :

```sh
python3 research/verify_public_samples.py
```

Résultat observé, code de sortie 0 :

```text
OK : 4 exports HACF, 6 trames X3D décodées, 65 536 aller-retour.
Ne valide ni la fréquence, ni A/B, ni l'association, ni une émission RF.
```

Ce contrôle contient les six trames ci-dessus et vérifie leurs invariants.
Il **ne reproduit pas l'extraction du flux démodulé**, ni les temps de capture.
L'extraction reste un résultat exploratoire ; les données, révision, empreinte
et hypothèses sont indiquées pour permettre son audit. Le fichier brut et le
composant diorcety sans licence générale identifiée ne sont pas recopiés ici.
