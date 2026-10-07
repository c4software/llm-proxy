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


# ── exécutions d'outils hébergés (table tool_calls) ─────────────────────

def flushed():
    """Vide la file d'écriture (arrêt puis reprise de l'écrivain) et rend
    les lignes de tool_calls, sans id ni horodatage."""
    stats.close()
    stats.init()
    with stats._reader() as r:
        return r.execute("SELECT tool, endpoint, model_key, outcome, duration, "
                         "result_chars FROM tool_calls ORDER BY id").fetchall()


def test_tool_run_recorded_from_each_of_the_three_paths(proxy, db):
    """Une exécution par /v1/responses, /v1/messages et /v1/tools : une
    ligne chacune, avec SA route et le modèle préfixé de la conversation
    (aucun pour l'appel direct) — et rien de ce qui a été cherché ou lu."""
    from fakes import (ANSWER_TURN, FOUND, QUERY, SEARCH_TURN, FakeUpstream,
                       stream)
    turns = lambda: [FakeUpstream(stream(*SEARCH_TURN)),
                     FakeUpstream(stream(*ANSWER_TURN))]
    proxy.replies = turns()
    assert proxy.client.post("/v1/responses", json={
        "model": "essai/qwen", "input": "x", "stream": True,
        "tools": [{"type": "web_search"}]}).status_code == 200
    proxy.replies = turns()
    assert proxy.client.post("/v1/messages", json={
        "model": "essai/qwen", "max_tokens": 64, "stream": True,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }).status_code == 200
    r = proxy.client.post("/v1/tools/web_fetch",
                          json={"url": "https://secret.example/page"})
    assert r.json()["result"] == FOUND
    assert len(proxy.hosted.runs) == 3

    rows = flushed()
    assert [row[:4] for row in rows] == [
        ("web_search", "/v1/responses", "essai/qwen", "ok"),
        ("web_search", "/v1/messages", "essai/qwen", "ok"),
        ("web_fetch", "/v1/tools", "", "ok")]
    assert all(row[4] >= 0 and row[5] == len(FOUND) for row in rows)
    # Une réponse à outils garde UNE ligne de requête ; l'appel direct n'en a pas.
    assert [line[3] for line in proxy.lines] == ["/v1/responses", "/v1/messages"]
    # Le contenu : nulle part dans la base, quelle que soit la table.
    with stats._reader() as conn:
        dump = "\n".join(conn.iterdump())
    for secret in ("llama.cpp", "secret.example", json.loads(QUERY)["query"],
                   "Releases"):
        assert secret not in dump, secret


def test_tool_run_outcome_ok_error_limit(db):
    """Succès, échec (le résultat porte un code d'erreur, quelle qu'en
    soit la cause — et pas son texte : un succès peut commencer par
    «Error:») et refus par limite d'appels sont trois issues distinctes ;
    un refus n'a pas de durée. Un nom
    inconnu ne laisse pas de ligne : ce n'est pas un outil."""
    import asyncio
    from fakes import outil
    from llm_proxy import tools

    async def run(args):
        if args.get("casse"):
            raise RuntimeError("secret")
        if args.get("refuse"):
            raise tools.ToolError("unavailable", "moteur éteint.")
        return args.get("rend", "ok")

    h = tools.Hosted([outil("echo", run)], tools.Memory(4, 60))
    go = lambda *a, **kw: asyncio.run(h.run(*a, endpoint="/v1/tools", **kw))
    go("echo", "{}", 0)
    go("echo", '{"refuse": true}', 0)
    go("echo", '{"casse": true}', 0)
    go("echo", "pas du json", 0)
    go("echo", "{}", tools.MAX_CALLS)
    go("echo", "{}", 1, limit=1)
    go("rm_rf", "{}", 0)
    go("echo", '{"rend": "Error: une page qui commence ainsi"}', 0)
    rows = flushed()
    assert [(row[0], row[3]) for row in rows] == [
        ("echo", "ok"), ("echo", "error"), ("echo", "error"), ("echo", "error"),
        ("echo", "limit"), ("echo", "limit"), ("echo", "ok")]
    assert [row[4] for row in rows[4:6]] == [0.0] * 2
    assert rows[0][5] == 2 and rows[1][5] == len("Error: moteur éteint.")


def test_tool_run_never_raises_when_stats_fail(db, monkeypatch):
    """Une panne des statistiques ne casse ni l'outil ni la réponse ; et
    sans écrivain (stats.init() jamais appelé) rien n'est mis en file."""
    import asyncio
    from fakes import outil
    from llm_proxy import tools

    async def run(args):
        return "résultat"

    h = tools.Hosted([outil("echo", run)], tools.Memory(4, 60))

    def panne(*a):
        raise RuntimeError("disque plein")
    with monkeypatch.context() as m:
        m.setattr(stats, "record_tool", panne)
        assert asyncio.run(h.run("echo", "{}", 0)).text == "résultat"
    stats.close()
    assert asyncio.run(h.run("echo", "{}", 0)).text == "résultat"
    assert stats._pending.empty()
    stats.init()
    assert flushed() == []


def test_usage_tools_route_buckets_group_by_window_and_pagination(proxy, db):
    """La route sœur de l'Usage API : même page → bucket → result, mêmes
    paramètres, même pagination et mêmes validations que /completions ;
    group_by sur l'outil, la route d'appel et le modèle."""
    day = 86_400
    t0 = 1_700_000_000 // day * day          # début d'un seau d'un jour
    rows = [  # (ts, outil, route, modèle, issue, durée, caractères)
        (t0 + 10, "web_search", "/v1/responses", "a/m", "ok", 1.0, 100),
        (t0 + 20, "web_search", "/v1/responses", "a/m", "error", 3.0, 30),
        (t0 + 30, "web_search", "/v1/responses", "a/m", "limit", 0.0, 80),
        (t0 + 40, "web_search", "/v1/tools", "", "ok", 2.0, 50),
        (t0 + day + 5, "web_fetch", "/v1/messages", "b/n", "ok", 0.5, 7),
        (t0 + 3 * day, "web_fetch", "/v1/tools", "", "ok", 9.0, 9),  # hors fenêtre
    ]
    with stats._reader() as conn:
        conn.executemany(stats.INSERT_TOOL, rows)
        conn.commit()

    def get(path="/v1/organization/usage/tools", **params):
        return proxy.client.get(path, params={"start_time": t0,
                                              "end_time": t0 + 2 * day, **params})

    # Sans group_by : un résultat par seau, dimensions nulles.
    page = get().json()
    assert {k: page[k] for k in ("object", "has_more", "next_page")} == {
        "object": "page", "has_more": False, "next_page": None}
    one, two = page["data"]
    assert (one["object"], one["start_time"], one["end_time"]) == (
        "bucket", t0, t0 + day)
    assert one["results"] == [{
        "object": "organization.usage.tools.result", "num_requests": 4,
        "project_id": None, "user_id": None, "api_key_id": None,
        "model": None, "tool": None, "endpoint": None,
        "num_errors": 1, "num_limited": 1,
        # La moyenne porte sur les 3 appels lancés, pas sur le refus.
        "total_duration_seconds": 6.0, "avg_duration_seconds": 2.0,
        "max_duration_seconds": 3.0, "result_chars": 260,
        "first_request_time": t0 + 10, "last_request_time": t0 + 40}]
    assert two["results"][0]["num_requests"] == 1

    # group_by, sous ses trois écritures ; l'appel direct n'a pas de modèle.
    for params in ({"group_by": ["tool", "endpoint", "model"]},
                   {"group_by[]": ["tool", "endpoint", "model"]},
                   {"group_by": "tool,endpoint,model"}):
        data = get(**params).json()["data"]
        assert [[(r["tool"], r["endpoint"], r["model"], r["num_requests"])
                 for r in b["results"]] for b in data] == [
            [("web_search", "/v1/responses", "a/m", 3),
             ("web_search", "/v1/tools", None, 1)],
            [("web_fetch", "/v1/messages", "b/n", 1)]], params

    # Filtre `models`, seau unique (extension du proxy), page suivante.
    data = get(models="b/n", bucket_width="all", group_by="tool").json()["data"]
    assert [(r["tool"], r["num_requests"]) for b in data for r in b["results"]] \
        == [("web_fetch", 1)]
    first = get(limit=1).json()
    assert (len(first["data"]), first["has_more"], first["next_page"]) == (1, True, "1")
    rest = get(limit=1, page=first["next_page"]).json()
    assert rest["data"] == [two] and rest["has_more"] is False

    # Mêmes validations que /completions, et la même chose sous /ui.
    assert get("/ui/usage/tools").json() == page
    for params, param in (({"group_by": "batch"}, "group_by"),
                          ({"bucket_width": "1w"}, "bucket_width"),
                          ({"limit": "x"}, "limit")):
        r = get(**params)
        assert r.status_code == 400 and r.json()["error"]["param"] == param
    assert proxy.client.get("/v1/organization/usage/tools").status_code == 400
    assert get(project_ids="p").json()["data"] == []
    # /completions ne connaît pas les dimensions des outils, et n'en voit rien.
    assert get("/v1/organization/usage/completions",
               group_by="tool").status_code == 400
    assert all(b["results"] == [] for b in get(
        "/v1/organization/usage/completions").json()["data"])


def test_migration_adds_tool_calls_to_a_base_with_requests_only(tmp_path, monkeypatch):
    """Une base d'avant les outils (la seule table `requests`, peuplée)
    reçoit `tool_calls` au démarrage, sans rien perdre."""
    import sqlite3
    path = tmp_path / "prod.db"
    conn = sqlite3.connect(path)
    conn.executescript(stats.SCHEMA)
    conn.execute(stats.INSERT, (time.time(), "a/m", "a", "m",
                                "/v1/chat/completions", 200, 1.0, 10, 2, 1, 0, 0))
    conn.commit(); conn.close()
    monkeypatch.setattr(stats, "DB_PATH", str(path))
    stats.init()
    try:
        stats.record_tool("web_search", "/v1/tools", "", "ok", 0.2, 12)
        assert [row[:4] for row in flushed()] == [
            ("web_search", "/v1/tools", "", "ok")]
        with stats._reader() as r:
            assert r.execute("SELECT model_key, prompt_tokens FROM requests"
                             ).fetchall() == [("a/m", 10)]
    finally:
        stats.close()


def test_purge_drops_old_tool_calls_with_old_requests(db, monkeypatch):
    """La rétention vaut pour les deux tables ; 0 = rien n'est purgé."""
    old, recent = time.time() - 10 * 86_400, time.time() - 3600
    with stats._reader() as conn:
        for ts in (old, recent):
            conn.execute(stats.INSERT_TOOL,
                         (ts, "web_search", "/v1/tools", "", "ok", 0.1, 1))
            conn.execute(stats.INSERT, (ts, "a/m", "a", "m", "/v1/messages",
                                        200, 1.0, 1, 1, 1, 0, 0))
        conn.commit()
        count = lambda: [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in stats.TABLES]
        monkeypatch.setattr(stats, "RETENTION_DAYS", 0)
        stats._purge(conn)
        assert count() == [2, 2]
        monkeypatch.setattr(stats, "RETENTION_DAYS", 7)
        stats._purge(conn)
        assert count() == [1, 1]
        assert conn.execute("SELECT ts FROM tool_calls").fetchone()[0] == recent


def test_page_ui_versionne_ses_fichiers_statiques():
    """Gabarit et script vont ensemble : leurs URL portent une empreinte,
    pour qu'un navigateur ne marie pas le nouveau gabarit à l'ancien script
    resté en cache (page blanche), et la page elle-même n'est pas gardée."""
    from fastapi.testclient import TestClient
    from llm_proxy import app as A
    r = TestClient(A.app).get("/ui")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    for name in ("dashboard.js", "dashboard.css", "vue.global.prod.js"):
        assert f'/ui/static/{name}?v={A.UI_VERSION}"' in r.text, name
    assert TestClient(A.app).get(
        f"/ui/static/dashboard.js?v={A.UI_VERSION}").status_code == 200
