"""Sauvegarde et restauration de data/ (backup.py) : les bases vivantes en
WAL, copiées par l'API de SQLite — y compris ce qui n'est encore que dans
le journal —, l'archive vérifiée, restaurée ailleurs et relue."""

import io
import os
import sqlite3
import tarfile

import pytest

from llm_proxy import accounts, backup, config, conversations, memories, stats, tools


@pytest.fixture
def data(tmp_path, monkeypatch):
    """Un data/ temporaire : config, secret, et trois bases en WAL."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "config.toml").write_text('[proxy]\nlog_level = "INFO"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", str(d / "config.toml"))
    monkeypatch.setattr(tools, "SECRET_PATH", str(d / "client.secret"))
    monkeypatch.setattr(tools, "_secret", None)
    tools.owner("init")                         # crée le secret
    monkeypatch.setattr(memories, "DB_PATH", str(d / "memory.db"))
    monkeypatch.setattr(accounts, "DB_PATH", str(d / "accounts.db"))
    monkeypatch.setattr(accounts, "_cache", {})
    monkeypatch.setattr(stats, "DB_PATH", str(d / "stats.db"))
    monkeypatch.setattr(conversations, "DB_PATH", str(d / "conversations.db"))
    conversations.save("cle", "c1", {"id": "c1", "title": "à garder", "messages": []})
    memories.write("cle", "/memories/note.md", "à garder")
    accounts.login("https://idp", "alice", "a@x")
    conn = sqlite3.connect(stats.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(stats.SCHEMA)
    conn.execute(stats.INSERT, (1.0, "a/m", "a", "m", "/v1/chat/completions",
                                200, 1.0, 10, 2, 1, 0, 0, ""))
    conn.commit()
    yield d, conn                    # la connexion reste ouverte : WAL vivant
    conn.close()


def test_backup_check_restore(data, tmp_path):
    d, live = data
    # Une écriture encore dans le journal (-wal), pas dans stats.db : une
    # copie de fichier la perdrait.
    live.execute(stats.INSERT, (2.0, "a/m", "a", "m", "/v1/chat/completions",
                                200, 1.0, 5, 1, 1, 0, 0, ""))
    live.commit()
    assert os.path.getsize(str(d / "stats.db") + "-wal") > 0

    archive = backup.backup(str(tmp_path / "backups"))
    assert oct(os.stat(archive).st_mode & 0o777) == "0o600"
    with tarfile.open(archive) as tar:
        assert sorted(tar.getnames()) == ["accounts.db", "client.secret",
                                         "config.toml", "conversations.db",
                                         "memory.db", "stats.db"]
    assert backup.check(archive)

    target = tmp_path / "restored"
    target.mkdir()
    (target / "stats.db").write_text("ancienne")      # mise de côté
    backup.restore(archive, str(target))
    assert sqlite3.connect(target / "stats.db").execute(
        "SELECT COUNT(*) FROM requests").fetchone()[0] == 2
    assert sqlite3.connect(target / "memory.db").execute(
        "SELECT content FROM files").fetchone()[0] == "à garder"
    assert sqlite3.connect(target / "accounts.db").execute(
        "SELECT sub FROM users").fetchone()[0] == "alice"
    assert (target / "client.secret").read_bytes() == \
        (d / "client.secret").read_bytes()
    aside = [p for p in target.iterdir() if p.name.startswith("pre-restore-")]
    assert (aside[0] / "stats.db").read_text() == "ancienne"


def test_only_the_last_archives_are_kept(data, tmp_path, monkeypatch):
    dest = tmp_path / "backups"
    stamps = iter(["20261001-000000", "20261002-000000", "20261003-000000"])
    monkeypatch.setattr(backup.time, "strftime", lambda fmt: next(stamps))
    for _ in range(3):
        backup.backup(str(dest), keep=2)
    assert sorted(os.listdir(dest)) == ["llm-proxy-20261002-000000.tar.gz",
                                        "llm-proxy-20261003-000000.tar.gz"]


def test_a_damaged_archive_is_not_restored(data, tmp_path):
    archive = backup.backup(str(tmp_path / "backups"))
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(bad, "w:gz") as dst:
        for member in src.getmembers():
            payload = src.extractfile(member).read()
            if member.name == "stats.db":
                payload = payload[:100] + b"\0" * (len(payload) - 100)
            member.size = len(payload)
            dst.addfile(member, io.BytesIO(payload))
    assert not backup.check(str(bad))
    with pytest.raises(SystemExit):
        backup.restore(str(bad), str(tmp_path / "ailleurs"))
