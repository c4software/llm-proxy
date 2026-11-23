"""
Ce que partagent les magasins PAR COMPTE rangés dans conversations.db, à
côté de l'historique et sauvegardés avec lui : les webhooks (webhooks.py),
les sources de dépôts (repos.py) et les backends (backends.py). Chacun a
ses tables ; les règles communes sont ici, pour qu'un nom, une adresse ou
une borne se jugent de la même façon partout.

  OwnedError            une opération refusée (le message est pour la page,
                        `status` le code HTTP), une HistoryError
  runner(setup)         run(fn) : fn(conn) sous le verrou de la base, après
                        setup(conn) (le schéma, idempotent)
  check_id(v, what)     un identifiant de la base (sinon « identifiant de
                        <what> invalide »)
  body(v)               le corps JSON d'une requête, un objet
  name(v)               un nom affiché, espaces réduits, borné, non vide
  http_url(v)           une adresse http(s), bornée
  taken(c, table, …)    un nom déjà pris par une AUTRE ligne du compte, sans
                        casse : les outils cherchent sans casse
  room(c, table, …)     la borne de lignes par compte (413)
  seconds(ts)           une date pour la page (lib/format.js, stamp)
  add_columns(c, …)     les colonnes qu'une base d'avant n'a pas encore
  once(c, name)         vrai la PREMIÈRE fois qu'un nettoyage de cette base
                        est demandé (table `migrations`), faux ensuite
"""

from urllib.parse import urlsplit

from . import conversations

MAX_NAME = 80
MAX_URL = 2000


class OwnedError(conversations.HistoryError):
    """Une opération refusée ; le message est pour la page."""


def runner(setup):
    def run(fn):
        def q(c):
            setup(c)
            return fn(c)
        return conversations._run(q)
    return run


def check_id(value, what: str) -> str:
    if not isinstance(value, str) or not conversations._ID.match(value):
        raise OwnedError(f"identifiant de {what} invalide")
    return value


def body(value) -> dict:
    if not isinstance(value, dict):
        raise OwnedError("corps JSON attendu")
    return value


def name(value) -> str:
    text = " ".join(str(value or "").split())[:MAX_NAME]
    if not text:
        raise OwnedError("un nom est attendu")
    return text


def http_url(value, what: str = "une URL") -> str:
    url = str(value or "").strip()
    if len(url) > MAX_URL or not url.lower().startswith(("https://", "http://")):
        raise OwnedError(f"{what} http(s) est attendue")
    try:
        parts = urlsplit(url)
        parts.port                        # lève ValueError sur un port illisible
    except ValueError:
        raise OwnedError(f"{what} lisible est attendue (hôte, port)")
    if not parts.hostname:
        raise OwnedError(f"{what} avec un hôte est attendue")
    return url


def taken(c, table: str, owner: str, wanted: str, but: str = "") -> bool:
    return any(n.casefold() == wanted.casefold() for n, in c.execute(
        f"SELECT name FROM {table} WHERE owner = ? AND id != ?", (owner, but)))


def room(c, table: str, owner: str, limit: int, label: str) -> None:
    n, = c.execute(f"SELECT count(*) FROM {table} WHERE owner = ?", (owner,)).fetchone()
    if n >= limit:
        raise OwnedError(f"{limit} {label} au plus", 413)


def seconds(ts):
    return round(ts) if ts else None


_migrated: set[tuple[str, str]] = set()


def add_columns(c, table: str, columns: dict[str, str]) -> None:
    """Une fois par base et par table : les colonnes `columns` (nom →
    définition SQL) qu'elle n'a pas encore. Toutes celles d'une table en UN
    appel : un second appel pour la même table serait sans effet."""
    if (conversations.DB_PATH, table) in _migrated:
        return
    have = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
    for column, kind in columns.items():
        if column not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    _migrated.add((conversations.DB_PATH, table))


def once(c, name: str) -> bool:
    """Un nettoyage de la base, joué une seule fois : vrai s'il ne l'a
    jamais été (et le note), faux sinon. À appeler dans la transaction du
    nettoyage : s'il échoue, rien n'est noté."""
    c.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, at REAL NOT NULL)")
    if c.execute("SELECT 1 FROM migrations WHERE name = ?", (name,)).fetchone():
        return False
    c.execute("INSERT INTO migrations (name, at) VALUES (?, strftime('%s', 'now'))", (name,))
    return True
