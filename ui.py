"""
Tableau de bord HTML du proxy. Ce module ne contient AUCUN balisage : la
présentation vit dans templates/ (Jinja2) et static/ (CSS, JS, HTMX) ; ici
on ne fait que préparer les valeurs et décider ce qu'il faut envoyer.

Les chiffres viennent de stats.snapshot() — la même source que
GET /v1/stats, donc rien à resynchroniser.

Rafraîchissement DIFFÉRENTIEL, et non « re-rendre la page toutes les 5 s » :
  - le sondage envoie la révision déjà affichée (stats.revision()) ;
  - rien n'a bougé → 204 No Content, HTMX ne touche pas au DOM ;
  - sinon la réponse ne contient QUE DES VALEURS : chaque nombre affiché
    est un <span> identifié, et seuls ceux des totaux et des modèles dont
    les compteurs ont bougé sont réécrits, en swaps « out of band ».
    Aucune carte, aucune ligne, aucune structure n'est redessinée ;
  - seule une STRUCTURE nouvelle (premier modèle vu, modèle qui apparaît)
    insère du DOM, et uniquement sa ligne ;
  - un delta impossible (premier chargement, proxy redémarré) réémet le
    tableau complet.
Les horodatages relatifs (« il y a 3 min ») sont calculés dans le
navigateur à partir d'un timestamp : le simple passage du temps ne
provoque donc ni requête ni redessin.
"""

import hashlib
import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

import stats

# Intervalle de sondage (secondes). Un tick sans changement coûte une
# requête vide et un 204, rien de plus.
REFRESH_SECONDS = 5

# Colonnes du tableau et valeurs d'une ligne (l'ordre des `cells` suit
# celui des colonnes, « flux » mis à part : il vit sous le nom du modèle).
COLUMNS = ("Modèle", "Requêtes", "Erreurs", "Entrée", "Sortie", "Total",
           "Moy./req", "p95", "Comptage", "Dernière")
CELLS = ("req", "err", "in", "out", "tot", "avg", "p95", "acc", "ago",
         "flux")

# Cartes de synthèse : (id du <span>, libellé). Chacune a aussi son
# «<id>-hint» sous le chiffre.
CARDS = (
    ("c-req", "Requêtes"),
    ("c-tok", "Tokens"),
    ("c-mod", "Modèles actifs"),
    ("c-err", "Erreurs"),
)

# Couleurs des modèles (pastille + segment de répartition), tons chauds
# lisibles sur les deux thèmes, cyclées au-delà de six modèles.
SEGMENT_COLORS = ("#d97757", "#e0a458", "#7d9b76", "#6b8fa3", "#a37ba0",
                  "#c08552")

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "templates")


def _n(value) -> str:
    """Entier à la française : espace comme séparateur de milliers."""
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _ms(seconds) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "—"
    return f"{seconds:.1f} s" if seconds >= 1 else f"{int(seconds * 1000)} ms"


def _epoch(ts) -> str:
    """Timestamp brut pour le navigateur, qui en tire « il y a 3 min »."""
    try:
        return f"{float(ts):.0f}"
    except (TypeError, ValueError):
        return "0"


env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters["n"] = _n
env.filters["ms"] = _ms
env.filters["epoch"] = _epoch


def _row_id(key: str) -> str:
    """Identifiant DOM stable pour un modèle (les id contiennent « / »,
    des points…) : préfixe + empreinte courte."""
    return "row-" + hashlib.md5(key.encode()).hexdigest()[:10]


def _prepare(snap: dict) -> list[dict]:
    """Ajoute à chaque modèle ce dont la présentation a besoin et qui ne
    change jamais : son identifiant DOM et sa couleur."""
    models = []
    for index, model in enumerate(snap["data"]):
        model = dict(model)
        model["row_id"] = _row_id(model["id"])
        model["color"] = SEGMENT_COLORS[index % len(SEGMENT_COLORS)]
        models.append(model)
    return models


def _totals(snap: dict) -> dict:
    """Valeurs des cartes de synthèse."""
    totals = dict(snap["totals"])
    totals["success_rate"] = (
        f"{100 * totals['requests_ok'] / totals['requests']:.1f}%"
        if totals["requests"] else "—"
    )
    totals["backends"] = " · ".join(
        f"{name} : {_n(b['requests'])} req"
        for name, b in snap["backends"].items()
    ) or "aucun trafic"
    totals["since"] = snap["since"]
    return totals


def _segments(models: list[dict], total_tokens: int) -> list[dict]:
    """Largeur et pourcentage de chaque part de la répartition — des
    valeurs, elles aussi : la barre n'est pas reconstruite."""
    if not total_tokens:
        return []
    segments = []
    for model in models:
        tokens = model["usage"]["total_tokens"]
        pct = 100 * tokens / total_tokens
        segments.append({
            "row_id": model["row_id"],
            "color": model["color"],
            "pct": f"{pct:.2f}",
            "label": f"{pct:.0f}%",
            "title": f"{model['id']} — {_n(tokens)} tokens",
        })
    return segments


def page(refresh: int = REFRESH_SECONDS) -> str:
    return env.get_template("page.html").render(
        refresh=refresh, cards=CARDS, columns=COLUMNS,
    )


def _update(snap: dict, updated_ids: list[str], new_ids: list[str],
            structure: bool, rows_oob: str) -> str:
    models = _prepare(snap)
    by_id = {m["id"]: m for m in models}
    totals = _totals(snap)
    return env.get_template("update.html").render(
        cards=CARDS,
        cells=CELLS,
        columns=COLUMNS,
        totals=totals,
        models=models,
        updated=[by_id[k] for k in updated_ids if k in by_id],
        new_rows=[by_id[k] for k in new_ids if k in by_id],
        structure=structure,
        rows_oob=rows_oob,
        segments=_segments(models, totals["total_tokens"]),
        revision=snap["revision"],
    )


def render(since_rev: int) -> tuple[str, int]:
    """(corps HTML, statut). 204 = rien n'a changé, le DOM n'est pas touché."""
    change = stats.delta_since(since_rev)
    if change is None:
        return "", 204

    snap = stats.snapshot()
    force_full, changed = change
    if force_full:
        # Premier chargement ou proxy redémarré : le client n'a rien.
        return _update(
            snap, updated_ids=[], new_ids=[m["id"] for m in snap["data"]],
            structure=True, rows_oob="innerHTML",
        ), 200

    new_ids, updated_ids = [], []
    for key in changed:
        model = next((m for m in snap["data"] if m["id"] == key), None)
        if model is None:
            continue
        (new_ids if model["created_revision"] > since_rev
         else updated_ids).append(key)
    return _update(
        snap, updated_ids=updated_ids, new_ids=new_ids,
        structure=bool(new_ids),
        # Premier modèle jamais vu : l'état vide doit céder la place ;
        # sinon la nouvelle ligne s'ajoute à la suite.
        rows_oob=("innerHTML" if len(new_ids) == len(snap["data"])
                  else "beforeend"),
    ), 200
