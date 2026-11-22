"""
La DESCRIPTION de chaque réglage, lue dans data/config.example.toml : le
bloc de commentaires juste au-dessus d'une clé (et son commentaire de fin
de ligne). C'est la seule documentation des champs : la page la montre
sous chaque réglage de Configuration → Proxy, section Avancé
(GET /ui/config, `docs`), et un test exige qu'aucun réglage lu par le
proxy n'en manque.

Règles de lecture :
  * une table `[a.b]` (ou `# [a.b]`, commentée) fixe le préfixe ; le bloc
    au-dessus de son en-tête la décrit (docs["a.b"]), ainsi que les clés
    qui la suivent sans commentaire à elles (`enabled`, `issuer`…) ;
  * le bloc de commentaires qui précède une clé la décrit ; il vaut aussi
    pour les clés qui la suivent sans ligne vide ni nouveau commentaire
    (« Bornes : une conversation, une image… » au-dessus de trois clés) ;
  * une clé commentée (`# cle = valeur`, un exemple) n'interrompt pas le
    bloc et n'y entre pas ; elle est elle-même décrite par lui ;
  * les filets (`# ── Section ──`) et les lignes vides ferment le bloc ;
  * dans une table à noms libres ([backends.<nom>]…), le nom devient `*`
    (backends.*.url) : une description vaut pour tous. Le commentaire
    au-dessus d'UNE instance (« Backend local llama.cpp ») ne décrit pas
    ses champs ; eux viennent des lignes `#   champ — rôle` du bloc qui
    précède la première instance (la liste « Champs » des backends).
"""

import os
import re

from .config import EXAMPLE_PATH

# Les tables dont les clés de second niveau sont des NOMS (un backend, une
# famille de modèles…) : leurs réglages se décrivent une fois, pour tous.
FREE = ("backends", "quotas.family_limits", "repos.sources")
# Les tables MIXTES : des réglages à elles ([tools.mcp].timeout) et des
# sous-tables nommées ([tools.mcp.<serveur>]) ; seul un chemin à deux
# niveaux (tools.mcp.docs.url) y devient générique.
MIXED = ("tools.mcp", "extensions")

# Clés lues à leur ancien emplacement, à défaut du nouveau (tools/net.py).
ALIASES = {f"tools.web_fetch.{k}": f"tools.net.{k}"
           for k in ("allow_private", "allowed_domains", "blocked_domains", "user_agent")}

_TABLE = re.compile(r"^\s*#?\s*\[+\s*([A-Za-z0-9_.\-\"*]+?)\s*\]+\s*(#.*)?$")
_KEY = re.compile(r"^\s*(#\s*)?([A-Za-z0-9_\-]+|\"[^\"]+\")\s*=\s*(.*)$")
_RULE = re.compile(r"^\s*#\s*[─━=\-]{2,}")
# `#   champ — rôle` (liste de champs d'une table à noms libres), et sa
# suite plus indentée.
_FIELD = re.compile(r"^#\s{2,3}([a-z_]+)\s+—\s+(.*)$")
_MORE = re.compile(r"^#\s{4,}(\S.*)$")


def _generic(path: str) -> str:
    """backends.albert.url → backends.*.url (tables à noms libres)."""
    for free in FREE:
        if path.startswith(free + "."):
            rest = path[len(free) + 1:].split(".", 1)
            return free + ".*" + ("." + rest[1] if len(rest) > 1 else "")
    for mixed in MIXED:
        if path.startswith(mixed + "."):
            rest = path[len(mixed) + 1:].split(".", 1)
            if len(rest) > 1:
                return mixed + ".*." + rest[1]
    return path


def _free_root(table: str) -> str:
    return next((f for f in FREE if table.startswith(f + ".*")), "")


def _inline(value: str) -> str:
    """Le commentaire de fin de ligne d'une valeur (hors chaîne)."""
    quote = None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return value[i + 1:].strip()
    return ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(";:").strip()


def parse(text: str) -> dict[str, str]:
    docs: dict[str, str] = {}
    table = ""
    block: list[str] = []         # le bloc de commentaires en cours
    current = ""                  # la description des clés qui se suivent
    fields: dict[str, str] = {}   # `#   champ — rôle`, en attente d'une table
    field = ""                    # le champ dont on lit la suite
    depth = 0                     # dans une valeur sur plusieurs lignes
    for line in text.splitlines():
        if depth:
            depth += line.count("[") + line.count("{") - line.count("]") - line.count("}")
            depth = max(depth, 0)
            continue
        stripped = line.strip()
        if not stripped or _RULE.match(line):
            block, current, field = [], "", ""
            continue
        m = _TABLE.match(line)
        if m and "=" not in stripped:
            table = _generic(m.group(1).replace('"', ""))
            root = _free_root(table)
            about = _clean(" ".join(block))
            block, field = [], ""
            if root:
                # Une instance : son commentaire décrit la table entière (s'il
                # n'y en a pas déjà), jamais les champs ; ceux-ci viennent de
                # la liste « Champs » lue avant.
                if about:
                    docs.setdefault(root, about)
                for name, doc in fields.items():
                    docs.setdefault(f"{root}.*.{name}", _clean(doc))
                fields = {}
                current = ""
            else:
                current = about
                if about:
                    docs.setdefault(table, about)
            continue
        # Une ligne de la liste « Champs » (ou sa suite) : un champ, pas une
        # clé commentée (« false = rien (défaut) » en serait une).
        if (f := _FIELD.match(stripped)) or (field and (f := _MORE.match(stripped))):
            current = ""
            if f.re is _FIELD:
                # L'intitulé de la liste (« Champs : ») n'entre pas dans le
                # texte de la table.
                if block and block[-1].endswith(":"):
                    block.pop()
                field = f.group(1)
                fields[field] = f.group(2)
            else:
                fields[field] += " " + f.group(1)
            continue
        m = _KEY.match(line)
        if m:
            commented, key, value = m.group(1), m.group(2).strip('"'), m.group(3)
            if block:
                current = _clean(" ".join(block))
                block = []
            path = _generic(f"{table}.{key}" if table else key)
            own = "" if commented else _inline(value)
            doc = _clean(" ".join(x for x in (current, own) if x))
            if doc:
                docs.setdefault(path, doc)
            if not commented:
                depth = max(value.count("[") + value.count("{")
                            - value.count("]") - value.count("}"), 0)
            continue
        if stripped.startswith("#"):
            current, field = "", ""   # un nouveau commentaire : un nouveau bloc
            block.append(stripped.lstrip("#").strip())
    return docs


def _load() -> dict[str, str]:
    if not os.path.exists(EXAMPLE_PATH):
        return {}
    with open(EXAMPLE_PATH, encoding="utf-8") as fh:
        return parse(fh.read())


DOCS: dict[str, str] = _load()


def describe(path: str) -> str:
    """La description d'un réglage (chemin pointé), "" si aucune."""
    if path in ALIASES:
        new = ALIASES[path]
        doc = describe(new)
        return f"Ancien emplacement, lu à défaut de [{new.rsplit('.', 1)[0]}]." \
               f"{new.rsplit('.', 1)[1]} : y déplacer la clé." + (f" {doc}" if doc else "")
    generic = _generic(path)
    if (doc := DOCS.get(path) or DOCS.get(generic)):
        return doc
    # Un champ non décrit d'une table à noms libres : la table.
    root = _free_root(generic)
    return DOCS.get(root, "") if root else ""
