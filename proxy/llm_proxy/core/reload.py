"""
Le rechargement À CHAUD d'une partie de la configuration (SIGHUP, ou
POST /ui/config/reload) : data/config.toml relu, et les réglages de SAFE
appliqués sans redémarrer. Tout autre changement (backends, chemins,
secrets, outils activés…) attend un redémarrage, et le rapport le dit.

Chaque réglage est une constante de module lue à l'import :
`NOM = config.integer("tools.max_calls", 8)`. Pour la recharger, le
module relit le SOURCE des modules du proxy (ast) une fois et y trouve :
  * les affectations de premier niveau dont l'expression appelle
    `config.<lecteur>("<clé>", …)` : elles sont réévaluées telles quelles,
    dans l'espace du module, sur la configuration relue ;
  * les `from … import NOM` qui recopient une telle constante (un paquet
    qui réexporte ses parties, `from ..core.settings import
    PROXY_API_KEYS`) : la copie suit ;
  * les affectations qui en dérivent sans lire la configuration
    (`HOSTED_HARD_LIMIT = tools.MAX_CALLS + …`) : réévaluées après.

Tout est évalué AVANT d'être appliqué : une valeur invalide (TOML illisible,
un entier qui n'en est pas un) laisse la configuration en vigueur intacte.
Le rechargement s'exécute dans la boucle d'événements : aucune requête ne
voit une moitié de configuration.
"""

import ast
import importlib.util
import inspect
import logging
import sys

from . import config

log = logging.getLogger("albert-proxy")

PACKAGE = __name__.split(".")[0]          # «llm_proxy»

# Les réglages rechargeables : lus à chaque usage (pas d'objet construit
# à l'import autour d'eux), sans effet sur l'état gardé. Une clé couvre ses
# sous-clés (anthropic.model_map.<nom>).
SAFE = (
    "proxy.api_keys",
    "anthropic.model_map",
    "anthropic.ping_interval",
    "anthropic.trace",
    "anthropic.reasoning_as_thinking",
    "responses.reasoning_as_summary",
    "chat.always",
    "chat.always_except",
    "chat.annotations",
    "fair_share.tokens_per_hour",
    "tools.max_calls",
    "tools.run_timeout",
    "tools.max_result_chars",
    "tools.direct_calls_per_minute",
    "quotas.max_queue_seconds",
    "mcp_server.allowed_origins",
    "ui.page_tools_disabled",
    "ui.linux_relay_max_bytes",
    "ui.linux_relay_idle_seconds",
)


def _safe(key: str) -> bool:
    return any(key == s or key.startswith(s + ".") for s in SAFE)


# ── Lecture du source ───────────────────────────────────────────────────

class _Site:
    """Une affectation de premier niveau : sa cible (un nom, ou plusieurs
    pour `A, B = …`), son expression compilée, les clés de configuration
    qu'elle lit, les noms qu'elle cite."""

    def __init__(self, module: str, targets: list[str], node: ast.expr,
                 keys: set[str], refs: set[tuple[str, str]]):
        self.module, self.targets, self.keys, self.refs = \
            module, targets, keys, refs
        self.code = compile(ast.Expression(node), f"<reload {module}>", "eval")


def _targets(node) -> list[str] | None:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple) and all(isinstance(e, ast.Name) for e in node.elts):
        return [e.id for e in node.elts]
    return None


def _config_keys(expr: ast.expr) -> set[str]:
    """Les clés lues par `config.<lecteur>("<clé>", …)` dans `expr`."""
    keys = set()
    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == "config" and n.args \
                and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str):
            keys.add(n.args[0].value)
    return keys


def _refs(expr: ast.expr) -> set[tuple[str, str]]:
    """Les noms cités : ("", NOM) pour un nom du module, (alias, NOM) pour
    `alias.NOM`."""
    out = set()
    for n in ast.walk(expr):
        if isinstance(n, ast.Name):
            out.add(("", n.id))
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            out.add((n.value.id, n.attr))
    return out


_SCAN: tuple[list[_Site], dict] | None = None


def _scan() -> tuple[list[_Site], dict]:
    """(sites, copies) des modules du proxy chargés ; copies :
    (module source, NOM) → [(module qui l'importe, son nom local)]."""
    global _SCAN
    if _SCAN is not None:
        return _SCAN
    sites: list[_Site] = []
    copies: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for name, module in list(sys.modules.items()):
        if module is None or not (name == PACKAGE or name.startswith(PACKAGE + ".")):
            continue
        try:
            tree = ast.parse(inspect.getsource(module))
        except (OSError, TypeError, SyntaxError):
            continue
        package = name if hasattr(module, "__path__") else name.rpartition(".")[0]
        for stmt in tree.body:
            if isinstance(stmt, ast.ImportFrom):
                try:
                    source = importlib.util.resolve_name(
                        "." * stmt.level + (stmt.module or ""), package)
                except ImportError:
                    continue
                for alias in stmt.names:
                    copies.setdefault((source, alias.name), []).append(
                        (name, alias.asname or alias.name))
                continue
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target, value = stmt.target, stmt.value
            else:
                continue
            names = _targets(target)
            if names:
                sites.append(_Site(name, names, value, _config_keys(value),
                                   _refs(value)))
    _SCAN = sites, copies
    return _SCAN


def prepare() -> None:
    """Lit le source des modules une fois (au démarrage, hors de la boucle)."""
    _scan()


def _bindings(module: str, name: str, copies: dict) -> list[tuple[str, str]]:
    """(module, nom) et toutes ses copies, de proche en proche."""
    out, todo = [], [(module, name)]
    while todo:
        pair = todo.pop()
        if pair in out:
            continue
        out.append(pair)
        todo.extend(copies.get(pair, ()))
    return out


# ── Comparaison ─────────────────────────────────────────────────────────

def _leaves(node, path: str = "") -> dict[str, object]:
    if isinstance(node, dict) and node:
        out = {}
        for k, v in node.items():
            out.update(_leaves(v, f"{path}.{k}" if path else str(k)))
        return out
    return {path: node} if path else {}


def _lookup(tree: dict, key: str):
    node = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _put(tree: dict, key: str, value) -> None:
    parts = key.split(".")
    node = tree
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = node[part] = {}
        node = nxt
    if value is None:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = value


# ── Rechargement ────────────────────────────────────────────────────────

class ReloadError(Exception):
    pass


def reload(path: str | None = None) -> dict:
    """Relit la configuration et applique ce qui peut l'être. Rend
    {"applied": [clés], "restart": [clés changées non rechargeables]} ;
    lève ReloadError (rien n'a changé) si le fichier est illisible ou
    une valeur invalide."""
    path = path or config.CONFIG_PATH
    if config.SETUP:
        raise ReloadError("proxy en mode installation : redémarrer après "
                          "avoir posé la configuration")
    try:
        new = config.load(path)
    except SystemExit as exc:
        raise ReloadError(str(exc)) from None
    old = config.CONFIG
    changed = {k for k in set(_leaves(old)) | set(_leaves(new))
               if _lookup(old, k) != _lookup(new, k)}
    applied = sorted({s for s in SAFE
                      if _lookup(old, s) != _lookup(new, s)})
    restart = sorted(k for k in changed if not _safe(k))
    if not applied:
        _report(applied, restart)
        return {"applied": applied, "restart": restart}

    # La configuration en vigueur, avec les seules clés rechargeables
    # remplacées : ce que lira le proxy, et ce que montre la page.
    staged = _copy(old)
    for key in applied:
        _put(staged, key, _copy(_lookup(new, key)))

    sites, copies = _scan()
    touched = set(applied)
    todo = [s for s in sites if s.keys and s.keys & _covering(touched, s.keys)
            and all(_safe(k) for k in s.keys)]
    saved: dict[tuple[str, str], object] = {}

    def apply(values: dict) -> None:
        for (module, name), value in values.items():
            for mod, local in _bindings(module, name, copies):
                target = sys.modules.get(mod)
                if target is not None and hasattr(target, local):
                    saved.setdefault((mod, local), getattr(target, local))
                    setattr(target, local, value)

    config.CONFIG = staged
    try:
        values: dict[tuple[str, str], object] = {}
        for site in todo:
            values.update(_evaluate(site, values))
        apply(values)
        # Ce qui en dérive sans lire la configuration (une passe), évalué
        # sur les valeurs nouvelles, déjà en place.
        updated = {b for (m, n) in values for b in _bindings(m, n, copies)}
        derived: dict[tuple[str, str], object] = {}
        for site in sites:
            if not site.keys and _depends(site, updated):
                derived.update(_evaluate(site, derived))
        apply(derived)
    except (SystemExit, Exception) as exc:
        for (mod, local), value in saved.items():
            setattr(sys.modules[mod], local, value)
        config.CONFIG = old
        raise ReloadError(f"valeur invalide, rien n'est appliqué : {exc}") from None

    _report(applied, restart)
    return {"applied": applied, "restart": restart}


def _covering(touched: set[str], keys: set[str]) -> set[str]:
    """Les clés d'un site concernées par les clés changées (une clé
    changée couvre ses sous-clés, et inversement)."""
    return {k for k in keys for t in touched
            if k == t or k.startswith(t + ".") or t.startswith(k + ".")}


def _depends(site: _Site, updated: set[tuple[str, str]]) -> bool:
    module = sys.modules.get(site.module)
    if module is None:
        return False
    for alias, name in site.refs:
        if alias == "":
            if (site.module, name) in updated:
                return True
            continue
        other = getattr(module, alias, None)
        other_name = getattr(other, "__name__", None)
        if other_name and (other_name, name) in updated:
            return True
    return False


def _evaluate(site: _Site, pending: dict) -> dict:
    """La nouvelle valeur des cibles de `site`, évaluée dans l'espace de
    son module — sur les valeurs déjà recalculées de ce module (`pending`)."""
    module = sys.modules[site.module]
    scope = dict(vars(module))
    for (mod, name), value in pending.items():
        if mod == site.module:
            scope[name] = value
    value = eval(site.code, scope)
    if len(site.targets) == 1:
        return {(site.module, site.targets[0]): value}
    value = tuple(value)
    if len(value) != len(site.targets):
        raise ValueError(f"{site.module} : {len(site.targets)} valeurs attendues")
    return {(site.module, t): v for t, v in zip(site.targets, value)}


def _copy(node):
    if isinstance(node, dict):
        return {k: _copy(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_copy(v) for v in node]
    return node


def _report(applied: list[str], restart: list[str]) -> None:
    if applied:
        log.info("configuration rechargée : %s", ", ".join(applied))
    else:
        log.info("configuration relue : aucun réglage rechargeable n'a changé")
    if restart:
        log.warning("configuration : %d réglage(s) modifié(s) attendent un "
                    "redémarrage : %s", len(restart), ", ".join(restart))
