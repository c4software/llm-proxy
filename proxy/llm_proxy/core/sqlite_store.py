"""
Une base SQLite de magasin (comptes, mémoire, conversations) : UNE
connexion par processus, ouverte à la première opération, schéma et
migrations posés une seule fois — au lieu d'ouvrir, de rejouer les PRAGMA
et le DDL, puis de fermer à chaque opération. Les opérations sont
sérialisées par un verrou (appelées depuis asyncio.to_thread). Le chemin
est relu à chaque opération : un test qui remplace DB_PATH ouvre SA base.
"""

import sqlite3
import threading


class Store:
    def __init__(self, path, setup, row_factory=None, foreign_keys=False,
                 before=()):
        """`path()` : le chemin de la base ; `setup(conn)` : schéma et
        migrations, idempotents, joués une fois par connexion ; `before` :
        des PRAGMA à poser avant le passage en WAL (auto_vacuum, qui n'a
        d'effet qu'à la création de la base)."""
        self._path = path
        self._setup = setup
        self._row_factory = row_factory
        self._foreign_keys = foreign_keys
        self._before = tuple(before)
        self._conn: sqlite3.Connection | None = None
        self._opened_for: str | None = None
        # Réentrant : un appelant peut tenir le verrou autour de run().
        self.lock = threading.RLock()

    def _connection(self) -> sqlite3.Connection:
        path = self._path()
        if self._conn is None or self._opened_for != path:
            self._close()
            conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
            if self._row_factory is not None:
                conn.row_factory = self._row_factory
            for pragma in self._before:
                conn.execute(pragma)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            if self._foreign_keys:
                conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                self._setup(conn)
            self._conn, self._opened_for = conn, path
        return self._conn

    def run(self, fn):
        """fn(conn) sous le verrou, dans une transaction."""
        with self.lock:
            conn = self._connection()
            with conn:
                return fn(conn)

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = self._opened_for = None

    def close(self) -> None:
        with self.lock:
            self._close()
