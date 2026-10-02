# X2D dans Home Assistant

Intégration personnalisée **0.3.1** pour Home Assistant Core **2026.9.4** et
versions compatibles. La passerelle apparaît comme **X2D Gateway**, avec une
connexion série locale ou une URL prise en charge par `serialx`.
Chaque volet est un appareil connecté distinct, avec les commandes montée,
STOP et descente. La fiche de la passerelle présente les volets dans la liste
native **Connected devices** ; la page des intégrations les regroupe par
sous-entrée. Les diagnostics reprennent l’identification, le firmware et les
capacités annoncés par `hello`, sans supposer un modèle de carte.

**Le firmware 0.3.0-commands commande les identités déjà associées dans la clé.
L'association d'une nouvelle télécommande est reportée.** Un moteur a été
vérifié physiquement ; plusieurs volets sont couverts uniquement en simulation.
La position, le mouvement et l'état fermé restent inconnus sans retour moteur.

## Installer avec HACS

HACS exige un dépôt GitHub public. Une release publiée contenant l'asset
**x2d.zip** est nécessaire. Un tag
ou le téléchargement de la branche source ne suffit pas : le client Python est
intégré au ZIP lors de sa construction. La branche source est masquée dans HACS.

1. Dans HACS, ouvrir **⋮ → Dépôts personnalisés**.
2. Ajouter `https://github.com/guilhem/ha-x2d`, catégorie **Intégration**.
3. Télécharger **X2D Gateway**, version **0.3.1**, puis redémarrer HA.
4. Dans **Paramètres → Appareils et services**, accepter la découverte USB ou
   ajouter **X2D Gateway**. Pour une installation manuelle existante,
   conserver son entrée X2D et ses volets ; ne pas les supprimer ou les recréer.

L'icône est livrée dans le composant via le mécanisme de
[marque locale de Home Assistant](https://developers.home-assistant.io/docs/core/integration/brand_images/).
Ce dépôt est installé comme
[dépôt personnalisé HACS](https://www.hacs.dev/docs/faq/custom_repositories/).
Son inscription au catalogue par défaut est une démarche distincte.

HA OS doit exposer la clé à Core. Préférer un port `/dev/serial/by-id/…` ;
pour une VM ou un conteneur, transmettre explicitement l'USB. **Reconfigurer**
permet de changer la connexion de la même passerelle. Son identité est vérifiée ;
les références des volets et les noms personnalisés sont conservés.
La clé doit rester éveillée pendant les échanges : `power/control=on` a été
vérifié sur HA OS 18.3. Sur un autre hôte Linux, utiliser la
[règle de veille USB ciblée](../firmware/99-ha-x2d-power.rules) et vérifier ce
réglage ; prolonger les délais ne corrige pas une suspension USB.

Pour une passerelle réseau, saisir par exemple `socket://192.168.1.20:6638`
dans le champ **Port série ou URL**, lors de l’ajout manuel ou de la
reconfiguration. `tcp://` est également pris en charge par `serialx`. L’URL
est conservée intacte ; la résolution `/dev/serial/by-id` concerne uniquement
les chemins locaux. Le point de connexion doit fournir le même protocole
JSONL v2. Le firmware USB actuel ne fournit pas de serveur réseau.

## Récupérer un volet déjà associé

Dans l'entrée de la passerelle, ajouter une sous-entrée **Volet** et choisir
son nom. Si une seule identité associée est disponible, elle est sélectionnée
automatiquement. Sinon, choisir un volet dans la liste des identités enregistrées.
Le parcours propose ensuite une commande de test explicite et demande de
confirmer le résultat observé avant de créer l'entité.

Cette récupération n'envoie ni `provision`, ni `pair`, ni `confirm`. Elle
conserve l'identité, la génération et les compteurs de la clé. Si tous ses
volets associés sont déjà présents dans HA, le bouton d'ajout explique que le
firmware ne permet pas de créer une nouvelle association. Cela ne signifie pas
que les 16 emplacements de la clé sont occupés.

L'ouverture du mode association par une télécommande physique concerne un
futur firmware d'association qualifié. Elle n'est pas nécessaire pour récupérer
C. Les essais supervisés et leurs limites sont documentés dans les
[observations radio](../docs/OBSERVATIONS_RADIO.md).

## Mettre à jour ou revenir à une version précédente

Avant une mise à jour, conserver une sauvegarde de la configuration HA et la
version actuellement installée. Installer la release souhaitée depuis HACS,
puis redémarrer HA. Une première migration depuis l'archive manuelle suit la
même procédure : HACS reprend la gestion des fichiers du composant.

L'entrée X2D, ses sous-entrées, les noms personnalisés et les identifiants des
entités sont conservés, notamment `cover.volet_c`. Le tableau de bord et les
automatisations continuent donc de viser la même entité. Le firmware n'est pas
mis à jour par HACS et les compteurs restent exclusivement dans la clé.

Pour revenir en arrière, sélectionner la release précédente dans HACS,
la télécharger et redémarrer. L'archive manuelle de cette version constitue
également un recours si HACS est indisponible. Ne pas supprimer l'intégration
ni réinitialiser la clé. Restaurer une ancienne configuration HA ne remet pas
les compteurs radio à zéro ; une génération de journal différente rend les
anciennes références indisponibles au lieu de les réutiliser.

## Construire les archives

Depuis la racine du dépôt, dans l'environnement `devenv` :

```sh
devenv test
devenv tasks run ha:package
python tools/build_component.py --hacs
```

`dist/x2d.zip` contient directement les fichiers du composant, ses traductions,
sa marque locale et `_client/`. C'est l'asset à joindre à la release HACS.
`dist/x2d-0.3.1.zip` conserve le préfixe `custom_components/x2d/` pour une
installation manuelle dans le répertoire de configuration HA. Les deux formats
ont le même contenu, avec des métadonnées ZIP fixes et reproductibles.

La bibliothèque Python garde une source unique dans `python/src/x2d_gateway/` ;
le générateur la copie dans `_client/`. Les versions du firmware, du client et
de l'intégration sont indépendantes ; le contrat partagé reste JSONL v2.

## État et reprise

`last_command_intent` indique la dernière intention enregistrée par la clé,
pas la position ni la confirmation d'un mouvement. Une émission RF terminée
ne prouve pas que le moteur a reçu la commande. Les diagnostics exportés
masquent les identités, sessions, générations et compteurs privés.

La disponibilité exige une connexion validée, le bon journal, un volet associé,
une radio détectée et l'émission autorisée. Après perte de liaison, le coordinateur
réessaie la connexion au prochain rafraîchissement, sans rejouer de commande.
STOP n'attend pas un rafraîchissement déjà en cours. Une commande dont le
résultat est perdu après une rupture de liaison reste incertaine et exige
une vérification humaine avant toute nouvelle tentative explicite. Supprimer une entité
ou sous-entrée HA n'efface ni l'identité radio ni son compteur dans la clé.

Les essais historiques ont confirmé montée/STOP et descente/STOP de C depuis
HA OS 18.3 / Core 2026.9.4, le tableau de bord, une automatisation STOP et un
redémarrage sans mouvement. Les télécommandes A/B ont continué à fonctionner.
L'installation et la mise à jour réelles par HACS restent à vérifier après
publication ; ces preuves sont distinctes des tests logiciels et de la
qualification future de plusieurs moteurs.

Les tests logiciels utilisent le client `serialx` réel sur PTY et TCP local,
ainsi que les parcours de configuration, reconfiguration et reconnexion du
composant empaqueté dans HA Core. Ils ne qualifient ni une passerelle réseau
matérielle ni un futur firmware ESPHome.
