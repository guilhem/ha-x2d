# Protocole USB — v1, diagnostic 0.1.0

Ce contrat est implémenté par les trois composants. Il ne fixe pas encore le
contrat de capture ou d'émission radio proposé dans l'étude.

USB CDC, 115200 demandé par l'hôte (débit de ligne virtuel). Le RP2040 porte
les descripteurs `ha-x2d` / `HA-X2D Gateway`, VID/PID du profil Arduino-Pico
YD-RP2040 `2E8A:800A`, et l'identifiant unique de sa flash en numéro série.

Un objet JSON UTF-8 par ligne, LF obligatoire ; CRLF accepté. **512 octets
maximum, LF compris.** Les requêtes comportent exactement trois champs.
`v` vaut l'entier `1`, `id` est un entier de 1 à 2147483647, `op` une chaîne.
Un booléen n'est pas un entier. Aucun événement spontané dans cette version.
Une seule requête en attente ; délai hôte de trois secondes par échange.

```json
{"v":1,"id":1,"op":"hello"}
{"v":1,"id":1,"ok":true,"result":{"product":"ha-x2d","firmware":"0.1.0","device_id":"0123456789ABCDEF","session":"FEDCBA9876543210","max_line_bytes":512,"capabilities":["info","status","cc1101_probe"]}}
{"v":1,"id":2,"op":"status"}
{"v":1,"id":2,"ok":true,"result":{"uptime_ms":100,"radio":{"detected":true,"partnum":0,"version":20,"marcstate":1},"tx_enabled":false}}
```

Les identifiants ci-dessus sont fictifs. `device_id` est stable pour la flash
physique ; remplacer celle-ci change l'identité. `session` change au démarrage.
Les deux sont des chaînes hexadécimales majuscules de 16 caractères.
`uptime_ms` est un compteur 32 bits qui reboucle après environ 49,7 jours.

`hello` n'accède pas à la radio. `status` lit PARTNUM, VERSION et MARCSTATE :
`detected` demande les valeurs CC1101 acceptées et l'état IDLE. En cas d'absence,
d'erreur SPI ou de valeurs non reconnues, les trois registres sont `null`.
La détection ne prouve ni la bande du module, ni son antenne, ni sa compatibilité
avec un volet. Le firmware initialise le CC1101 au repos ; `tx_enabled` est
toujours `false` et aucune commande d'émission n'existe.

Erreurs : `invalid_request`, `unsupported_operation`, `line_too_long`.

```json
{"v":1,"id":3,"ok":false,"error":"unsupported_operation"}
{"v":1,"id":null,"ok":false,"error":"line_too_long"}
```

L'identifiant d'une demande incorrecte peut être `null` si sa structure ne
permet pas de le reconnaître. Après dépassement, le firmware ignore les octets
jusqu'au prochain LF, renvoie une erreur puis accepte une nouvelle ligne.
Une demande tronquée sans LF ne s'exécute pas. Le client ferme la connexion
après timeout, annulation, erreur, réponse invalide ou identifiant incorrect.
Il n'envoie aucune reprise automatique ; HA réouvre une session au prochain
contrôle périodique et vérifie à nouveau l'identifiant attendu.

Sources d'API : [serialx](https://puddly.github.io/serialx/how-to/async-serial.html),
[migration recommandée par HA](https://developers.home-assistant.io/blog/2026/04/27/pyserial-to-serialx/),
[USB Arduino-Pico](https://arduino-pico.readthedocs.io/en/latest/usb.html).
