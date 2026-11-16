"""
Le PRIX d'une requête, en dollars : les tarifs d'un backend
([backends.<nom>.prices], $ par million de tokens, par motif de modèle)
appliqués à l'usage que le backend a rendu.

    [backends.claude.prices]
    "claude-opus-5-5" = { input = 4.0, output = 20.0, cache_read = 0.2, cache_write = 5.0 }
    "claude-haiku-5-5" = { input = 0.1, output = 0.5, long = { above = 100000, input = 0.5, output = 2.5 } }

  * `input` : les tokens d'entrée lus normalement ; `output` : les tokens
    générés (réflexion comprise) ;
  * `cache_read`, `cache_write` : les tokens lus du cache, écrits dans le
    cache — `input` à défaut ;
  * `long` : un autre tarif quand l'entrée dépasse `above` tokens (ses
    champs absents reprennent ceux du tarif de base).

Un modèle sans tarif n'a pas de prix (None) : un backend local n'en a pas,
et « gratuit » (0) se dit en l'écrivant. Le coût est calculé UNE fois,
quand la requête est comptée (relaying.Call.done) : enregistré dans
stats.db au tarif du moment, rendu au client en `usage.cost` (la forme
d'OpenRouter).
"""

import fnmatch

FIELDS = ("input", "output", "cache_read", "cache_write")


def _rate(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def parse(name: str, cfg) -> list[tuple[str, dict]]:
    """Les tarifs d'un backend, dans l'ordre du fichier : (motif, tarif).
    Une valeur illisible arrête le démarrage, comme le reste de la
    configuration d'un backend."""
    out = []
    for pattern, price in (cfg.items() if isinstance(cfg, dict) else ()):
        where = f"backends.{name}.prices.{pattern!r}"
        if not isinstance(price, dict):
            raise SystemExit(f"{where} : une table attendue ({{ input = …, output = … }})")
        rate = {}
        for k, v in price.items():
            if k == "long":
                continue
            if k not in FIELDS or _rate(v) is None:
                raise SystemExit(f"{where}.{k} : champ inconnu ou valeur non positive "
                                 f"(attendus : {', '.join(FIELDS)}, long)")
            rate[k] = _rate(v)
        if "input" not in rate or "output" not in rate:
            raise SystemExit(f"{where} : input et output sont requis")
        long = price.get("long")
        if long is not None:
            if not isinstance(long, dict) or not isinstance(long.get("above"), int):
                raise SystemExit(f"{where}.long : une table avec above = <tokens> attendue")
            tier = {"above": long["above"]}
            for k, v in long.items():
                if k == "above":
                    continue
                if k not in FIELDS or _rate(v) is None:
                    raise SystemExit(f"{where}.long.{k} : champ inconnu ou valeur non positive")
                tier[k] = _rate(v)
            rate["long"] = tier
        out.append((str(pattern).strip().lower(), rate))
    return out


def rate_of(prices: list[tuple[str, dict]], model: str) -> dict | None:
    """Le tarif du premier motif qui couvre `model` (sans préfixe de
    backend, casse ignorée), ou None."""
    name = str(model or "").lower()
    for pattern, rate in prices:
        if fnmatch.fnmatchcase(name, pattern):
            return rate
    return None


def cost(rate: dict | None, prompt: int, completion: int,
         cached: int = 0, cache_write: int = 0) -> float | None:
    """Le prix, en dollars, d'une requête : `prompt` compte TOUTE
    l'entrée (lue normalement, lue du cache, écrite dans le cache), comme
    `prompt_tokens` d'OpenAI. None sans tarif."""
    if rate is None:
        return None
    cached = max(int(cached or 0), 0)
    cache_write = max(int(cache_write or 0), 0)
    fresh = max(int(prompt or 0) - cached - cache_write, 0)
    r = dict(rate)
    long = r.pop("long", None)
    if long and int(prompt or 0) > long["above"]:
        r.update({k: v for k, v in long.items() if k != "above"})
    total = (fresh * r["input"]
             + cached * r.get("cache_read", r["input"])
             + cache_write * r.get("cache_write", r["input"])
             + max(int(completion or 0), 0) * r["output"])
    return round(total / 1e6, 8)


def _details(u: dict, key: str) -> int:
    d = u.get("prompt_tokens_details")
    v = d.get(key) if isinstance(d, dict) else None
    return int(v) if isinstance(v, (int, float)) and v > 0 else 0


def usage_cost(rate: dict | None, usages) -> float | None:
    """Le prix d'une suite d'usages chat/completions (un par aller-retour
    avec le backend : le palier `long` se juge requête par requête).
    None sans tarif ou sans usage."""
    usages = [u for u in usages or () if isinstance(u, dict)]
    if rate is None or not usages:
        return None
    return round(sum(cost(rate, int(u.get("prompt_tokens") or 0),
                          int(u.get("completion_tokens") or 0),
                          _details(u, "cached_tokens"),
                          _details(u, "cache_write_tokens")) for u in usages), 8)
