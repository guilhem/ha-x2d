# Client Python

Bibliothèque USB v2 indépendante de Home Assistant, Python ≥ 3.11 et `serialx`.
Un hello avec seulement `status` et `shutters` est accepté ; le firmware peut
bloquer le provisionnement tant que l’identité et le compteur RF initiaux ne
sont pas validés. Une seule tâche lit la liaison JSONL (4096 octets maximum). Les réponses sont
routées par ID et les événements par session et séquence ; plusieurs requêtes
peuvent attendre simultanément. STOP peut être envoyé pendant l’attente TX d’OPEN.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ./python
.venv/bin/python -m x2d_gateway /dev/serial/by-id/LE_PORT_DE_LA_CLE
```

La CLI lit seulement hello, status et shutters. Décharger l’intégration HA avant
son utilisation : un seul processus possède le port.

```python
from x2d_gateway import Gateway

gateway = await Gateway.open("/dev/serial/by-id/LE_PORT_DE_LA_CLE")
try:
    print(await gateway.status())
    print(await gateway.shutters())
    record = await gateway.provision(1)  # explicite, persistant, idempotent, sans RF
    # Après préparation physique documentée, avec un profil RF vérifié :
    # await gateway.pair(1)       # attend ACK puis tx_result ; reste pending
    # await gateway.confirm(1)    # seulement après confirmation humaine du moteur
    # await gateway.command(1, "stop")  # open / stop / close ; attend tx_result
finally:
    await gateway.close()
```

Un refus applicatif valide lève `GatewayError` avec son attribut `code`, sans
corrompre la session. `ProtocolError` signale des données incompatibles ou
invalides et ferme la liaison. Timeout, annulation et déconnexion ferment aussi
la session ; aucune écriture n’est automatiquement réessayée. Un résultat RF
inconnu ou une commande perdue sur la liaison lève `CommandUncertain`, y compris
si les données USB deviennent invalides après soumission. La cause protocolaire
reste accessible via `__cause__` ; aucun résultat d'émission n'est supposé.
`emitted` ne signifie ni réception moteur ni position connue. Les callbacks
synchrones inscrits via `add_event_callback()` reçoivent uniquement des événements
validés ; ils doivent rester courts. Leur retour d’inscription permet le retrait.

Après une erreur, rouvrir explicitement avec `expected_device_id=identite`.
Réutiliser le même slot pending après interruption préserve l’identité persistée
par le firmware. Aucun compteur radio n’est géré par Python.

Les tests Linux utilisent un vrai transport serialx sur PTY. Ils ne prouvent
ni la RF, ni l’association moteur, ni la récupération du journal sur flash.
