# Adaptateur Home Assistant

Composant personnalisé `x2d` 0.3.0 pour Home Assistant Core **2026.9.4**.
Une entrée USB possède un coordinateur partagé et les diagnostics USB/CC1101.
Chaque volet est une sous-entrée native liée à son slot et à la génération du
journal, avec une entité `cover` OPEN, STOP et CLOSE.

```sh
python3 tools/build_component.py
```

Décompresser `dist/x2d-0.3.0.zip` dans le répertoire de configuration HA, puis
redémarrer. Ajouter **X2D USB Gateway** dans **Paramètres → Appareils et services**
ou accepter sa découverte USB. HA OS utilise directement le port USB accessible
à Core ; préférer `/dev/serial/by-id/…`. VM et conteneurs nécessitent une
transmission USB adaptée. Le flux vérifie le produit et l’identité physique.
**Reconfigurer** change le port de la même passerelle.

La bibliothèque Python n’étant pas publiée sur PyPI, le script la copie dans
`_client/` à la fabrication de l’archive. Sa source unique reste
`python/src/x2d_gateway/` ; copier le composant source seul ne suffit pas.

## Installation HACS après publication

L’adaptateur est dans `custom_components/x2d/` à la racine du dépôt, avec son
`manifest.json`, comme l’exige la [structure officielle HACS](https://www.hacs.dev/docs/publish/integration/).
Le guide reste dans `home_assistant/README.md` et la source du client dans
`python/src/x2d_gateway/`. Le [code de validation HACS](https://github.com/hacs/integration/blob/main/custom_components/hacs/repositories/integration.py)
cherche ce manifeste dans l’arborescence Git avant le téléchargement du ZIP.
**La publication GitHub et l’installation via HACS restent à faire.**

Le `hacs.json` racine prépare une distribution par release avec un asset de nom
fixe **`x2d.zip`**, HA minimum **2026.9.4** et la branche par défaut masquée.
Le client reste généré dans l’archive ; il n’est pas nécessaire de versionner
`_client/` pour ce téléchargement par release. La branche source seule reste
incomplète. Voir les [métadonnées HACS](https://www.hacs.dev/docs/publish/start/)
et le [téléchargement des archives](https://github.com/hacs/integration/blob/main/custom_components/hacs/repositories/base.py).

Depuis la racine du dépôt, avec l’environnement existant épinglé par `uv.lock` :

```sh
.devenv/state/venv/bin/python tools/build_component.py --hacs
.devenv/state/venv/bin/python -m unittest discover -s tests -p test_home_assistant.py -v
```

`dist/x2d.zip` contient directement `manifest.json`, les plateformes,
les traductions et `_client/`. HACS l’extrait dans `custom_components/x2d/`.
**Ne pas joindre `dist/x2d-0.3.0.zip` comme asset HACS** : cette archive manuelle
conserve le préfixe `custom_components/x2d/`. Les deux formats ont le même contenu
et des métadonnées ZIP fixes ; chaque format est reproductible octet pour octet
avec les mêmes sources et le même environnement Python/zlib.

Publication restant à faire par le mainteneur :

1. Publier les sources avec `custom_components/x2d/`, le `hacs.json` racine,
   le README et le manifeste enrichi.
2. Vérifier les versions du manifeste et du client, construire depuis les
   sources de la version retenue et lancer les checks ci-dessus.
3. Publier une **release GitHub non draft**, avec un tag correspondant à la
   version du manifeste (actuellement `0.3.0`) et l’asset exact **`x2d.zip`**.
   Un tag seul ne suffit pas ; chaque release proposée doit posséder cet asset.

Ensuite, dans HACS : menu **⋮ → Dépôts personnalisés**, ajouter
`https://github.com/guilhem/ha-x2d`, type
**Intégration**, puis télécharger **X2D USB Gateway** et redémarrer HA.
Ajouter ensuite l’intégration dans **Paramètres → Appareils et services**, ou
conserver l’entrée `x2d` déjà configurée. La procédure de dépôt personnalisé est
[documentée par HACS](https://www.hacs.dev/docs/faq/custom_repositories/).
L’ajout à son catalogue par défaut est une démarche distincte.
L’enregistrement du dépôt, le téléchargement et la mise à jour via HACS
restent à valider après publication ; aucun de ces gestes n’a été effectué ici.

## Configuration des volets

Ajouter un volet via la sous-entrée **Volet** : choisir son nom et un emplacement
(1–16). Les consignes français/anglais livrées concernent uniquement le moteur
**Franciasoft / Well’com observé**, avec sa télécommande d’origine reconnue comme
émetteur de **BASE** (un accusé mécanique, confirmé par l’utilisateur).
Elles suivent le Well’book [p. 87 — ajout d’un émetteur](https://pro.franciaflex.com/medias/FX-ex/book/wellbook/87-Ajouter-un-emetteur-complementaire.html)
et [p. 89 — identification BASE/complémentaire](https://pro.franciaflex.com/medias/FX-ex/book/wellbook/89-Reconnaitre-un-emetteur-de-base-complementaire.html),
sources S03/S04 du [dossier technique](../docs/DOSSIER_TECHNIQUE.md).

1. Placer la télécommande de BASE existante en mode normal **N**.
2. Maintenir **STOP** jusqu’à l’accusé mécanique du moteur, puis relâcher.
3. Dans la minute suivant cet accusé, valider le formulaire HA. La passerelle
   inscrit uniquement sa nouvelle identité **C**. Si la minute est écoulée,
   reprendre les étapes 1 et 2 avant de valider.
4. Confirmer dans HA l’accusé mécanique suivant du moteur, après l’inscription de C.

L’ouverture du mode association est manuelle ; HA n’a pas besoin de recevoir
le trafic de la télécommande existante. Garder le volet sous surveillance et
conserver les télécommandes existantes, sans réinitialiser le moteur.
L’association est alors persistée dans la passerelle.
Choisir ensuite une commande de test explicite (OPEN, STOP ou CLOSE),
puis confirmer le résultat observé pour créer la sous-entrée HA. On peut choisir
un autre test sans émission implicite. Un slot déjà paired reprend directement
ce test, sans rejouer l’association. Provisionner n’émet aucune RF. Une interruption
conserve le slot pending ; reprendre avec le même emplacement. L’émission seule
ne crée jamais une association confirmée.

Avec le firmware **0.3.0-commands**, seuls les slots déjà associés dans la clé
sont proposés. Choisir le slot de C et son nom conduit directement au test,
sans `provision`, `pair` ou `confirm`. La clé conserve son identité, sa génération
et ses compteurs. Ce firmware ne permet pas encore d’inscrire un autre volet.

Ces consignes physiques ne valident pas le profil radio du firmware.
Si le firmware annonce seulement `status` et `shutters`, le flux refuse de
provisionner et ne modifie aucun
stockage. Sans capacité `pair` ou avec `tx_enabled=false`, le flux refuse aussi
l’association avec `profile_unverified` et garde le volet pending.

Position et état fermé restent inconnus ; `last_command_intent` représente
seulement la dernière intention enregistrée par la passerelle. Disponibilité :
liaison active, génération correspondante, stockage ready, slot paired, radio
détectée et TX autorisé. Une génération différente invalide les anciennes
références. Supprimer une entité ou sous-entrée n’émet rien, ne libère pas son
contrôleur en flash et ne réinitialise aucun compteur.

Après débranchement, les entités deviennent indisponibles ; le coordinateur
réouvre la même identité lors du prochain rafraîchissement (30 secondes).
Une commande utilise directement la connexion et son état déjà validés ; un
polling en attente ne retarde pas l’envoi de STOP. Une connexion indisponible ou
en cours de revalidation refuse la commande, sans reconnexion sur ce chemin.
Le rafraîchissement forcé après l’émission lit le journal avant le retour du
service, même pour deux commandes rapides. Il ne rejoue aucune commande. Les diagnostics exportés masquent identités,
sessions, génération et compteurs radio.

Validation locale : composant empaqueté chargé dans HA Core réel, transport PTY,
deux sous-entrées, flux guidé avec test observé, reprise paired, services cover,
STOP concurrent pendant TX ou status en attente, reconnexion et
invalidation de génération. Sur l’instance HA OS 18.3 / Core 2026.9.4,
l’archive manuelle et le firmware **0.3.0-commands** ont ensuite été installés.
Le slot de C a été adopté sans nouvelle association : montée/STOP depuis le
parcours d’ajout, puis descente/STOP depuis l’entité `cover.volet_c`, confirmés
physiquement par l’utilisateur. Les trois commandes du tableau de bord sont
visibles et l’utilisateur confirme son pilotage, notamment une montée jusqu’à
l’arrêt moteur en fin de course. Une automatisation temporaire STOP a terminé sans erreur, puis a
été supprimée. Ces preuves ne valident pas encore l’installation/mise à jour
via HACS. Après un redémarrage HA avec la sous-entrée créée, les diagnostics
confirment l’intégration chargée, C associée, la connexion USB et la dernière
intention montée conservée ; la position reste inconnue.
L’utilisateur confirme qu’aucun mouvement ne survient pendant ce redémarrage.
A/B avaient été vérifiées après les essais radio précédents ; voir les
[observations radio](../docs/OBSERVATIONS_RADIO.md).
