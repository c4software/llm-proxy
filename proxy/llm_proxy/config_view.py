"""
La configuration EFFECTIVE, pour la page (Configuration → Paramètres) :
tout ce que le proxy lit, avec la valeur du fichier ou, à défaut, celle
qu'il prend d'office — marquée « défaut ». Lecture seule : la page peut
modifier le texte, le vérifier, le copier ou le télécharger, jamais
l'écrire sur le proxy (il faudrait un redémarrage, et les secrets vivent
dans le .env de l'hôte).

  GET  /ui/config        le TOML effectif, secrets masqués
  POST /ui/config/check  un TOML modifié : syntaxe, clés inconnues du
                         proxy — rien n'est écrit

Les valeurs viennent du fichier tel qu'il est écrit : un «${VAR}» reste
«${VAR}» (sa valeur est dans l'environnement, jamais montrée). Une valeur
secrète écrite en clair (clé, jeton, mot de passe, en-tête) est masquée.
Avec les comptes OIDC, réservé aux admins ; sinon, à qui ouvre la page.
"""

import json
import re
import tomllib

from fastapi import APIRouter, Request

from . import config
from . import oidc
from .httputil import error_response, parse_json
from .ui_routes import admin_only

router = APIRouter()

MASK = "••• masqué •••"
# Les tables dont les clés sont libres : des noms (backends, familles,
# serveurs MCP, alias de modèles), pas des réglages connus d'avance.
FREE = ("backends", "anthropic.model_map", "quotas.family_limits",
        "quotas.router_models")
_SECRET = re.compile(r"(^|_)(api_keys?|secret|password|token)s?$|secret|password",
                     re.IGNORECASE)
_VAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def raw() -> dict:
    """Le fichier tel qu'il est écrit, «${VAR}» non substitués."""
    with open(config.CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


def _secret(path: tuple[str, ...]) -> bool:
    return bool(_SECRET.search(path[-1])) or "headers" in path[:-1]


def _mask(value, path: tuple[str, ...]):
    """Une valeur secrète écrite en clair → MASK ; «${VAR}» gardé (ce
    n'est qu'un renvoi à l'environnement)."""
    if isinstance(value, dict):
        return {k: _mask(v, path + (k,)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, path) for v in value]
    if isinstance(value, str) and value and _secret(path) and not _VAR.search(value):
        return MASK
    return value


def effective() -> tuple[dict, set[str]]:
    """Le fichier, plus chaque réglage lu qui n'y est pas, à sa valeur par
    défaut. Rend l'arbre et les chemins venus des défauts."""
    tree = raw()
    defaults = set()
    for path, default in sorted(config.READ.items()):
        if default is None or isinstance(default, dict):
            continue                 # pas de défaut à montrer
        parts = path.split(".")
        node = tree
        for part in parts[:-1]:
            nxt = node.get(part)
            if nxt is None:
                nxt = node[part] = {}
            if not isinstance(nxt, dict):
                break
            node = nxt
        else:
            if parts[-1] not in node:
                node[parts[-1]] = list(default) if isinstance(default, tuple) else default
                defaults.add(path)
    return _mask(tree, ()), defaults


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k, ensure_ascii=False)


def dumps(tree: dict, defaults: set[str]) -> str:
    """L'arbre en TOML : les valeurs d'une table, puis ses sous-tables ;
    une valeur venue des défauts suivie de « # défaut »."""
    out: list[str] = []

    def table(node: dict, path: tuple[str, ...]):
        scalars = [(k, v) for k, v in node.items() if not isinstance(v, dict)]
        if path and (scalars or not any(isinstance(v, dict) for v in node.values())):
            out.extend(["", "[" + ".".join(_key(p) for p in path) + "]"])
        for k, v in scalars:
            line = f"{_key(k)} = {_value(v)}"
            if ".".join(path + (k,)) in defaults:
                line += "  # défaut"
            out.append(line)
        for k, v in node.items():
            if isinstance(v, dict):
                table(v, path + (k,))

    table(tree, ())
    return "\n".join(out).lstrip("\n") + "\n"


def unknown(tree: dict) -> list[str]:
    """Les clés d'un TOML que le proxy ne lit pas : une faute de frappe y
    est inerte, sans un mot au démarrage. Sous une table lue entière
    (backends, familles de quotas, serveurs MCP, alias), tout est admis."""
    free = set(FREE) | {p for p, d in config.READ.items() if isinstance(d, dict)}
    known = set(config.READ)
    found = []

    def walk(node: dict, path: tuple[str, ...]):
        for k, v in node.items():
            p = path + (k,)
            dotted = ".".join(p)
            if any(dotted == f or dotted.startswith(f + ".") for f in free):
                continue
            if isinstance(v, dict) and dotted not in known:
                walk(v, p)
            elif dotted not in known:
                found.append(dotted)

    walk(tree, ())
    return found


def forbidden(request: Request):
    return admin_only(request) if oidc.ENABLED else None


@router.get("/ui/config")
async def ui_config(request: Request):
    if (bad := forbidden(request)) is not None:
        return bad
    tree, defaults = effective()
    return {"path": config.CONFIG_PATH, "toml": dumps(tree, defaults),
            "defaults": len(defaults)}


@router.post("/ui/config/check")
async def ui_config_check(request: Request):
    """Vérifie un TOML modifié dans la page : rien n'est écrit."""
    if (bad := forbidden(request)) is not None:
        return bad
    body = parse_json(await request.body())
    text = body.get("toml") if isinstance(body, dict) else None
    if not isinstance(text, str):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"toml\": \"…\"}")
    try:
        tree = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return {"ok": False, "error": str(exc), "unknown": [], "masked": 0}
    masked = text.count(MASK)
    return {"ok": True, "error": None, "unknown": unknown(tree), "masked": masked}
