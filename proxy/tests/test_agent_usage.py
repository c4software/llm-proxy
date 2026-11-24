"""ui/agent_usage.py : l'enregistrement d'usage d'un compte au format du
panneau d'agents d'Omarchy (GET /v1/usage/agent avec une clé,
/ui/usage/agent pour la page). Comptes OIDC (`idp`), base de statistiques
temporaire remplie à la main. Le forfait mensuel d'un backend : dans
test_backends.py, avec son faux compte Mistral."""

import sqlite3
import time
from datetime import datetime, timezone

from fakes import admin_and_user, bearer

from llm_proxy.access import auth, fairshare
from llm_proxy.core import identity
from llm_proxy.storage import stats

DAY = 86_400


def spent(key_id, ts, model, prompt, completion, cached=0):
    """Une requête servie, telle que stats.record l'aurait rangée."""
    backend, name = model.split("/")
    with sqlite3.connect(stats.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO requests (ts, model_key, backend, model, endpoint, status, latency, "
            "prompt_tokens, completion_tokens, exact, streamed, cached_tokens, api_key_id) "
            "VALUES (?, ?, ?, ?, '/v1/chat/completions', 200, 1.0, ?, ?, 1, 0, ?, ?)",
            (ts, model, backend, name, prompt, completion, cached, key_id))


def bob_with_key(idp):
    alice, bob, bob_id = admin_and_user(idp)
    r = bob.post("/ui/keys", json={"label": "omarchy"})
    assert r.status_code == 200, r.text
    return alice, bob, bob_id, r.json()["key"]


def test_un_compte_lit_son_usage_au_format_d_omarchy(idp, stats_db):
    """Ses tokens (toutes ses clés et sa page, pas ceux d'un autre compte)
    par jour du fuseau demandé et par modèle, l'entrée servie du cache à
    part ; la même chose par la clé et par la page. Sans clé : 401 ; un
    fuseau inconnu : 400."""
    alice, bob, bob_id, key = bob_with_key(idp)
    now = time.time()
    spent(identity.owner(key), now, "essai/a", 100, 20, cached=60)
    spent(auth.account_owner(bob_id), now - 2 * DAY, "essai/b", 10, 5)
    spent(identity.owner(key), now - 10 * DAY, "essai/a", 1, 1)
    alice_id = alice.get("/ui/session").json()["account"]["id"]
    spent(auth.account_owner(alice_id), now, "essai/a", 999, 999)

    r = idp.client.get("/v1/usage/agent?tz=UTC", headers=bearer(key))
    assert r.status_code == 200, r.text
    got = r.json()
    utc = lambda days: datetime.fromtimestamp(now - days * DAY, timezone.utc).strftime("%Y-%m-%d")
    assert {k: got[k] for k in ("schemaVersion", "id", "ready", "scope", "hasPromptStats",
                                "todayPrompts", "todayTotalTokens", "todayTokensByModel",
                                "totalPrompts", "activeDays", "activeDates")} == {
        "schemaVersion": 1, "id": "llm-proxy", "ready": True, "scope": "account",
        "hasPromptStats": False, "todayPrompts": 1, "todayTotalTokens": 120,
        "todayTokensByModel": {"essai/a": 120}, "totalPrompts": 3, "activeDays": 3,
        "activeDates": [utc(10), utc(2), utc(0)]}
    assert got["recentDays"] == [{"date": utc(d), "messageCount": {0: 120, 2: 15}.get(d, 0)}
                                 for d in range(6, -1, -1)]
    assert got["modelUsage"] == {
        "essai/a": {"inputTokens": 41, "outputTokens": 21, "cacheReadInputTokens": 60,
                    "cacheCreationInputTokens": 0},
        "essai/b": {"inputTokens": 10, "outputTokens": 5, "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0}}
    page = bob.get("/ui/usage/agent?tz=UTC").json()
    assert {k: v for k, v in page.items() if k != "updatedAt"} == \
        {k: v for k, v in got.items() if k != "updatedAt"}
    assert idp.client.get("/v1/usage/agent").status_code == 401
    r = idp.client.get("/v1/usage/agent?tz=Mars/Olympus", headers=bearer(key))
    assert r.status_code == 400 and r.json()["error"]["param"] == "tz"


def test_une_cle_du_toml_seule_ne_voit_que_ses_requetes(proxy, stats_db, monkeypatch):
    """Sans comptes, une clé du TOML : ses seules lignes ; rien encore,
    un enregistrement vide que le panneau saute (ready faux)."""
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["cle-a", "cle-b"])
    got = proxy.client.get("/v1/usage/agent", headers=bearer("cle-a")).json()
    assert (got["ready"], got["totalPrompts"], got["limits"], got["limitsFetchedAt"],
            len(got["recentDays"])) == (False, 0, [], 0, 7)
    spent(identity.owner("cle-a"), time.time(), "essai/a", 10, 2)
    spent(identity.owner("cle-b"), time.time(), "essai/a", 500, 500)
    got = proxy.client.get("/v1/usage/agent", headers=bearer("cle-a")).json()
    assert (got["ready"], got["totalPrompts"], got["todayTotalTokens"]) == (True, 1, 12)


def test_la_part_equitable_du_compte_est_une_limite(idp, stats_db, monkeypatch):
    """Une part par heure glissante : une jauge « Session », la part prise
    de ce que le compte a consommé dans l'heure ; sans part, aucune."""
    _, bob, bob_id, key = bob_with_key(idp)
    fairshare.reset()
    fairshare.charge(auth.account_owner(bob_id), 250)
    assert idp.client.get("/v1/usage/agent", headers=bearer(key)).json()["limits"] == []
    monkeypatch.setattr(fairshare, "DEFAULT_SHARE", 1000)
    got = idp.client.get("/v1/usage/agent", headers=bearer(key)).json()
    assert got["limits"] == [{"label": "Part équitable", "title": "Session",
                              "percent": 0.25, "resetsAt": ""}]
    assert got["ready"] and got["limitsFetchedAt"] > 0
