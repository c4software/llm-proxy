"""stats.py : le robinet identité et la base — sur un fichier SQLite
temporaire, sans serveur."""

import json
import time

import pytest

from llm_proxy import stats


def test_usage_collector_reads_cached_tokens_from_sse():
    c = stats.UsageCollector("text/event-stream")
    chunk = (b'data: {"choices":[{"delta":{"content":"Bonjour"}}]}\n\n'
             b'data: {"choices":[],"usage":{"prompt_tokens":20000,'
             b'"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":18500}}}\n\n'
             b'data: [DONE]\n\n')
    assert c.feed(chunk) == chunk       # identité
    assert c.finish() == b""
    assert c.tokens(0) == (20000, 3, True)
    assert c.cached() == 18500


def test_usage_collector_json_without_details():
    c = stats.UsageCollector("application/json")
    c.feed(json.dumps({"choices": [], "usage": {"prompt_tokens": 7,
                                                 "completion_tokens": 2}}).encode())
    c.finish()
    assert c.tokens(0) == (7, 2, True)
    assert c.cached() == 0


def test_cached_tokens_helper():
    assert stats.cached_tokens(None) == 0
    assert stats.cached_tokens({"prompt_tokens_details": {"cached_tokens": -3}}) == 0
    assert stats.cached_tokens({"prompt_tokens_details": {"cached_tokens": 12}}) == 12


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "DB_PATH", str(tmp_path / "stats.db"))
    stats.init()
    yield
    stats.close()


def test_migration_adds_cached_column_to_old_base(tmp_path, monkeypatch):
    """Une base d'avant la colonne cached_tokens est complétée au
    démarrage — aucune migration à jouer à la main."""
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(stats.SCHEMA.replace(
        ",\n  cached_tokens     INTEGER NOT NULL DEFAULT 0", ""))
    conn.execute("INSERT INTO requests VALUES (NULL, ?, 'a/m', 'a', 'm', "
                 "'/v1/chat/completions', 200, 1.0, 10, 2, 1, 0)", (time.time(),))
    conn.commit(); conn.close()
    monkeypatch.setattr(stats, "DB_PATH", str(path))
    stats.init()
    try:
        with stats._reader() as r:
            cols = {row[1] for row in r.execute("PRAGMA table_info(requests)")}
            assert "cached_tokens" in cols
            assert r.execute("SELECT cached_tokens FROM requests").fetchone()[0] == 0
    finally:
        stats.close()


def test_usage_api_exposes_input_cached_tokens(db):
    stats.record("a/m", "a", "m", "/v1/messages", 200, 0.5, 20000, 5, True,
                 True, cached_tokens=18000)
    stats.record("a/m", "a", "m", "/v1/messages", 200, 0.5, 20000, 5, True,
                 True, cached_tokens=0)
    stats.close()  # vide la file d'écriture
    stats.init()
    # end_time dans le futur : la ligne vient d'être écrite, à la seconde
    # près elle serait sinon hors de la plage.
    page = stats.usage_completions(start_time=0, end_time=int(time.time()) + 60,
                                   bucket_width="all", group_by=["model"])
    r = page["data"][0]["results"][0]
    assert r["model"] == "a/m"
    assert r["input_tokens"] == 40000
    assert r["input_cached_tokens"] == 18000
    assert r["num_anthropic_requests"] == 2
