"""La configuration de test : l'exemple du dépôt, jamais data/config.toml
(qui porte les réglages d'un déploiement réel et n'est pas versionné).
Posé AVANT tout import du paquet — config.py lit CONFIG_PATH à l'import."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault(
    "CONFIG_PATH", os.path.join(ROOT, "data", "config.example.toml"))
