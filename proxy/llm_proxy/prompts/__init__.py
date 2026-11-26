"""
Les PROMPTS : les textes que le modèle lit — descriptions des outils et de
leurs paramètres, consignes, instructions — hors du code. Les messages
d'erreur et les résultats des outils restent dans le code.

  *.toml     le CATALOGUE, les textes par défaut, en anglais, un fichier
             par domaine (tools, page, mcp) ; une table par sujet :
             [web_fetch] description = "…" donne la clé
             `web_fetch.description`
  prompts.json   les textes MODIFIÉS par l'admin (Configuration →
             Prompts ; storage/prompt_overrides.py), relus quand le
             fichier change : effet à la requête suivante

Un texte porte des VARIABLES `{nom}` que le code fournit (une borne, le
nom d'un outil, un fragment présent ou non) ; `{{` et `}}` écrivent une
accolade. Un texte modifié n'utilise que les variables de son défaut.

  text(clé, **variables)   le texte, modifié ou par défaut, rempli
  ref(clé)                 un texte à résoudre plus tard, dans un schéma
                           (resolve) : la description d'un paramètre
  catalog(), set_override(), reset()   pour la page (ui/prompt_routes.py)
"""

import os
import re
import string
import threading
import tomllib
from pathlib import Path

from ..core.settings import log
from ..storage import prompt_overrides

DOMAINS = ("tools", "page", "mcp")
MAX_CHARS = 20_000
_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")
_FORMATTER = string.Formatter()


class PromptError(ValueError):
    """Un texte refusé : variable inconnue ou mal écrite, accolade seule,
    texte trop long. Le message dit pourquoi."""


def _parts(template: str) -> list[tuple[str, str | None]]:
    """Le texte découpé en (littéral, variable ou None). Seuls des noms
    simples : ni attribut, ni index, ni format."""
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise PromptError(f"accolade mal fermée ({exc}) : écrire {{{{ ou }}}} pour une accolade") from exc
    out = []
    for literal, field, spec, conversion in parsed:
        if field is not None and (not _NAME.match(field) or spec or conversion):
            raise PromptError(f"variable « {{{field}}} » mal écrite : un nom simple, comme {{max_pages}}")
        out.append((literal, field))
    return out


def variables(template: str) -> list[str]:
    """Les variables d'un texte, dans l'ordre, sans doublon."""
    return list(dict.fromkeys(f for _, f in _parts(template) if f is not None))


def _load_catalog() -> dict[str, tuple[str, str]]:
    """Clé → (domaine, texte par défaut), de tous les fichiers."""
    out: dict[str, tuple[str, str]] = {}
    folder = Path(__file__).parent
    for domain in DOMAINS:
        with open(folder / f"{domain}.toml", "rb") as fh:
            doc = tomllib.load(fh)
        for subject, entries in doc.items():
            for name, value in entries.items():
                key = f"{subject}.{name}"
                if key in out or not isinstance(value, str):
                    raise RuntimeError(f"prompts/{domain}.toml : clé « {key} » en double ou pas un texte")
                variables(value)                  # un défaut mal écrit : au démarrage
                out[key] = (domain, value)
    return out


CATALOG = _load_catalog()

# Les modifications de l'admin, relues quand prompts.json change (une
# restauration de sauvegarde, une autre instance) : ((chemin, date),
# textes, lisible).
_cache: tuple[tuple | None, dict[str, str], bool] = (None, {}, True)


def _mtime() -> tuple:
    """Ce qui dit que le fichier a changé : chemin, date, inode, taille (un
    remplacement atomique dans le même tic d'horloge change l'inode)."""
    try:
        st = os.stat(prompt_overrides.PATH)
        return prompt_overrides.PATH, (st.st_mtime_ns, st.st_ino, st.st_size)
    except OSError:
        return prompt_overrides.PATH, None


def _overrides() -> dict[str, str]:
    global _cache
    mtime = _mtime()
    if mtime != _cache[0]:
        loaded = prompt_overrides.load() if mtime[1] is not None else {}
        if loaded is None:
            log.warning("prompts : textes par défaut")
        # Une modification devenue invalide (le défaut a changé de
        # variables avec une mise à jour) : le défaut, dit au journal.
        valid = {}
        for key, value in (loaded or {}).items():
            try:
                _check(key, value)
                valid[key] = value
            except (KeyError, PromptError) as exc:
                log.warning("prompts : modification de « %s » ignorée (%s)", key, exc)
        _cache = (mtime, valid, loaded is not None)
    return _cache[1]


# Lire, modifier, écrire prompts.json : une écriture à la fois (les routes
# écrivent depuis des fils), sinon deux modifications croisées s'écrasent.
_WRITE = threading.Lock()


class Unreadable(RuntimeError):
    """prompts.json est illisible : la page ne l'écrase pas."""


def _write(current: dict[str, str]) -> None:
    global _cache
    prompt_overrides.save(current)
    _cache = (_mtime(), current, True)


def _editable() -> dict[str, str]:
    current = dict(_overrides())
    if not _cache[2]:
        raise Unreadable(f"{prompt_overrides.PATH} est illisible : le corriger ou le retirer à la main")
    return current


def _check(key: str, text_: str) -> None:
    if key not in CATALOG:
        raise KeyError(key)
    if not text_.strip():
        raise PromptError("texte vide : « Rétablir » rend le texte par défaut")
    if len(text_) > MAX_CHARS:
        raise PromptError(f"texte trop long ({len(text_)} caractères ; {MAX_CHARS} au plus)")
    allowed = set(variables(CATALOG[key][1]))
    if unknown := [v for v in variables(text_) if v not in allowed]:
        raise PromptError(f"variable(s) inconnue(s) : {', '.join('{' + v + '}' for v in unknown)} ; "
                          f"permises : {', '.join('{' + v + '}' for v in sorted(allowed)) or 'aucune'}")


def template(key: str) -> str:
    """Le texte brut (modifié ou par défaut), variables non remplies."""
    return _overrides().get(key, CATALOG[key][1])


def text(key: str, **values) -> str:
    """Le texte de `key`, ses variables remplies. Une variable non fournie
    est une faute du code (KeyError)."""
    return "".join(literal + ("" if field is None else str(values[field]))
                   for literal, field in _parts(template(key)))


class Ref(str):
    """Un texte du catalogue nommé dans un schéma, résolu par resolve."""


def ref(key: str) -> Ref:
    if key not in CATALOG:
        raise KeyError(f"prompt inconnu : {key}")
    return Ref(key)


def resolve(value, **values):
    """Une COPIE de `value` (dict, liste…) où chaque Ref est remplacé par
    son texte."""
    if isinstance(value, Ref):
        return text(str(value), **values)
    if isinstance(value, dict):
        return {k: resolve(v, **values) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, **values) for v in value]
    return value


def catalog() -> list[dict]:
    """Chaque texte, pour la page : clé, domaine, défaut, modification
    (None sans), variables permises."""
    changed = _overrides()
    return [{"key": k, "domain": d, "default": v, "override": changed.get(k),
             "variables": variables(v)} for k, (d, v) in CATALOG.items()]


def entry(key: str) -> dict:
    return next(e for e in catalog() if e["key"] == key)


def set_override(key: str, text_: str) -> None:
    """Modifie le texte de `key`. KeyError : clé inconnue ; PromptError :
    texte refusé ; Unreadable : prompts.json illisible. Un texte égal au
    défaut retire la modification."""
    _check(key, text_)
    with _WRITE:
        current = _editable()
        if text_ == CATALOG[key][1]:
            current.pop(key, None)
        else:
            current[key] = text_
        _write(current)


def reset(key: str) -> None:
    """Rend à `key` son texte par défaut. KeyError : clé inconnue ;
    Unreadable : prompts.json illisible."""
    if key not in CATALOG:
        raise KeyError(key)
    with _WRITE:
        current = _editable()
        if current.pop(key, None) is not None:
            _write(current)
