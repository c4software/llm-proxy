"""
Chargement de la configuration : UN SEUL fichier TOML (data/config.toml),
source unique de vérité. L'environnement ne porte que deux choses :

  * CONFIG_PATH : où trouver le TOML (défaut « data/config.toml ») ;
  * les SECRETS : toute valeur chaîne du TOML peut contenir «${VAR}»,
    remplacé par la variable d'environnement correspondante. Les clés
    d'API restent ainsi hors du fichier (donc hors du dépôt) tandis que
    la structure, elle, est versionnable.

Au premier démarrage, si data/config.toml n'existe pas, il est créé à
partir de data/config.example.toml : le conteneur démarre sur un volume
vide sans intervention.

Ce module ne connaît ni FastAPI, ni httpx, ni les autres modules du
proxy : il est importé par tous, il n'importe personne.
"""

import os
import re
import shutil
import sys
import tomllib

# Le paquet vit un cran sous la racine du projet.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("CONFIG_PATH") or os.path.join(
    ROOT, "data", "config.toml")
EXAMPLE_PATH = os.path.join(ROOT, "data", "config.example.toml")

# «${VAR}» dans n'importe quelle chaîne du TOML.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value):
    """Substitue ${VAR} récursivement dans tout l'arbre chargé."""
    if isinstance(value, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _bootstrap(path: str) -> None:
    """Config absente : on installe l'exemple documenté à sa place."""
    if not os.path.exists(EXAMPLE_PATH):
        raise SystemExit(
            f"configuration introuvable : {path} (et aucun modèle "
            f"{EXAMPLE_PATH} pour l'initialiser)"
        )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    shutil.copyfile(EXAMPLE_PATH, path)
    print(f"configuration absente : {path} créé depuis "
          f"{os.path.basename(EXAMPLE_PATH)}", file=sys.stderr)


def load(path: str = CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        _bootstrap(path)
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{path} : TOML invalide — {exc}")
    except OSError as exc:
        raise SystemExit(f"{path} : illisible — {exc}")
    return _expand(raw)


CONFIG: dict = load()


def section(name: str) -> dict:
    """Une table de premier niveau, toujours un dict (vide si absente)."""
    value = CONFIG.get(name)
    return value if isinstance(value, dict) else {}


def get(path: str, default=None):
    """Lecture pointée : get(\"stats.database\"). `default` si absent."""
    node = CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def _typed(path, default, cast, label):
    value = get(path, default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{CONFIG_PATH} : {path} doit être {label} "
                         f"(reçu {value!r})")


def num(path: str, default: float) -> float:
    return _typed(path, default, float, "un nombre")


def integer(path: str, default: int) -> int:
    return _typed(path, default, int, "un entier")


def text(path: str, default: str = "") -> str:
    value = get(path, default)
    return str(value) if value is not None else default


def flag(path: str, default: bool = False) -> bool:
    return bool(get(path, default))


def strings(path: str, default=()) -> list[str]:
    """Liste de chaînes ; une chaîne unique séparée par des virgules est
    acceptée."""
    value = get(path)
    if value is None:
        return [str(v) for v in default]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    raise SystemExit(f"{CONFIG_PATH} : {path} doit être une liste de chaînes")


def resolve(path: str) -> str:
    """Chemin lu dans le TOML. Relatif = relatif au DOSSIER DU FICHIER DE
    CONFIGURATION, pas au dossier courant ni à la racine du projet : ce
    que la config désigne vit à côté d'elle, y compris quand CONFIG_PATH
    la range ailleurs."""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), path)
