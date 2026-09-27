# Client Python

Bibliothèque asynchrone indépendante de Home Assistant, utilisant `serialx`.
Elle gère le port, l'identification, les diagnostics, la validation des messages
et la fermeture sur erreur. Elle ne décode aucun protocole radio à ce stade.

Depuis la racine du projet :

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ./python
.venv/bin/python -m x2d_gateway /dev/serial/by-id/LE_PORT_DE_LA_CLE
```

L'intégration HA doit être déchargée pendant l'utilisation de la CLI : un seul
processus possède le port. La CLI affiche l'identité et les registres du CC1101.

```python
from x2d_gateway import Gateway

gateway = await Gateway.open("/dev/serial/by-id/LE_PORT_DE_LA_CLE")
try:
    print(gateway.info)
    print(await gateway.status())
finally:
    await gateway.close()
```

Après une erreur, ouvrir une nouvelle session avec
`expected_device_id=identite_precedente`. Le client ne réessaie jamais une
requête automatiquement. Une session a une seule requête en cours, même si
plusieurs coroutines appellent `status()` simultanément.

Python ≥ 3.11 ; validations exécutées avec Python 3.14 et serialx 1.10.0 sur Linux.
Les autres systèmes et les transports série distants restent à valider.
