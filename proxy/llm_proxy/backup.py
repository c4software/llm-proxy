"""
Sauvegarde et restauration de data/ — ce qu'on ne peut pas reconstruire :

  stats.db, memory.db, accounts.db,  les bases SQLite (statistiques,
  conversations.db                   mémoire persistante, comptes et clés,
                                     historique du chat de la page)
  client.secret                      le secret des condensés : le perdre,
                                     c'est perdre toutes les mémoires, les
                                     clés gérées et les sessions
  config.toml                        la configuration (ses secrets restent
                                     dans le .env, à sauvegarder à part)

Les bases sont en WAL : une copie de fichier pendant que le proxy écrit
n'est pas fiable. Elles sont copiées par l'API de sauvegarde de SQLite
(sqlite3.Connection.backup), cohérente à chaud. Le tout part dans UNE
archive datée (llm-proxy-AAAAMMJJ-HHMMSS.tar.gz, mode 600 : elle contient
le secret), dont les N plus récentes sont gardées.

    python -m llm_proxy.backup [dossier] [--keep 14]   sauvegarder
    python -m llm_proxy.backup --check <archive>        vérifier
    python -m llm_proxy.backup --restore <archive> [--to <data>]

--check rouvre chaque base de l'archive (PRAGMA integrity_check, nombre de
lignes), relit le TOML et la taille du secret : c'est le test de
restauration, sans rien toucher. --restore, proxy ARRÊTÉ, remet les
fichiers dans data/ ; ceux qui y étaient sont d'abord déplacés dans
data/pre-restore-<date>/, rien n'est écrasé.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import tomllib

from . import accounts, config, conversations, memories, stats, tools

PREFIX = "llm-proxy-"


def sources() -> dict[str, str]:
    """Nom dans l'archive → chemin sur le disque."""
    return {"stats.db": stats.DB_PATH, "memory.db": memories.DB_PATH,
            "accounts.db": accounts.DB_PATH,
            "conversations.db": conversations.DB_PATH,
            "client.secret": tools.SECRET_PATH,
            "config.toml": config.CONFIG_PATH}


def _copy_db(src: str, dst: str) -> None:
    """Copie cohérente d'une base vivante (API de sauvegarde de SQLite)."""
    # Pas en lecture seule : une base WAL sans -shm ne s'ouvrirait pas.
    # L'API de sauvegarde ne fait que lire.
    source = sqlite3.connect(src, timeout=30)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def backup(dest: str, keep: int = 14) -> str:
    """Écrit une archive dans `dest` ; garde les `keep` plus récentes (0 =
    toutes). Rend son chemin. Un fichier absent (pas de mémoire, pas de
    comptes) est sauté et dit."""
    os.makedirs(dest, exist_ok=True)
    name = PREFIX + time.strftime("%Y%m%d-%H%M%S") + ".tar.gz"
    path = os.path.join(dest, name)
    with tempfile.TemporaryDirectory() as tmp:
        kept = []
        for arc, src in sources().items():
            if not os.path.exists(src):
                print(f"  absent, sauté : {arc} ({src})")
                continue
            dst = os.path.join(tmp, arc)
            if arc.endswith(".db"):
                _copy_db(src, dst)
            else:
                shutil.copyfile(src, dst)
            kept.append(arc)
        fd = os.open(path + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh,
                                                     mode="w:gz") as tar:
            for arc in kept:
                tar.add(os.path.join(tmp, arc), arcname=arc)
        os.replace(path + ".part", path)
    print(f"sauvegarde : {path} ({', '.join(kept)}, "
          f"{os.path.getsize(path) // 1024} Ko)")
    if keep:
        old = sorted(f for f in os.listdir(dest)
                     if f.startswith(PREFIX) and f.endswith(".tar.gz"))
        for f in old[:-keep]:
            os.remove(os.path.join(dest, f))
            print(f"  ancienne retirée : {f}")
    return path


def _extract(archive: str, into: str) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        unknown = set(names) - set(sources())
        if unknown:
            raise SystemExit(f"{archive} : contenu inattendu {sorted(unknown)}")
        tar.extractall(into, filter="data")
    return names


def check(archive: str) -> bool:
    """Rouvre tout ce que contient l'archive ; dit ce qu'il y trouve. Vrai
    si tout est lisible."""
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for name in _extract(archive, tmp):
            path = os.path.join(tmp, name)
            if name.endswith(".db"):
                conn = sqlite3.connect(path)
                try:
                    verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    tables = [r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                    counts = ", ".join(
                        f"{t} {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}"
                        for t in tables)
                except sqlite3.DatabaseError as exc:
                    verdict, counts = f"ILLISIBLE ({exc})", ""
                finally:
                    conn.close()
                ok &= verdict == "ok"
                print(f"  {name} : {verdict} — {counts or 'vide'}")
            elif name == "config.toml":
                try:
                    with open(path, "rb") as fh:
                        tomllib.load(fh)
                    print("  config.toml : TOML valide")
                except tomllib.TOMLDecodeError as exc:
                    ok = False
                    print(f"  config.toml : INVALIDE — {exc}")
            elif name == "client.secret":
                size = os.path.getsize(path)
                ok &= size >= 16
                print(f"  client.secret : {size} octets"
                      + ("" if size >= 16 else " — TROP COURT"))
    print(f"{archive} : {'restaurable' if ok else 'ABÎMÉE'}")
    return ok


def restore(archive: str, data: str | None = None) -> None:
    """Remet le contenu de l'archive (proxy arrêté) : à leurs chemins
    configurés, ou dans `data` s'il est donné. Ce qui y était part d'abord
    dans <data>/pre-restore-<date>/."""
    if not check(archive):
        raise SystemExit("archive abîmée : rien n'est restauré")
    home = data or os.path.dirname(os.path.abspath(config.CONFIG_PATH))
    aside = os.path.join(home, "pre-restore-" + time.strftime("%Y%m%d-%H%M%S"))
    with tempfile.TemporaryDirectory() as tmp:
        for name in _extract(archive, tmp):
            target = os.path.join(data, name) if data else sources()[name]
            for extra in ("", "-wal", "-shm") if name.endswith(".db") else ("",):
                if os.path.exists(target + extra):
                    os.makedirs(aside, exist_ok=True)
                    shutil.move(target + extra, os.path.join(aside, name + extra))
            shutil.copyfile(os.path.join(tmp, name), target)
            if name == "client.secret":
                os.chmod(target, 0o600)
            print(f"  restauré : {target}")
    if os.path.isdir(aside):
        print(f"les fichiers remplacés sont dans {aside}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m llm_proxy.backup",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("dest", nargs="?",
                   default=os.path.join(os.path.dirname(
                       os.path.abspath(config.CONFIG_PATH)), "backups"),
                   help="dossier des archives (défaut : data/backups)")
    p.add_argument("--keep", type=int, default=14,
                   help="archives gardées (défaut 14 ; 0 = toutes)")
    p.add_argument("--check", metavar="ARCHIVE", help="vérifier une archive")
    p.add_argument("--restore", metavar="ARCHIVE",
                   help="restaurer une archive (proxy arrêté)")
    p.add_argument("--to", metavar="DATA", help="dossier où restaurer "
                   "(défaut : le chemin configuré de chaque fichier)")
    args = p.parse_args(argv)
    if args.check:
        return 0 if check(args.check) else 1
    if args.restore:
        restore(args.restore, args.to)
        return 0
    backup(args.dest, args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
