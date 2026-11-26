"""
L'ÉCRITURE de data/config.toml par la page (Configuration → Proxy, Prompts,
Extensions) : le fichier est MIS À JOUR valeur par valeur, jamais réécrit
d'après ce que la page en connaît — commentaires, ordre, mise en forme,
«${VAR}» et secrets de ce qui ne change pas restent tels quels (tomlkit).

  read()                       le texte du fichier et son empreinte
  mask(valeur)                 ce que la page montre d'un secret : un
                               condensé qui le désigne sans le révéler
  diff(brut, proposé)          ce qui change, en (sets, deletes) : un
                               masque redevient la valeur qu'il désigne,
                               un défaut affiché n'est pas écrit, un masque
                               restant est refusé
  update(sets, deletes, …)     applique, sous LOCK : refuse si le fichier
                               a changé depuis `base` (Stale), valide le
                               résultat (`check`, relu comme écrit), garde
                               l'ancien dans config-history/ (les KEEP
                               derniers), écrit atomiquement, puis `then`
                               (la configuration en mémoire)
  restore(texte, attendu)      remet le texte d'avant (un enregistrement
                               que le rechargement refuse), si le fichier
                               est encore celui qu'on a écrit
  migrate()                    une fois, au démarrage : les anciens
                               prompts.json et extensions.json reportés
                               dans le fichier ([prompts],
                               [extensions.<nom>].active), puis renommés

Le rechargement (core/reload.py) est à l'appelant et prend LOCK : la page
recharge après un enregistrement ; un prompt ou une extension met à jour
sa valeur en mémoire dans `then`.
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time
import tomllib

import tomlkit

from . import atomic, config
from .settings import log

HISTORY = "config-history"
KEEP = 20
# Ce que la page montre à la place d'un secret écrit en clair
# (ui/config_view.py) : un condensé de SA valeur (HMAC, à une clé tirée au
# démarrage : il ne révèle rien, et ne se compare pas d'un démarrage à
# l'autre). Il désigne la valeur à garder ; il n'est jamais écrit.
MASK_FORM = "••• masqué {} •••"
MASK_RE = re.compile(r"••• masqué [0-9a-f]{10} •••")
_MASK_KEY = os.urandom(16)
# Écrire le fichier (et ce qui suit en mémoire) : une écriture à la fois,
# et pas pendant un rechargement (core/reload.py le prend aussi).
LOCK = threading.Lock()


class Stale(Exception):
    """Le fichier a changé depuis que la page l'a lu."""


class Absent(Exception):
    """Pas de config.toml (mode installation, fichier retiré)."""


class Invalid(ValueError):
    """Le résultat ne serait pas lu tel quel, ou un masque ne désigne
    aucune valeur du fichier."""


def mask(value: str) -> str:
    return MASK_FORM.format(hmac.new(_MASK_KEY, value.encode("utf-8"), "sha256").hexdigest()[:10])


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read() -> tuple[str, str]:
    """(texte, empreinte) du fichier. Absent : pas de fichier."""
    try:
        with open(config.CONFIG_PATH, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise Absent(f"{config.CONFIG_PATH} n'existe pas : rien à mettre à jour") from None
    return text, fingerprint(text)


def _default(path: tuple[str, ...]):
    value = config.READ.get(".".join(path))
    return list(value) if isinstance(value, tuple) else value


def _strings(node, out: dict) -> dict:
    """masque → valeur, pour chaque texte de l'arbre."""
    if isinstance(node, str):
        out[mask(node)] = node
    elif isinstance(node, dict):
        for v in node.values():
            _strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _strings(v, out)
    return out


def _unmask(node, known: dict):
    """`node` avec chaque masque remplacé par la valeur qu'il désigne.
    Invalid : un masque qui ne désigne aucune valeur du fichier."""
    if isinstance(node, str) and MASK_RE.fullmatch(node):
        if node not in known:
            raise Invalid(f"« {node} » ne désigne aucune valeur du fichier : relire la "
                          f"configuration (le proxy a redémarré ?), ou écrire la valeur")
        return known[node]
    if isinstance(node, dict):
        return {k: _unmask(v, known) for k, v in node.items()}
    if isinstance(node, list):
        return [_unmask(v, known) for v in node]
    return node


def _changes(raw: dict, new: dict, path: tuple[str, ...]) -> tuple[dict, list]:
    sets: dict[tuple, object] = {}
    deletes: list[tuple] = [path + (k,) for k in raw if k not in new]
    for k, v in new.items():
        p = path + (k,)
        old = raw.get(k)
        if isinstance(v, dict) and (old is None or isinstance(old, dict)):
            s, d = _changes(old or {}, v, p)
            sets.update(s)
            deletes += d
        elif k in raw:
            if v != old:
                sets[p] = v
        elif v != _default(p):
            sets[p] = v
    return sets, deletes


def _masked(node) -> bool:
    if isinstance(node, str):
        return bool(MASK_RE.search(node)) or "••• masqué" in node
    if isinstance(node, dict):
        return any(_masked(v) for v in node.values())
    if isinstance(node, list):
        return any(_masked(v) for v in node)
    return node is None


def diff(raw: dict, new: dict) -> tuple[dict, list]:
    """Ce que `new` (l'arbre proposé par la page) change à `raw` (le
    fichier, «${VAR}» non substitués) : {chemin: valeur} à poser et
    [chemin] à retirer. Un masque est d'abord remplacé par la valeur qu'il
    désigne, où qu'elle soit dans le fichier (une valeur retirée, déplacée,
    un backend renommé gardent le bon secret) ; un défaut affiché n'est
    pas ajouté. Une table est suivie clé par clé ; une valeur (tableau,
    table en ligne) est posée entière. Invalid : un masque reste."""
    sets, deletes = _changes(raw, _unmask(new, _strings(raw, {})), ())
    if bad := [".".join(p) for p, v in sets.items() if _masked(v)]:
        raise Invalid(f"valeur masquée ou vide, jamais écrite : {', '.join(bad)}")
    return sets, deletes


def _item(value):
    if isinstance(value, str) and "\r" in value:
        return tomlkit.string(value)                 # échappée : \r\n gardés
    if isinstance(value, str) and "\n" in value:
        return tomlkit.string(value, multiline=True)
    return tomlkit.item(value)


def _set(doc, path: tuple, value) -> None:
    node = doc
    for part in path[:-1]:
        if part not in node:
            node[part] = tomlkit.table()
        elif not isinstance(node[part], dict):
            raise Invalid(f"{'.'.join(path)} : {part} n'est pas une table, rien n'est écrasé")
        node = node[part]
    node[path[-1]] = _item(value)


def _delete(doc, path: tuple) -> None:
    trail, node = [], doc
    for part in path[:-1]:
        if part not in node or not isinstance(node[part], dict):
            return
        trail.append((node, part))
        node = node[part]
    if path[-1] in node:
        del node[path[-1]]
    # Une table vidée par la suppression part avec elle.
    for parent, part in reversed(trail):
        if len(parent[part]) == 0:
            del parent[part]
        else:
            break


def _lookup(tree: dict, path: tuple):
    for part in path:
        if not isinstance(tree, dict) or part not in tree:
            return None
        tree = tree[part]
    return tree


def _keep(text: str) -> None:
    """L'ancien fichier dans config-history/, les KEEP derniers : nommé
    à la seconde, puis -002, -003… dans la même seconde (l'ordre des noms
    est celui des écritures)."""
    folder = os.path.join(os.path.dirname(os.path.abspath(config.CONFIG_PATH)), HISTORY)
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    prefix = f"config.toml.{stamp}"
    # Après le plus grand suffixe pris dans cette seconde (pas le premier
    # libre : un nom élagué ne doit pas revenir avant les plus récents).
    taken = [f[len(prefix):] for f in os.listdir(folder) if f.startswith(prefix)]
    if not taken:
        name = prefix
    else:
        n = max(int(t[1:]) if t[1:].isdigit() else 1 for t in taken) + 1
        name = f"{prefix}-{n:03d}"
    atomic.write_text(os.path.join(folder, name), text)
    olds = sorted(f for f in os.listdir(folder) if f.startswith("config.toml."))
    for old in olds[:-KEEP]:
        os.unlink(os.path.join(folder, old))


def update(sets: dict, deletes=(), base: str | None = None, check=None,
           then=None) -> tuple[str, str]:
    """Applique `sets` ({chemin: valeur}) et `deletes` ([chemin]) au
    fichier, sous LOCK ; rend (le texte remplacé, la nouvelle empreinte).
    `check(texte)` : la validation du résultat avant écriture (elle lève
    pour refuser) ; `then()` : ce qui suit l'écriture, encore sous LOCK
    (la configuration en mémoire). Stale : le fichier a changé depuis
    `base` ; Absent : pas de fichier ; Invalid : le résultat ne se
    lirait pas, ou pas avec les valeurs posées. Rien d'écrit si rien ne
    change."""
    with LOCK:
        text, current = read()
        if base is not None and base != current:
            raise Stale("le fichier a été modifié entre-temps : la page relit la configuration")
        try:
            doc = tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError as exc:
            raise Invalid(f"{config.CONFIG_PATH} illisible : {exc}") from None
        for path in deletes:
            _delete(doc, tuple(path))
        for path, value in sets.items():
            _set(doc, tuple(path), value)
        new = tomlkit.dumps(doc)
        if new == text:
            return text, current
        try:
            written = tomllib.loads(new)
        except tomllib.TOMLDecodeError as exc:
            raise Invalid(f"le fichier ne se lirait plus : {exc}") from None
        if wrong := [".".join(p) for p, v in sets.items() if _lookup(written, tuple(p)) != v]:
            raise Invalid(f"relu autrement qu'écrit : {', '.join(wrong)}")
        if check is not None:
            check(new)
        _keep(text)
        atomic.write_text(config.CONFIG_PATH, new)
        if then is not None:
            then()
        return text, fingerprint(new)


def restore(text: str, expected: str) -> None:
    """Remet `text` (une écriture que le rechargement a refusée) — si le
    fichier est encore celui qu'on a écrit (`expected`, son empreinte) ;
    sinon Stale, rien n'est écrasé."""
    with LOCK:
        _, current = read()
        if current != expected:
            raise Stale("le fichier a changé depuis l'écriture refusée : laissé tel quel")
        atomic.write_text(config.CONFIG_PATH, text)


# ── la migration des anciens fichiers JSON ────────────────────────────

def _json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("migration : %s illisible (%s) — laissé en place", path, exc)
        return None
    return doc if isinstance(doc, dict) else None


def migrate() -> int:
    """Une fois : prompts.json (clé → texte) et extensions.json (nom →
    true ou liste d'outils), à côté du fichier, reportés dans [prompts] et
    [extensions.<nom>].active, puis renommés *.migre-AAAAMMJJ. Rend le
    nombre de valeurs reportées. Idempotente : sans ces fichiers, rien."""
    folder = os.path.dirname(os.path.abspath(config.CONFIG_PATH))
    sets, done = {}, []
    for name in ("prompts.json", "extensions.json"):
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            continue
        doc = _json(path)
        if doc is None:
            continue
        for key, value in doc.items():
            if name == "prompts.json" and isinstance(value, str):
                sets[("prompts", str(key))] = value
            elif name == "extensions.json" and (value is True or (
                    isinstance(value, list) and value and all(isinstance(n, str) for n in value))):
                sets[("extensions", str(key), "active")] = value
        done.append(path)
    if not done or config.SETUP:
        return 0
    if sets:
        def then():
            # Lus après le démarrage (prompts, extensions.load qui suit) :
            # la configuration en mémoire les reçoit.
            fresh = config.load(config.CONFIG_PATH)
            for table in ("prompts", "extensions"):
                if table in fresh:
                    config.CONFIG[table] = fresh[table]
        update(sets, then=then)
    stamp = time.strftime("%Y%m%d")
    for path in done:
        os.replace(path, f"{path}.migre-{stamp}")
    log.info("migration : %d valeur(s) de %s reportée(s) dans %s", len(sets),
             ", ".join(os.path.basename(p) for p in done), config.CONFIG_PATH)
    return len(sets)
