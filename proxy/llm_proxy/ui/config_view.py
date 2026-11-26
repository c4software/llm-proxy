"""
La configuration EFFECTIVE, pour la page (Configuration → Proxy) : tout ce
que le proxy lit, avec la valeur du fichier ou, à défaut, celle qu'il
prend d'office — marquée « défaut ». La page la modifie, la vérifie et
l'ENREGISTRE : le fichier est mis à jour valeur par valeur
(core/config_file.py), puis rechargé.

  GET  /ui/config        le TOML effectif, secrets masqués, ses valeurs
                         (de quoi préremplir le formulaire de la page) et
                         l'empreinte du fichier (`base`)
  POST /ui/config/merge  le fichier partiel du formulaire appliqué sur la
                         configuration effective : le fichier complet
  POST /ui/config/check  un TOML modifié : syntaxe, clés inconnues du
                         proxy — rien n'est écrit
  POST /ui/config/save   {toml, base} : ce qui change par rapport au
                         fichier y est écrit, puis rechargé à chaud ;
                         400 : TOML illisible, clé inconnue, valeur
                         refusée au rechargement (le fichier est remis) ;
                         409 : le fichier a changé depuis `base`
  POST /ui/config/reload le fichier relu, comme un SIGHUP

Les valeurs viennent du fichier tel qu'il est écrit : un «${VAR}» reste
«${VAR}» (sa valeur est dans l'environnement, jamais montrée). Une valeur
secrète écrite en clair (clé, jeton, mot de passe, en-tête) est masquée.
Avec les comptes OIDC, réservé aux admins ; sinon, à qui ouvre la page.
"""

import asyncio
import json
import re
import tomllib

from fastapi import APIRouter, Request

from ..access import oidc
from ..core import config, config_docs, config_file, reload
from ..core.httputil import error_response, json_only, parse_json
from .dashboard import admin_only

router = APIRouter()

MASK_FORM = config_file.MASK_FORM
MASK_RE = config_file.MASK_RE
# Les tables dont les clés sont libres : des noms (backends, familles,
# serveurs MCP, alias de modèles, extensions et leurs réglages, clés du
# catalogue des prompts), pas des réglages connus d'avance.
FREE = ("backends", "anthropic.model_map", "quotas.family_limits", "extensions", "prompts")
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
    """Une valeur secrète écrite en clair → son masque (un condensé qui la
    désigne : config_file.mask) ; «${VAR}» gardé (ce n'est qu'un renvoi à
    l'environnement)."""
    if isinstance(value, dict):
        return {k: _mask(v, path + (k,)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, path) for v in value]
    if isinstance(value, str) and value and _secret(path) and not _VAR.search(value):
        return config_file.mask(value)
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
    """LIRE la configuration : un admin avec les comptes OIDC ; sans
    comptes, la page connectée."""
    return admin_only(request) if oidc.ENABLED else None


NO_WRITE = ("sans comptes OIDC, la page n'écrit pas config.toml : le modifier à la main "
            "(ou le télécharger depuis Configuration → Proxy), puis Recharger")


def write_forbidden(request: Request):
    """ÉCRIRE config.toml (Proxy, Prompts, Extensions) : un admin OIDC
    seulement, comme la sauvegarde ; sans comptes, personne."""
    if not oidc.ENABLED:
        return error_response("openai", 403, "forbidden", NO_WRITE)
    return admin_only(request)


def writable(request: Request) -> bool:
    """La page peut-elle écrire config.toml (de quoi montrer ou cacher
    Enregistrer, Modifier, Activer) ?"""
    return write_forbidden(request) is None


# Ce que le formulaire de la page règle ET peut retirer : absent de son
# fichier partiel, le réglage est retiré du fichier complet (case décochée,
# champ vidé). Le reste du formulaire écrit toujours ses clés.
FORM_DROPS = ("oidc", "oidc.redirect_uri", "anthropic.model_map.default",
              "ui.chat_model", "tools.image_generation.model", "tools.ocr.model",
              "tools.transcribe.model")
# Les clés d'un backend que le formulaire règle ; les autres (délais,
# tokenize_path…) restent. Un backend absent du formulaire est retiré.
BACKEND_FORM_KEYS = ("url", "api_key", "api", "quotas", "images", "force_tool_choice",
                     "max_tokens", "fair_share", "chat_template")


def _pop(tree: dict, path: str) -> None:
    *parents, last = path.split(".")
    for part in parents:
        tree = tree.get(part)
        if not isinstance(tree, dict):
            return
    tree.pop(last, None)


def _overlay(base: dict, top: dict, path: tuple[str, ...], touched: set[str]):
    """`top` posé sur `base`. `touched` : les chemins dont la valeur vient
    désormais du formulaire — pas ceux où il redit la valeur en place
    (un défaut redit reste un défaut)."""
    for k, v in top.items():
        p = path + (k,)
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _overlay(base[k], v, p, touched)
        else:
            if base.get(k) != v:
                touched.add(".".join(p))
                if isinstance(v, dict):      # une table neuve : ses clés aussi
                    touched.update(".".join(p + (x,)) for x in v)
            base[k] = v


def _leaves(node, path: str) -> list[str]:
    if not isinstance(node, dict):
        return [path]
    return [leaf for k, v in node.items() for leaf in _leaves(v, f"{path}.{k}")]


def merged(partial: dict, overrides: list | None = None) -> tuple[dict, set[str]]:
    """Le fichier partiel du formulaire appliqué sur la configuration
    effective : ce qu'il écrit remplace, ce qu'il retire (FORM_DROPS,
    backends) disparaît, tout le reste est gardé tel quel. Puis
    `overrides` (la section « Avancé » de la page) : [{"path": [...],
    "value": …}], chaque réglage posé à son chemin."""
    tree, defaults = effective()
    for path in FORM_DROPS:
        node, current = partial, tree
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            current = current.get(part) if isinstance(current, dict) else None
        # Omis par le formulaire : retiré — sauf s'il n'y a là que des
        # défauts (rien du fichier à retirer, la vue complète les garde).
        if node is None and current is not None and \
                any(leaf not in defaults for leaf in _leaves(current, path)):
            _pop(tree, path)
    if isinstance(partial.get("backends"), dict):
        current = tree.get("backends") or {}
        for name in list(current):
            if name not in partial["backends"]:
                del current[name]
            else:
                for key in BACKEND_FORM_KEYS:
                    if key not in partial["backends"][name]:
                        current[name].pop(key, None)
                # Ses en-têtes : la table du formulaire remplace l'ancienne
                # (un en-tête retiré du champ disparaît).
                current[name].pop("headers", None)
    touched: set[str] = set()
    _overlay(tree, partial, (), touched)
    for item in overrides or ():
        path, value = item.get("path"), item.get("value")
        if not (isinstance(path, list) and path and all(isinstance(x, str) for x in path)):
            raise ValueError("override : `path` attendu, une liste de noms")
        node = tree
        for part in path[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = node[part] = {}
            node = nxt
        if node.get(path[-1]) != value:
            node[path[-1]] = value
            touched.add(".".join(path))
    return tree, {d for d in defaults if d not in touched}


def docs(tree: dict) -> dict[str, str]:
    """La description de chaque table et de chaque réglage de l'arbre
    (config_docs : les commentaires de data/config.example.toml)."""
    out: dict[str, str] = {}

    def walk(node: dict, path: tuple[str, ...]):
        for k, v in node.items():
            p = path + (k,)
            if (doc := config_docs.describe(".".join(p))):
                out[".".join(p)] = doc
            if isinstance(v, dict):
                walk(v, p)
    walk(tree, ())
    return out


@router.get("/ui/config")
async def ui_config(request: Request):
    if (bad := forbidden(request)) is not None:
        return bad
    tree, defaults = effective()
    _, base = config_file.read()
    return {"path": config.CONFIG_PATH, "toml": dumps(tree, defaults), "base": base,
            "writable": writable(request),
            "defaults": len(defaults), "values": tree,
            "default_paths": sorted(defaults),
            # Le rôle de chaque réglage, pour la section Avancé.
            "docs": docs(tree)}


@router.post("/ui/config/merge")
async def ui_config_merge(request: Request):
    """Le formulaire Proxy de la page → le fichier complet. Rien n'est
    écrit."""
    if (bad := forbidden(request)) is not None:
        return bad
    body = parse_json(await request.body())
    text = body.get("toml") if isinstance(body, dict) else None
    if not isinstance(text, str):
        return error_response("openai", 400, "invalid_request_error",
                              "corps attendu : {\"toml\": \"…\"}")
    try:
        partial = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return error_response("openai", 400, "invalid_request_error",
                              f"TOML du formulaire invalide : {exc}")
    overrides = body.get("overrides") or []
    if not isinstance(overrides, list) or not all(isinstance(o, dict) for o in overrides):
        return error_response("openai", 400, "invalid_request_error",
                              "overrides : une liste de {path, value}")
    try:
        tree, defaults = merged(partial, overrides)
    except ValueError as exc:
        return error_response("openai", 400, "invalid_request_error", str(exc))
    return {"toml": dumps(tree, defaults), "defaults": len(defaults)}


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
    masked = len(MASK_RE.findall(text))
    return {"ok": True, "error": None, "unknown": unknown(tree), "masked": masked}


def write_error(exc: Exception):
    """L'erreur d'une écriture de config.toml, pour les trois écrivains."""
    if isinstance(exc, config_file.Stale):
        return error_response("openai", 409, "conflict", str(exc))
    if isinstance(exc, config_file.Absent):
        return error_response("openai", 409, "conflict", str(exc))
    return error_response("openai", 400, "invalid_config", str(exc))


WRITE_ERRORS = (config_file.Stale, config_file.Absent, config_file.Invalid)


@router.post("/ui/config/save")
async def ui_config_save(request: Request):
    """Enregistre le TOML de la page : seules les valeurs qui changent
    sont écrites (un masque redevient la valeur qu'il désigne, un défaut
    affiché n'est pas ajouté), après une évaluation à blanc de chaque
    réglage (une valeur que le proxy refuserait au démarrage : 400, rien
    d'écrit), puis le fichier est rechargé. Une valeur que le rechargement
    refuse : l'ancien fichier est remis, 400."""
    if (bad := write_forbidden(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    body = parse_json(await request.body())
    text = body.get("toml") if isinstance(body, dict) else None
    base = body.get("base") if isinstance(body, dict) else None
    if not isinstance(text, str) or not isinstance(base, str):
        return error_response("openai", 400, "invalid_request_error",
                              'corps attendu : {"toml": "…", "base": "…"}')
    try:
        tree = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return error_response("openai", 400, "invalid_config", f"TOML invalide : {exc}")
    if bad := unknown(tree):
        return error_response("openai", 400, "invalid_config",
                              f"clé(s) inconnue(s) du proxy : {', '.join(bad)}")

    def check(new: str) -> None:
        try:
            reload.validate(new)
        except reload.ReloadError as exc:
            raise config_file.Invalid(f"refusé : {exc}") from None

    def write():
        try:
            current = raw()
        except FileNotFoundError:
            raise config_file.Absent(f"{config.CONFIG_PATH} n'existe pas") from None
        except tomllib.TOMLDecodeError as exc:
            raise config_file.Invalid(f"{config.CONFIG_PATH} illisible : {exc}") from None
        sets, deletes = config_file.diff(current, tree)
        return len(sets) + len(deletes), config_file.update(sets, deletes, base, check=check)

    try:
        changed, (before, new_base) = await asyncio.to_thread(write)
    except WRITE_ERRORS as exc:
        return write_error(exc)
    try:
        result = reload.reload()
    except reload.ReloadError as exc:
        try:
            await asyncio.to_thread(config_file.restore, before, new_base)
            kept = "fichier remis tel quel"
        except WRITE_ERRORS:
            kept = "fichier modifié entre-temps, laissé tel quel"
        return error_response("openai", 400, "invalid_config", f"{exc} — {kept}")
    return {**result, "base": new_base, "changed": changed}


@router.post("/ui/config/reload")
async def ui_config_reload(request: Request):
    """Relit data/config.toml et applique à chaud les réglages
    rechargeables (core/reload.py), comme un SIGHUP. Rend ce qui a été
    appliqué et ce qui attend un redémarrage ; un fichier invalide ne
    change rien (400)."""
    if (bad := forbidden(request)) is not None:
        return bad
    if (bad := json_only(request)) is not None:
        return bad
    try:
        result = reload.reload()
    except reload.ReloadError as exc:
        return error_response("openai", 400, "invalid_config", str(exc))
    return {**result, "reloadable": list(reload.SAFE)}
