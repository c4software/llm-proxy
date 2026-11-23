"""
La forme des règles `limits` d'un backend à quotas, du TOML
([[backends.<nom>.limits]]) comme d'un backend de compte
(storage/backends.py) : vérifiée ici, appliquée par upstream/quotas.py.
"""

# Les quatre plafonds d'une règle ; 0 = illimité.
KINDS = ("rpm", "tpm", "rpd", "tpd")
MAX_RULES = 32
MAX_MODELS = 20


def rules(value) -> list[dict]:
    """Les règles `limits` d'un backend, vérifiées : une liste de
    {models: [motifs], rpm?, tpm?, rpd?, tpd?} (motifs à jokers shell, casse
    ignorée, sur le nom sans préfixe ; plafonds entiers ≥ 0, absents = 0 =
    illimité). Lève ValueError, au message lisible."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > MAX_RULES:
        raise ValueError(f"`limits` : une liste de {MAX_RULES} règles au plus")
    out = []
    for i, rule in enumerate(value, 1):
        if not isinstance(rule, dict):
            raise ValueError(f"`limits`, règle {i} : une table attendue")
        unknown = set(rule) - {"models", *KINDS}
        if unknown:
            raise ValueError(f"`limits`, règle {i} : champ inconnu {sorted(unknown)[0]!r}")
        models = rule.get("models")
        if isinstance(models, str):
            models = [models]
        if not isinstance(models, list) or not 0 < len(models) <= MAX_MODELS or not all(
                isinstance(m, str) and m.strip() and len(m) <= 200 for m in models):
            raise ValueError(f"`limits`, règle {i} : `models`, 1 à {MAX_MODELS} motifs de noms")
        caps = {}
        for kind in KINDS:
            v = rule.get(kind, 0)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"`limits`, règle {i} : `{kind}`, un entier ≥ 0")
            caps[kind] = v
        if not any(caps.values()):
            raise ValueError(f"`limits`, règle {i} : au moins un plafond")
        out.append({"models": [m.strip().lower() for m in models], **caps})
    return out
