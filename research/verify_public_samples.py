"""Vérifications hors ligne de données publiques, sans USB ni émission RF.

Le format X3D testé est une hypothèse pour l'export HACF, pas une
identification des télécommandes A/B. Les six trames sont déjà démodulées
et déblanchies ; ceci n'est pas un décodeur de captures brutes.

SPDX-License-Identifier: Apache-2.0
Transformation du compteur adaptée de x3d_enc_msg_id/x3d_dec_msg_id :
Copyright (c) 2023 Sven Fabricius, mr-sven/x3d-rfm-esp32, x3d-lib/x3d.c
https://github.com/mr-sven/x3d-rfm-esp32/blob/
92d9927462b93a2574986aa9e5c5b63dc5997ef4/x3d-lib/x3d.c
Modifications : traduction Python, transformation sans incrémentation,
vérifications de corpus. Licence jointe dans LICENSE-APACHE.

Export HACF, message 9 de quenbo (27 septembre 2025) :
https://forum.hacf.fr/t/rf-player-nemet-plus-apres-quelques-trames-sature/64980/9
Trames tirées des données raw_x3d.bin, diorcety/X2D :
https://github.com/diorcety/X2D/blob/
4d724089425481c50c14115fbbded076c1a848e2/raw_x3d.bin
Les identités de ce corpus tiers ne doivent pas être utilisées pour émettre.
"""

from binascii import crc_hqx

SBOX = (1, 0, 12, 8, 10, 9, 14, 7, 3, 5, 4, 11, 2, 15, 6, 13)


def transform(value: int, device_id: int, *, inverse: bool = False) -> int:
    """Transforme un mot 16 bits ; n'alloue et n'incrémente aucun compteur."""
    if not 0 <= value <= 0xFFFF or not 0 <= device_id <= 0xFFFFFF:
        raise ValueError("Mot 16 bits et identité 24 bits requis")
    key = (device_id & 0xFF00) | ((device_id & 0xFF) ^ (device_id >> 16))
    for turn in (range(31, -1, -1) if inverse else range(32)):
        shift = turn % 13
        if inverse:
            value ^= key
        value = (value & ~(0xF << shift)) | (SBOX[(value >> shift) & 0xF] << shift)
        if not inverse:
            value ^= key
    return value


def verify() -> None:
    if not __debug__:
        raise SystemExit("Exécuter sans -O : les vérifications utilisent assert.")
    device_id = 0xF73192
    exports = ((0xE2DC, 0xC4FB), (0xC07A, 0x48FC),
               (0xE212, 0x8EFC), (0x1071, 0x01FD))
    for counter, (rolling, exported_checksum) in enumerate(exports, start=6641):
        assert transform(counter, device_id) == rolling
        assert transform(rolling, device_id, inverse=True) == counter
        # En-tête candidat X3D : ID LE24, réseau 01, payload, rolling LE16.
        header = (device_id.to_bytes(3, "little") + bytes.fromhex("01 05 98 22 04 00")
                  + rolling.to_bytes(2, "little"))
        checksum = int.from_bytes(exported_checksum.to_bytes(2, "little"), "big")
        assert (-sum(header)) & 0xFFFF == checksum

    # Une trame distincte par groupe de cinq copies du corpus tiers.
    # L'ordre des commandes provient du rapprochement avec X3DWellcomRemote.
    frames = (
        (140, "14ff8b000e5460180105982282006a11fd77f812"),
        (141, "14ff8c000e546018010598228400e80bfcfdaa6d"),
        (142, "14ff8d000e5460180105982281004219fd98e1a4"),
        (3032, "14ffd7000e916018010598228200a219fcfa850b"),
        (3033, "14ffd8000e916018010598228400b0ebfc18d761"),
        (3034, "14ffd9000e9160180105982281001e19fd7fd62a"),
    )
    for counter, encoded in frames:
        frame = bytes.fromhex(encoded)
        assert frame[0] == len(frame) == 20
        assert frame[4] & 0x1F == 14
        assert crc_hqx(frame[:-2], 0) == int.from_bytes(frame[-2:], "big")
        assert (-sum(frame[5:-4])) & 0xFFFF == int.from_bytes(frame[-4:-2], "big")
        identity = int.from_bytes(frame[5:8], "little")
        rolling = int.from_bytes(frame[14:16], "little")
        assert transform(rolling, identity, inverse=True) == counter
        assert transform(counter, identity) == rolling
        assert frame[2] == (counter - 1) & 0xFF

    for value in range(65536):
        assert transform(transform(value, device_id), device_id, inverse=True) == value
    print("OK : 4 exports HACF, 6 trames X3D décodées, 65 536 aller-retour.")
    print("Ne valide ni la fréquence, ni A/B, ni l'association, ni une émission RF.")


if __name__ == "__main__":
    verify()
