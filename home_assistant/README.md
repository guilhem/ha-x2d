# Adaptateur Home Assistant

Composant personnalisé `x2d`, initialement limité au diagnostic de la passerelle.
Il fournit un assistant de configuration en français/anglais, un appareil,
un diagnostic de connexion USB et un état du CC1101. Interrogation toutes les
30 secondes ; reconnexion au même identifiant physique après une erreur.
Les diagnostics exportés masquent l'identifiant et la session de démarrage.

## Préparer l'installation

```sh
python3 tools/build_component.py
```

L'archive `dist/x2d-0.1.0.zip` contient `custom_components/x2d/`. Décompresser
son contenu dans le répertoire de configuration Home Assistant, puis redémarrer
HA. Ajouter **X2D USB Gateway** dans **Paramètres → Appareils et services**,
ou accepter la découverte d'une clé équipée de ce firmware. Le flux vérifie
le produit et l'identité USB avant de créer l'entrée.

La bibliothèque n'étant pas publiée sur PyPI, le script copie son code dans
`_client/` à la fabrication de l'archive. Sa seule source reste
`python/src/x2d_gateway/` ; ne pas modifier la copie générée. Copier uniquement
le dossier source `home_assistant/custom_components/x2d/` ne suffit pas.

Le composant est développé pour Home Assistant **2026.9.4**. Il utilise le
sélecteur série natif et déclare son port à la couche USB de HA. Pour changer de
port, utiliser **Reconfigurer** ; la nouvelle liaison doit répondre avec la
même identité. Préférer `/dev/serial/by-id/…` ; HA doit avoir accès au port,
ce qui peut demander une transmission USB au conteneur ou à la VM.

Cette version ne crée pas d'entité volet et ne réalise ni réception RF, ni
association, ni commande moteur. Elle n'ajoute pas de route « X2D » au menu
Connectivité. Elle n'a pas été installée sur l'instance de l'utilisateur.
