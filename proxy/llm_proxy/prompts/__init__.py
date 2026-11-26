"""
Les PROMPTS : les textes que le modèle lit — descriptions des outils et de
leurs paramètres, consignes, instructions — hors du code. Les messages
d'erreur et les résultats des outils restent dans le code.

  *.toml     le CATALOGUE, les textes par défaut, en anglais, un fichier
             par domaine (tools, page, chat, mcp) ; une table par sujet :
             [web_fetch] description = "…" donne la clé
             `web_fetch.description`
  [prompts]  les textes MODIFIÉS par l'admin, dans config.toml (clé du
             catalogue = texte) : écrits par Configuration → Prompts
             (core/config_file.py), effet à la requête suivante ; modifiés
             à la main, rechargeables à chaud

Un texte porte des VARIABLES `{nom}` que le code fournit (une borne, le
nom d'un outil, un fragment présent ou non) ; `{{` et `}}` écrivent une
accolade. Un texte modifié n'utilise que les variables de son défaut.

  text(clé, **variables)   le texte, modifié ou par défaut, rempli
  ref(clé)                 un texte à résoudre plus tard, dans un schéma
                           (resolve) : la description d'un paramètre
  for_page()                les gabarits que la PAGE remplit elle-même
                           (/ui/session, `chat.prompts`)
  catalog(), set_override(), reset()   pour la page (ui/prompt_routes.py)
"""

import re
import string
import tomllib
from pathlib import Path

from ..core import config, config_file
from ..core.settings import log

DOMAINS = ("tools", "page", "chat", "mcp")
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

# Les modifications valides de [prompts], refaites quand la table change
# (écrite par la page, rechargée) : (la table lue, ses textes valides).
_cache: tuple[object, dict[str, str]] = (None, {})


def _overrides() -> dict[str, str]:
    global _cache
    table = config.CONFIG.get("prompts")
    if table is not _cache[0]:
        # Une modification devenue invalide (le défaut a changé de
        # variables avec une mise à jour, une clé retirée) : le défaut,
        # dit au journal.
        valid = {}
        for key, value in (table if isinstance(table, dict) else {}).items():
            try:
                if not isinstance(value, str):
                    raise PromptError("pas un texte")
                _check(key, value)
                valid[key] = value
            except (KeyError, PromptError) as exc:
                log.warning("prompts : [prompts].%s ignoré (%s)", key, exc)
        _cache = (table, valid)
    return _cache[1]


def _store(key: str, text_: str | None) -> None:
    """Écrit (ou retire, None) la modification de `key` dans config.toml,
    puis dans la configuration en mémoire — sous le verrou de l'écriture,
    qu'un rechargement prend aussi : les deux ne se croisent pas."""
    def then():
        table = dict(config.CONFIG.get("prompts") or {})
        if text_ is None:
            table.pop(key, None)
        else:
            table[key] = text_
        config.CONFIG["prompts"] = table
    if text_ is None:
        config_file.update({}, [("prompts", key)], then=then)
    else:
        config_file.update({("prompts", key): text_}, then=then)


def _check(key: str, text_: str) -> None:
    if key not in CATALOG:
        raise KeyError(key)
    if not text_.strip():
        raise PromptError("texte vide : « Rétablir » rend le texte par défaut")
    if "${" in text_:
        # Ni substitué ni envoyé : une valeur d'environnement (un secret)
        # n'a rien à faire dans un texte lu par le modèle et la page.
        raise PromptError("« ${ » n'est pas permis : un prompt ne lit pas l'environnement")
    if len(text_) > MAX_CHARS:
        raise PromptError(f"texte trop long ({len(text_)} caractères ; {MAX_CHARS} au plus)")
    allowed = set(variables(CATALOG[key][1]))
    if unknown := [v for v in variables(text_) if v not in allowed]:
        raise PromptError(f"variable(s) inconnue(s) : {', '.join('{' + v + '}' for v in unknown)} ; "
                          f"permises : {', '.join('{' + v + '}' for v in sorted(allowed)) or 'aucune'}")


def template(key: str) -> str:
    """Le texte brut (modifié ou par défaut), variables non remplies."""
    return _overrides().get(key, CATALOG[key][1])


_PAGE_STATE = re.compile(r"family\.[^.]+\.state")


def for_page() -> dict[str, str]:
    """Les gabarits, variables NON remplies, que la page du chat remplit
    (lib/prompts.js) : le domaine `chat`, et l'état du moment des familles
    (`family.<clé>.state…`)."""
    return {k: template(k) for k, (domain, _) in CATALOG.items()
            if domain == "chat" or _PAGE_STATE.match(k)}


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
    texte refusé. Un texte égal au défaut retire la modification."""
    _check(key, text_)
    _store(key, None if text_ == CATALOG[key][1] else text_)


def reset(key: str) -> None:
    """Rend à `key` son texte par défaut. KeyError : clé inconnue."""
    if key not in CATALOG:
        raise KeyError(key)
    _store(key, None)
