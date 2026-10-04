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

## Nouvelle preuve RX reproductible — 1 octobre 2026

`tools/analyze_rx.py` analyse hors ligne les JSONL produits par
`tools/collect_rx_debug.py`, uniquement avec la bibliothèque standard Python.
Les captures de l'installation et les rapports détaillés restent privés dans
`build/` : aucun identifiant ni corps de trame de l'installation n'est conservé
dans les sources ou les tests publics. Cette section publie seulement les
résultats agrégés. L'analyse n'ouvre aucun périphérique et n'émet rien.

Le lecteur extrait GDO0 des échantillons emballés sur deux bits, sans confondre
GDO2 avec les données. La fréquence réelle est calculée à partir de
`system_clock_hz * 256 / divider_256` : **400 kHz** sur les captures examinées,
indépendamment du débit radio annoncé par le CC1101. Les indices et temps sont
conservés dans leur epoch. Les changements d'epoch/configuration/horloge,
ruptures d'indices/chunks/séquence, indicateurs de perte et changements de
compteurs de perte séparent les segments ; aucune trame ne traverse une telle
frontière. L'indicateur de perte initial est rapporté, même sans rupture interne.
Les enregistrements malformés provoquent une erreur avec le numéro de ligne.

La recherche sélectionne des groupes de pulses hauts assez longs, puis essaie
une grille de durées de chip, phases et seuils, ou une quantification des runs
avec compensation de largeur des pulses OOK. Chaque copie acceptée possède
un préfixe ou séparateur de répétition, une fin de trame, les transitions aux
frontières biphase mark, un stuffing valide et un nombre entier d'octets LSB
first. Le checksum est la somme négative de tous les octets du corps sauf les
deux derniers, comparée à ces deux octets en big-endian ; **ce n'est pas un CRC**.
Aucune correction de bits, suppression de glitch ou décision par vote.

Les variantes de paramètres d'une même copie sont dédupliquées dans une
fenêtre d'un chip, séparément pour chaque corps et segment. Pour les corps de
12 octets, le rapport donne l'identité dans l'ordre RF, les octets d'en-tête et
de commande, le rolling LE16 et son inverse via la transformation existante de
`research/verify_public_samples.py`. Les deltas de compteur sont modulo 65536.
Les autres longueurs restent des corps bruts validés. Les commandes ne sont
pas étiquetées automatiquement montée/STOP/descente ; leur interprétation
exige une séquence d'appuis confirmée. La cohérence des compteurs ne tranche
pas l'ordre des octets de l'identité : la clé est symétrique par inversion.

Validation sur les anciennes captures, avec les paramètres par défaut :

| Capture privée | Groupes d'activité / décodés | Copies strictes | Comparaison à l'analyse exploratoire |
|---|---:|---:|---|
| FSK STOP | 6 / 6 | 147 | Les 136 copies antérieures sont retrouvées, mêmes corps et compteurs |
| OOK STOP | 6 / 5 | 84 | Les 84 copies antérieures sont retrouvées, mêmes corps et compteurs |

Les extractions ont été revérifiées depuis leurs grilles sur les samples bruts,
avec framing, stuffing et checksum. Ces décomptes sont des **bornes inférieures** :
la recherche réduite n'essaie pas toutes les grilles des scripts privés et ne
mesure pas le taux de réception. Une absence de résultat ne prouve pas l'absence d'émission.
Le sélecteur d'activité peut manquer un groupe ; les paramètres réduits peuvent
manquer des copies bruitées. La RAM utilisée dépend du plus grand segment
continu, environ un octet par sample, plus les fenêtres de recherche.

La capture FSK de 600 s `open-stop-close-fsk-01.jsonl` contient 239 992 832
samples dans un segment sans rupture interne, avec 26 941 275 fronts GDO0.
La recherche par grille trouve un groupe candidat de 51 pulses sur 1,73 s,
à environ 557,64 s, mais **aucune trame valide**. L'ordre des
appuis n'étant pas encore confirmé, elle ne démontre aucune séquence de commandes.
Le rapport privé est dans
`build/implementation-2026-10-01/open-stop-close-fsk-01-analysis.json`.
La preuve de relecture des anciennes captures et les statistiques de la nouvelle
sont dans `build/implementation-2026-10-01/analysis-validation.json`.

Utilisation rapide sur une nouvelle capture montage/STOP/descente :

```sh
python3 tools/analyze_rx.py build/captures/montage-stop-descente.jsonl \
  --method grid --output build/captures/montage-stop-descente-analysis.json
python3 -m unittest discover -s tests -p test_radio_analysis.py -v
```

Le rapport est créé exclusivement, sans écraser un fichier existant ; sans
`--output`, il est écrit sur stdout et contient des données privées. Les options
`--chip-us`, `--phase-step-us`, `--thresholds`, `--window-us`, `--bias-us`,
`--gap-s`, `--min-pulses` et `--max-frame-bytes` règlent la recherche.
Les listes sont séparées par des virgules ; `--method pulse` sélectionne la
quantification des runs et `--method both` (défaut) combine les deux méthodes.

API Python, depuis la racine du projet :

```python
from tools.analyze_rx import analyze_capture

report = analyze_capture("build/captures/montage-stop-descente.jsonl", method="grid")
for group in report["groups"]:
    for body in group["bodies"]:
        if "rolling_clear" in body:
            print(group["group"], body["command_bytes"], body["rolling_clear"],
                  body["validated_physical_copies"])
```

Le rapport contient les paramètres, l'empreinte SHA-256 du JSONL, les segments,
groupes, corps et copies avec coordonnées et paramètres d'extraction. Les temps
localisent les centres de chips de données, pas le premier front RF ni l'heure
hôte. Les six tests utilisent des identités synthétiques et un en-tête candidat
HACF public encodé synthétiquement : ce dernier n'est pas une capture brute
publique X2D. Ils vérifient aussi les pertes, epochs, horloges, répétitions,
inversions de polarité et rejets de corruption. Ces preuves ne qualifient ni
l'association, ni une émission, ni le mouvement du volet ; la configuration RX
ne mesure pas la modulation ou la fréquence centrale de l'émetteur.

## Association et qualification opérationnelle — état du 1er octobre 2026

La [procédure constructeur Well’book S03, p. 87](https://pro.franciaflex.com/medias/FX-ex/book/wellbook/87-Ajouter-un-emetteur-complementaire.html)
demande STOP maintenu sur l’émetteur existant jusqu’au va-et-vient, puis montée
et descente simultanées sur le nouvel émetteur, jusqu’au va-et-vient, dans la
minute. La notice Franciasoft MR distingue émetteur de base et complémentaire ;
le rôle de A reste à confirmer. Cette procédure ne fournit pas les octets RF,
le profil d’émission ni le compteur initial d’une identité neuve. Le geste
montée + descente d’un émetteur déjà inscrit peut être observé passivement,
mais ne prouve pas une première association. Ne pas construire le payload
par analogie avec STOP, le chauffage X2D ou Well’com X3D.

Les deux nouvelles fenêtres FSK de 600 secondes et la fenêtre OOK de 600 secondes
n’ont pas d’appuis confirmés ni de commande attribuée. La fenêtre OOK contient
23 copies strictement valides d’un même corps de 12 octets entre 560,70 et
561,89 secondes ; BMC, stuffing et checksum sont vérifiés depuis les samples.
Les octets concordent avec le STOP historique d’une télécommande connue,
avec un compteur avancé de un ; l’appui effectif reste à confirmer. Les captures restent privées
dans `build/implementation-2026-10-01/`. Une nouvelle capture doit être armée
avec l’utilisateur présent : A puis B, montée, STOP, descente et STOP. Les
commandes de mouvement et l’association ne sont pas qualifiées.

La passerelle v2 **0.3.0** a été flashée et testée en USB sur cette carte :
100 lectures de statut, 20 demandes concurrentes, 20 reconnexions ; cache
identique, conflit de doublon, ancienne session, ancien ID évincé et ligne trop
longue contrôlés. La médiane de réponse de statut est de **3,2 ms** sur cet essai ;
ce n’est pas une mesure de latence STOP. `tx_enabled` reste faux et les six
opérations de mutation testées sont refusées avec `profile_unverified`.
Les 64 KiB du journal étaient effacés avant l’essai et identiques octet par octet
après le flashage et les demandes refusées. Cela vérifie la préservation d’une
zone vierge, pas la préservation d’associations existantes ni une coupure
pendant écriture. Le récepteur passif a ensuite été restauré.

Les tests automatisés couvrent le codec, le journal et sa récupération, les
compteurs séparés, la file STOP et deux sous-entrées HA sur transport PTY.
Les gestes physiques du parcours d’association restent bloqués ; son parcours
de test et confirmation a été exercé en simulation. La revue indépendante a
fait réserver le dernier compteur et 16 records durables pour STOP ; ces
corrections sont testées. Le scheduler devra assurer la maintenance au repos
avant de réadmettre des mouvements, sans promettre un budget STOP infini.

### Cycle A/B confirmé et capturé

L’utilisateur confirme montée, descente et STOP pendant les deux mouvements
avec A et B. La capture passive OOK à 868,350 MHz du 1er octobre, commencée
à 16:48:06 UTC, contient 61 937 664 échantillons à 400 kHz, sans rupture
d’indices ni de séquence. Son unique indicateur de perte est au début du
segment ; il ne traverse aucune trame acceptée. Les données privées restent
dans `build/captures/` et les rapports dans `build/implementation-2026-10-01/`.

L’analyse trouve **98 copies strictes** de corps de 12 octets et deux identités
distinctes, rapprochées en privé des captures STOP historiques de A et B.
Pour B, la séquence complète contient les octets de
commande `22 81`, `22 04`, `22 82`, `22 04`, avec respectivement 21, 25, 24 et
25 copies et une progression de compteur de un à chaque commande suivante.
Leur ordre concorde avec montée, STOP, descente et STOP observés. Pour A,
seules trois copies de STOP sont décodées dans cette recherche, sans pouvoir
attribuer ce STOP à l’un des deux appuis du cycle.
Les groupes d’activité peuvent réunir deux appuis ; cinq groupes ne signifient
donc pas cinq commandes. Aucun identifiant ni compteur réel n’est publié.

Cela établit les actions du cycle observé, mais ne qualifie ni le profil TX,
ni une nouvelle identité C, ni son association. Au test documenté STOP 5 s sur
A en N, l’utilisateur observe ensuite **un va-et-vient** : A est donc identifiée
comme émetteur de base selon S04. La capture de cet appui contient 50 copies
strictes : 25 d’un STOP de 12 octets (`22 04`), puis 25 d’un corps distinct de
15 octets (`22 20`), dont le début est 5,004 s après celui du STOP. Le préfixe
d’identité correspond à A ; les corps sont constants dans chaque train. Le
compteur candidat du corps étendu est le suivant, mais sa disposition et le
sens de ses champs supplémentaires restent à établir. Aucune seconde identité
valide ni réponse RF du moteur n’est démontrée. La capture n’a aucun trou
d’indices ou de séquence, et la radio est vérifiée au repos après sa fermeture.

Dans le parcours Home Assistant, l’utilisateur ouvre lui-même le mode
association avec sa télécommande existante, puis confirme sa préparation.
La clé envoie seulement la demande d’inscription de sa nouvelle identité C ;
elle n’attend ni ne reproduit les trames de A. Les captures de A servent au
développement, sans imposer leur décodage comme prérequis de l’installation.

### Geste simultané confirmé sur B

Le premier essai montée+descente avait été attribué à B ; l'utilisateur l'a
corrigé : il l'avait réalisé sur **A**. Le rapprochement des identités RF
historiques confirme cette correction. Ce premier essai ne prouve donc pas
le geste de B.

La capture suivante, commencée à 17:36:47 UTC, contient 239 992 832 samples
à 400 kHz, sans rupture de séquence ni d'indices. L'utilisateur effectue
d'abord descente/STOP sur A puis B, ensuite montée+descente sur **B en N**
pendant 2 secondes, et rapporte un va-et-vient. Le dernier geste contient
24 copies strictes d'un corps de 12 octets (`01 85 98`, action `22 02`),
puis 24 copies d'un corps de 13 octets (`01 05 98`, action `22 20`). Les
débuts des deux trains sont séparés d'environ 2,001 s. La position candidate
du rolling est décalée d'un octet dans le corps étendu ; son compteur décodé
est le suivant. L'octet supplémentaire correspond à celui du geste sur A.
Ces copies sont des minima de la recherche bornée, pas une mesure du nombre
total émis. Elles établissent une séquence authentique de B, mais pas
l'acceptation d'une identité encore inconnue du moteur ni son compteur initial.
L'octet supplémentaire est `07`. Les deux débuts de train comprennent huit
bits zéro puis le préfixe de six bits un et un zéro, revérifiés directement
sur les échantillons bruts avec les grilles des premières copies strictes.
Les copies successives n'ajoutent aucun chip idle entre EOF et préfixe.

Après ce geste, l'utilisateur vérifie de nouveau descente puis STOP avec A
et B et confirme que tout fonctionne. Une capture séparée retrouve 93 copies
strictes : 21 descente et 23 STOP pour A, 24 descente et 25 STOP pour B.
Les compteurs progressent dans chaque paire. Cette réception de A est plus
complète que celle du premier cycle ; elle ne mesure pas la portée maximale.
Les rapports et captures personnels restent uniquement dans `build/`.
La carte est vérifiée au repos après fermeture : RX/TX désactivés, IDLE.

Le codec et le pilote continu construisent désormais les deux phases observées
de B. Cent contrôles numériques sur la carte, dont cinquante sous trafic USB,
confirment trois copies synthétiques complètes à 208,5 us/chip, avec des écarts
de jonction de -2 à +1,5 us à la résolution de mesure de 2,5 us. Le CC1101 reste
IDLE et aucun RF n'est activé. Les deux PIO partagent la même horloge : cela
ne qualifie ni sa fréquence absolue, ni l'émission RF, ni l'acceptation moteur.
Les rapports personnels et le binaire d'essai restent sous `build/`.

### Premier essai C et veille automatique USB

Après ouverture manuelle de la fenêtre avec A, la clé accepte une seule
opération `pair`. Aucun va-et-vient distinct après C n'est observé. Le client
expire en attente du résultat d'émission ; l'association n'est pas confirmée.
La lecture physique du journal vérifie deux réservations consécutives
commises, avec C toujours en attente. L'identité et les compteurs sont conservés.
Cela ne prouve pas que les deux rafales RF ont été entièrement émises.

Un diagnostic dérivé du même firmware, sans STX et avec journal synthétique
en RAM, reproduit le résultat USB manquant pendant une attente silencieuse.
L'instrumentation constate une annulation de session ; des requêtes `status`
espacées de 250 ms permettent au contraire d'obtenir les 48 copies numériques
et leur événement. Linux configure la clé avec veille automatique à 2 000 ms,
état `suspended` et réveil désactivé. Le signal de connexion de la bibliothèque
USB devient faux lors de cette veille, ce qui déclenche l'annulation prévue
par le firmware. Cette reproduction isole l'effet du lien USB ; elle ne
qualifie pas le signal RF de C. Une règle udev ciblée est préparée et vérifiée ;
après son installation, `power/control=on` et `runtime_status=active` sont
vérifiés. Un contrôle numérique silencieux reçoit le résultat des 48 copies
en 3,24 s, sans requêtes intermédiaires, puis vérifie le statut dans la même
connexion après trois secondes supplémentaires au repos. Le CC1101 reste
IDLE et le journal synthétique est exclusivement en RAM. Le journal physique
de C relu ensuite est identique octet par octet à celui du premier essai
incertain : même identité en attente et mêmes deux compteurs consommés.

Cette correction du lien USB autorise la préparation d'une reprise supervisée
unique, avec la même C et les deux compteurs suivants. Elle exige une nouvelle
fenêtre ouverte manuellement et un binaire privé borné à ces compteurs ; aucun
reset, changement de seed, rejeu automatique ou confirmation de C n'en découle.
La reprise supervisée a ensuite été réalisée avec la même identité C et les
deux compteurs suivants, après une nouvelle fenêtre ouverte par l’utilisateur.
Le résultat USB indique 48 copies complètes et la radio revient en IDLE.
L’utilisateur observe deux va-et-vient pendant l’émission de C. Ce geste
d’inscription ne permet pas d’attribuer le rôle BASE ou complémentaire selon
S04, qui décrit un autre geste : STOP maintenu cinq secondes.

Après confirmation explicite de cette réponse moteur, C est marquée associée
dans le journal. L’utilisateur confirme deux essais réels : montée puis STOP,
et descente puis STOP. Pour chacun, STOP est demandé environ une seconde après
le mouvement et interrompt sa rafale à une frontière de trame complète. Les
25 copies de STOP se terminent respectivement 1,215 s et 1,234 s après la
demande USB. Ces durées mesurent la fin de la rafale, pas le délai d’arrêt
physique du moteur. L’utilisateur vérifie ensuite A et B et confirme qu’elles
fonctionnent toujours. Aucun compteur n’est remis à zéro, aucune nouvelle
identité n’est créée et aucune commande n’est rejouée automatiquement.

Le firmware privé d’essai pilote donc le premier moteur avec C. La réception
opérationnelle dans le firmware passerelle, la généralisation de l’inscription,
la latence physique de STOP, les coupures matérielles et les mises à jour d’un
journal réellement utilisé restent à qualifier. Le MCP
confirme HA OS 18.3 et Core 2026.9.4 ; l’installation et les essais sur cette
instance restent à réaliser. La validation du cycle de C ne constitue pas
une validation de plusieurs moteurs ou d’un autre matériel radio.

### Mise à jour conservant C, avant installation HA

Le build séparé **0.3.0-commands** est compilé et relu indépendamment. Il permet
les commandes des slots déjà associés et refuse `pair`, `confirm` et la
création d’un slot. Il ne contient aucun suffixe personnel. Le flux HA propose
les slots associés sans les reprovisionner ; le parcours de test et la
confirmation physique restent explicites.

Les 23 tests Python et les contrôles natifs passent avec cette modification.
Les tâches `firmware:build`, `firmware:commands-build` et `ha:package` passent.
La carte reçoit l’UF2 relu, vérifié par le chargeur et par une relecture de
toute sa région programme. Les deux lectures physiques des 64 KiB du journal,
avant et après mise à jour, sont identiques octet par octet. L’identité de C,
sa génération, son état associé et ses réservations de commande sont conservés.
Les dumps personnels restent sous `build/`.

Après redémarrage, USB confirme **0.3.0-commands**, les seules capacités
`status`, `shutters`, `command`, C associée avec dernière intention STOP et la
radio IDLE. Les trois opérations d’inscription sont refusées ; la lecture
d’un slot existant par `provision` est sans modification. Aucune nouvelle
émission RF n’est effectuée pendant ces vérifications. La veille USB est
désactivée sur Ubuntu ; ce comportement reste à vérifier séparément sur HA OS.
L’archive d’intégration est prête ; l’utilisateur choisit de l’installer
lui-même. Le tableau de bord, l’automatisation et la reprise après redémarrage
sur son instance restent à tester.

### Installation sur HA OS et pilotage de C

L’utilisateur installe l’archive manuelle, redémarre HA et branche la clé sur
HA OS 18.3 / Core 2026.9.4. Les diagnostics MCP confirment la connexion au
firmware **0.3.0-commands**, C toujours associée et le CC1101 IDLE. Une lecture
SSH sur l’hôte confirme `power/control=on` pour la clé : la veille automatique
qui interrompait les essais Ubuntu n’est pas active ici.

Le parcours officiel de sous-entrée choisit le slot existant sans `provision`,
`pair` ou `confirm`. Il émet montée puis STOP ; l’utilisateur confirme le
mouvement et l’arrêt. Après cette observation, la sous-entrée « Volet C » est
créée. Les services de `cover.volet_c` émettent ensuite descente puis STOP ;
l’utilisateur confirme de nouveau le mouvement et l’arrêt. Les réponses HTTP
et la dernière intention STOP constituent des preuves logicielles ; les
réponses de l’utilisateur constituent les preuves du comportement moteur.

Le tableau de bord « Volets » est créé via l’API officielle. L’utilisateur
confirme que ses trois commandes sont visibles, puis que son pilotage fonctionne.
Il monte ensuite le volet jusqu’à sa fin de course et confirme l’arrêt autonome
du moteur ; la dernière intention de C est alors montée. Cet arrêt ne produit
aucun retour de position dans HA. Une automatisation temporaire,
sans déclencheur automatique, appelle `cover.stop_cover` sur cette entité ; sa
trace se termine sans erreur, puis elle est supprimée. Cette vérification
supplémentaire de STOP ne mesure pas un arrêt en mouvement. L’état HA demeure
inconnu : aucun retour de position n’est inventé.

Après un redémarrage HA avec cette sous-entrée, les diagnostics confirment
l’intégration chargée, une sous-entrée, la connexion USB, C associée et le
CC1101 IDLE. La dernière intention montée de l’essai utilisateur est conservée.
L’utilisateur confirme qu’aucun mouvement ne survient pendant ce redémarrage.
La restauration d’une ancienne sauvegarde, les coupures matérielles et
plusieurs moteurs restent à qualifier.
La préparation HACS est locale ; publication, installation et mise à jour via
HACS restent à vérifier. Les preuves personnelles restent dans `build/`.

### Deuxième association via MySensors — 4 octobre 2026

Le firmware privé `0.5.0-trial` branché présente le premier volet associé.
Un appui sur l'association sélectionne l'emplacement 2 et rapporte
`2:pair_unqualified` : ce binaire n'autorise que l'emplacement 1. Le contrôleur
refuse l'opération avant de créer une identité ou de réserver des compteurs.

Un candidat privé conserve la même base et la disposition OTA de `0.5.0`,
mais autorise uniquement l'emplacement 2 pour un essai initial 0/1. La lecture
physique vérifie que le programme installé est identique au candidat validé et
que les 64 Kio du journal sont identiques octet par octet avant et après la
mise à jour, avant toute nouvelle association. La passerelle redémarre,
répond au heartbeat, présente encore le premier volet et laisse les deux
transports d'association OFF. Aucune commande RF n'est envoyée par ces contrôles.

L'utilisateur confirme ensuite que l'association a fonctionné. Sa capture HA
montre deux entités volet et le diagnostic `2:pos_unknown`. Cela établit une
deuxième association confirmée dans HA sur ce montage ; la capture ne démontre
pas un cycle montée/descente/STOP du deuxième moteur. Les identités, suffixes,
compteurs privés et binaires d'essai restent dans `build/`.

Ce résultat porte sur le candidat privé `0.5.0-trial` avec la sélection de slot
modifiée, pas sur un flash du firmware standard de cette révision. La sélection
publique `HA_X2D_TRIAL_SLOT` permet de reproduire la même autorisation bornée
sans modifier le code du sketch ; elle ne qualifie pas les autres moteurs ou
l'ensemble des 16 emplacements.
