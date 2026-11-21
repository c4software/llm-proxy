"""Les webhooks (storage/webhooks.py, ui/webhook_routes.py, ui/webhook_turn.py,
tools/webhook.py) : le magasin, la réception au format Slack, le tour mené
par le proxy, l'envoi sortant et l'outil `webhook`. Sans réseau : l'envoi
(net.post) et le backend (relaying.send_upstream) sont remplacés."""

import json

import pytest

from fakes import FakeUpstream, everywhere, go

from llm_proxy.access import auth
from llm_proxy.storage import conversations, webhooks
from llm_proxy.tools import net, webhook as slack
from llm_proxy.tools.contract import Call, ToolError
from llm_proxy.ui import dashboard, webhook_routes, webhook_turn


@pytest.fixture(autouse=True)
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conversations.db"))
    everywhere(monkeypatch, dashboard, "UI_CHAT", True)
    monkeypatch.setattr(slack, "ENABLED", True)
    webhook_routes._recent.clear()
    webhook_routes._running.clear()


@pytest.fixture
def posted(monkeypatch):
    """Ce que le proxy POSTe vers l'extérieur : (url, corps JSON)."""
    sent = []

    async def post(url, content, content_type, **kw):
        sent.append((url, json.loads(content)))
        return 200, b"ok"
    monkeypatch.setattr(net, "post", post)
    return sent


def answer(text, **message):
    return FakeUpstream(json.dumps({
        "id": "x", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text, **message}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}).encode())


# ── le format Slack ─────────────────────────────────────────────────────

def test_markdown_vers_mrkdwn():
    got = slack.to_mrkdwn("# Titre\n**gras** et ~~barré~~, [lien](https://x.fr) a<b\n"
                          "- un\n```python\n**pas touché**\n```")
    assert got == ("*Titre*\n*gras* et ~barré~, <https://x.fr|lien> a&lt;b\n"
                   "• un\n```\n**pas touché**\n```")


def test_texte_d_un_message_slack():
    assert slack.from_slack({"text": "Voir <https://x.fr|ce lien> &amp; <@U12|bob>"}) \
        == "Voir [ce lien](https://x.fr) & @bob"
    assert slack.from_slack({"blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Alerte"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "CPU à 99 %"},
         "fields": [{"type": "mrkdwn", "text": "*Hôte* srv1"}]},
        {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
            {"type": "text", "text": "détail "}, {"type": "link", "url": "https://y.fr"}]}]},
    ]}) == "Alerte\n\nCPU à 99 %\n\n*Hôte* srv1\n\ndétail \n\nhttps://y.fr"
    assert slack.from_slack({"attachments": [{"title": "Build", "text": "échec",
                                              "fields": [{"title": "branche", "value": "main"}]}]}) \
        == "**Build**\néchec\nbranche: main"
    assert slack.from_slack({"text": "  "}) == ""


# ── le magasin ──────────────────────────────────────────────────────────

def test_magasin_jeton_cloisonne_et_regenere():
    out = webhooks.create_out("a", {"name": "Équipe", "url": "https://hooks.slack.com/services/T/B/secret"})
    assert out["url"] == "https://hooks.slack.com/…cret"
    hook, token = webhooks.create_in("a", {"name": "Alertes", "model": "essai/m",
                                           "system": "Sois bref.", "reply": out["id"]}, None, "a")
    assert token.startswith("wh-") and hook["hint"] == token[3:9]
    owner, got = webhooks.by_token(token)
    assert owner == "a" and got["system"] == "Sois bref." and got["reply"] == out["id"]
    assert webhooks.by_token("wh-faux") is None and webhooks.list_in("b") == []
    # Le compte b ne touche pas aux webhooks de a.
    with pytest.raises(webhooks.WebhookError):
        webhooks.update_in("b", hook["id"], {"name": "x"})
    with pytest.raises(webhooks.WebhookError, match="sortant"):
        webhooks.create_in("b", {"name": "x", "reply": out["id"]}, None, "b")
    _, fresh = webhooks.rotate_in("a", hook["id"])
    assert webhooks.by_token(token) is None and webhooks.by_token(fresh)[0] == "a"
    # Le sortant supprimé : l'entrant n'y répond plus.
    webhooks.delete_out("a", out["id"])
    assert webhooks.get_in("a", hook["id"])["reply"] == ""
    with pytest.raises(webhooks.WebhookError, match="URL"):
        webhooks.create_out("a", {"name": "x", "url": "file:///etc/passwd"})


# ── la réception ────────────────────────────────────────────────────────

@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(webhook_routes, "_start", lambda *a: calls.append(a))
    return calls


def test_reception_aux_formes_de_slack(proxy, started):
    hook, token = webhooks.create_in("", {"name": "H", "model": "essai/m"}, None, "")
    c = proxy.client
    assert c.post("/hooks/wh-inconnu", json={"text": "x"}).text == "no_service"
    r = c.post(f"/hooks/{token}", json={"text": "Bonjour", "username": "zabbix"})
    assert (r.status_code, r.text) == (200, "ok")
    owner, got, text, meta = started[-1]
    assert (owner, got["id"], text, meta["user"]) == ("", hook["id"], "Bonjour", "zabbix")
    # payload= (formulaire de Slack), et commande slash.
    c.post(f"/hooks/{token}", data={"payload": json.dumps({"text": "par formulaire"})})
    assert started[-1][2] == "par formulaire"
    c.post(f"/hooks/{token}", data={"text": "/ask quoi", "user_name": "bob",
                                    "response_url": "https://hooks.slack.com/commands/1"})
    assert started[-1][3]["response_url"] == "https://hooks.slack.com/commands/1"
    assert c.post(f"/hooks/{token}", json={"blocks": []}).text == "no_text"
    assert c.post(f"/hooks/{token}", content=b"{", headers={"content-type": "application/json"}).text \
        == "invalid_payload"
    # Le modèle du corps : seulement si le webhook le permet.
    r = c.post(f"/hooks/{token}", json={"text": "x", "model": "essai/autre"})
    assert (r.status_code, r.text) == (403, "action_prohibited")
    webhooks.update_in("", hook["id"], {"name": "H", "model_override": True})
    assert c.post(f"/hooks/{token}", json={"text": "x", "model": "inconnu/m"}).text == "model_not_found"
    assert c.post(f"/hooks/{token}", json={"text": "x", "model": "essai/autre"}).text == "ok"
    assert started[-1][3]["model"] == "essai/autre"


def test_reception_bornee_et_fermee(proxy, started, monkeypatch):
    _, token = webhooks.create_in("", {"name": "H"}, None, "")
    monkeypatch.setattr(webhook_routes, "CALLS_PER_MINUTE", 2)
    codes = [proxy.client.post(f"/hooks/{token}", json={"text": "x"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    monkeypatch.setattr(slack, "ENABLED", False)
    assert proxy.client.post(f"/hooks/{token}", json={"text": "x"}).status_code == 404


def test_reception_sans_cle_meme_auth_active(proxy, started, monkeypatch):
    """La clé du proxy n'est pas demandée (le jeton vaut droit d'accès) ;
    une clé du TOML retirée depuis ferme le webhook qu'elle a créé."""
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-a"])
    owner = auth.toml_digests().pop()
    _, token = webhooks.create_in(owner, {"name": "H"}, None, owner)
    assert proxy.client.post(f"/hooks/{token}", json={"text": "x"}).text == "ok"
    assert proxy.client.post("/v1/models").status_code == 401
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-b"])
    assert proxy.client.post(f"/hooks/{token}", json={"text": "x"}).status_code == 403


# ── le tour ─────────────────────────────────────────────────────────────

def test_tour_nouvelle_conversation_et_reponse_sortante(proxy, posted):
    out = webhooks.create_out("", {"name": "Équipe", "url": "https://chat.example/hooks/abc"})
    hook, _ = webhooks.create_in("", {"name": "Alertes", "model": "essai/m",
                                      "system": "Réponds en une phrase.",
                                      "reply": out["id"]}, None, "")
    proxy.replies = [answer("**Rien** de grave.", reasoning_content="je lis")]
    go(webhook_turn.handle("", hook, "Disque plein sur srv1 ?", {"user": "zabbix", "thread_ts": "1.2"}))
    sent = proxy.sent[-1]
    assert sent["model"] == "m" and sent["stream"] is False
    assert sent["messages"] == [{"role": "system", "content": "Réponds en une phrase."},
                                {"role": "user", "content": "Disque plein sur srv1 ?"}]
    [conv] = conversations.list_all("")
    assert conv["title"].startswith("Alertes · Disque plein") and conv["model"] == "essai/m"
    q, a = conv["messages"]
    assert q["webhook"]["from"] == "zabbix" and q["role"] == "user"
    assert (a["content"], a["reasoning"], a["error"]) == ("**Rien** de grave.", "je lis", "")
    assert a["usage"]["total_tokens"] == 13 and a["duration"] > 0
    assert posted == [("https://chat.example/hooks/abc", {"text": "*Rien* de grave.", "thread_ts": "1.2"})]
    assert webhooks.get_in("", hook["id"])["calls"] == 1
    assert webhooks.list_out("")[0]["calls"] == 1


def test_tour_toujours_la_meme_conversation(proxy, posted):
    hook, _ = webhooks.create_in("", {"name": "Fil", "model": "essai/m",
                                      "target": "conversation"}, None, "")
    proxy.replies = [answer("un"), answer("deux")]
    go(webhook_turn.handle("", hook, "premier", {}))
    cid = webhooks.get_in("", hook["id"])["conversation"]
    assert cid
    go(webhook_turn.handle("", hook, "second", {}))
    [conv] = conversations.list_all("")
    assert conv["id"] == cid and [m["content"] for m in conv["messages"]] \
        == ["premier", "un", "second", "deux"]
    # Le second tour ne reçoit QUE son message : pas les tours précédents.
    assert [m["content"] for m in proxy.sent[-1]["messages"]] == ["second"]


def test_tour_dans_une_conversation_existante(proxy, posted):
    """Une conversation de la page : le modèle reçoit son prompt système (le
    webhook n'en a pas) et le seul message reçu — ni ses messages, ni ses
    tours d'outils, ni son résumé. Le message et la réponse y sont rangés."""
    conversations.save("", "c1", {
        "id": "c1", "title": "T", "created": 1, "updated": 1, "model": "essai/m",
        "system": "Prompt de la page.", "tools": [], "messages": [
            {"role": "user", "content": "vieux"},
            {"role": "assistant", "content": "oublié"},
            {"role": "summary", "content": "On a parlé de X."},
            {"role": "user", "content": "et la date ?"},
            {"role": "assistant", "content": "", "final": "Lundi.", "steps": [
                {"content": "", "calls": [{"id": "t1", "name": "get_date",
                                           "arguments": "{}", "result": "lundi"}]}]},
        ]})
    hook, _ = webhooks.create_in("", {"name": "H", "target": "conversation",
                                      "conversation": "c1"}, None, "")
    proxy.replies = [answer("ok")]
    go(webhook_turn.handle("", hook, "suite", {}))
    assert proxy.sent[-1]["messages"] == [{"role": "system", "content": "Prompt de la page."},
                                          {"role": "user", "content": "suite"}]
    assert [m["content"] for m in conversations.get("", "c1")["messages"][-2:]] == ["suite", "ok"]


def test_tour_en_erreur_est_dit(proxy, posted):
    out = webhooks.create_out("", {"name": "S", "url": "https://chat.example/h"})
    hook, _ = webhooks.create_in("", {"name": "H", "model": "essai/m", "reply": out["id"]}, None, "")
    proxy.replies = [(502, "proxy_error", "backend injoignable")]
    go(webhook_turn.handle("", hook, "x", {}))
    a = conversations.list_all("")[0]["messages"][-1]
    assert "injoignable" in a["error"]
    assert posted[-1][1]["text"].startswith("Erreur : ")
    assert "injoignable" in webhooks.get_in("", hook["id"])["last_error"]


# ── l'outil ─────────────────────────────────────────────────────────────

def test_outil_webhook(posted):
    call = Call(client="a")
    with pytest.raises(ToolError, match="no outgoing webhook"):
        go(slack.TOOL.run({"text": "x"}, call))
    webhooks.create_out("a", {"name": "Équipe", "url": "https://chat.example/1", "tool": True})
    got = go(slack.TOOL.run({"text": "Salut **toi**"}, call))
    assert "Équipe" in got.text and posted[-1] == ("https://chat.example/1", {"text": "Salut *toi*"})
    webhooks.create_out("a", {"name": "Ops", "url": "https://chat.example/2", "tool": True})
    with pytest.raises(ToolError, match="several"):
        go(slack.TOOL.run({"text": "x"}, call))
    go(slack.TOOL.run({"text": "x", "webhook": "ops"}, call))
    assert posted[-1][0] == "https://chat.example/2"
    with pytest.raises(ToolError, match="available"):
        go(slack.TOOL.run({"text": "x", "webhook": "autre"}, call))
    # Un autre compte n'a rien.
    with pytest.raises(ToolError, match="no outgoing"):
        go(slack.TOOL.run({"text": "x", "webhook": "Ops"}, Call(client="b")))


# ── la page ─────────────────────────────────────────────────────────────

def test_routes_de_la_page(proxy, posted, monkeypatch):
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-a", "sk-b"])
    c = proxy.client
    c.post("/ui/login", json={"key": "sk-a"})
    out = c.post("/ui/webhooks/out", json={"name": "S", "url": "https://chat.example/xyz1234"}).json()["webhook"]
    r = c.post("/ui/webhooks/in", json={"name": "H", "model": "essai/m", "reply": out["id"]})
    assert r.status_code == 200, r.text
    token, hook = r.json()["token"], r.json()["webhook"]
    assert "api_key_id" not in hook
    j = c.get("/ui/webhooks").json()
    assert [h["id"] for h in j["incoming"]] == [hook["id"]] and j["path"] == "/hooks/"
    assert "xyz1234" not in json.dumps(j)                 # l'URL sortante, masquée
    assert c.post(f"/ui/webhooks/out/{out['id']}/test", json={}).json()["sent"] is True
    assert posted[-1][0] == "https://chat.example/xyz1234"
    # Un autre compte ne voit rien.
    c.cookies.clear()
    c.post("/ui/login", json={"key": "sk-b"})
    assert c.get("/ui/webhooks").json()["incoming"] == []
    assert c.delete(f"/ui/webhooks/in/{hook['id']}").json()["deleted"] is False
    assert webhooks.by_token(token) is not None
    assert c.get("/ui/session").json()["webhooks"] is True


def test_outils_declares_comme_la_page(monkeypatch):
    """Seuls les outils hébergés ACTIFS du chat sont déclarés (un inactif
    vaudrait un 400), et rien si le chat n'en présente pas."""
    from llm_proxy.surfaces import chat_api
    monkeypatch.setattr(webhook_turn, "chat_tool_groups",
                        lambda: [{"type": "web_search"}, {"type": "webhook"}])
    everywhere(monkeypatch, chat_api, "ENABLED", True)
    assert webhook_turn.declarable(["webhook", "memory", 3]) == ["webhook"]
    everywhere(monkeypatch, chat_api, "ENABLED", False)
    assert webhook_turn.declarable(["webhook"]) == []


def test_corps_mal_formes_et_boucle(proxy, started):
    """Un corps Slack mal typé est refusé (400), pas une panne ; un message
    posté par un proxy (un sortant pointé sur un entrant) est refusé."""
    _, token = webhooks.create_in("", {"name": "H"}, None, "")
    c = proxy.client
    for body in ({"attachments": 5}, {"text": "x", "attachments": [{"fields": 5}]},
                 {"blocks": [{"elements": 5}]}, {"blocks": [{"fields": 7}]}):
        r = c.post(f"/hooks/{token}", json=body)
        assert r.status_code in (200, 400), (body, r.status_code)
    assert c.post(f"/hooks/{token}", json={"text": "x"},
                  headers={"user-agent": slack.USER_AGENT}).status_code == 403
    with pytest.raises(webhooks.WebhookError, match="entrant"):
        webhooks.create_out("", {"name": "boucle", "url": f"https://proxy.example/hooks/{token}"})


def test_redirection_est_un_echec(monkeypatch):
    async def post(url, content, content_type, **kw):
        return 301, b""
    monkeypatch.setattr(net, "post", post)
    with pytest.raises(ToolError, match="redirection"):
        go(slack.send("http://chat.example/h", "x"))


def test_echec_du_tour_est_envoye(proxy, posted, monkeypatch):
    """Une panne en route : l'erreur part quand même au sortant, et la
    conversation dédiée créée est gardée."""
    out = webhooks.create_out("", {"name": "S", "url": "https://chat.example/h"})
    hook, _ = webhooks.create_in("", {"name": "H", "model": "essai/m", "target": "conversation",
                                      "reply": out["id"]}, None, "")

    async def boom(*a):
        raise RuntimeError("explosion")
    monkeypatch.setattr(webhook_turn, "complete", boom)
    go(webhook_turn.handle("", hook, "x", {}))
    assert "explosion" in posted[-1][1]["text"]
    cid = webhooks.get_in("", hook["id"])["conversation"]
    assert cid and conversations.get("", cid)["messages"][-1]["error"]


def test_response_url_jamais_privee(proxy, monkeypatch):
    seen = []

    async def post(url, content, content_type, **kw):
        seen.append(kw["allow_private"])
        return 200, b"ok"
    monkeypatch.setattr(net, "post", post)
    monkeypatch.setattr(slack, "ALLOW_PRIVATE", True)
    hook, _ = webhooks.create_in("", {"name": "H", "model": "essai/m"}, None, "")
    proxy.replies = [answer("ok")]
    go(webhook_turn.handle("", hook, "x", {"response_url": "https://slack.example/r"}))
    assert seen == [False]


def test_salon_du_sortant(posted):
    out = webhooks.create_out("a", {"name": "S", "url": "https://chat.example/h",
                                    "channel": " #alertes ", "tool": True})
    assert out["channel"] == "#alertes"
    go(slack.TOOL.run({"text": "x"}, Call(client="a")))
    assert posted[-1][1] == {"text": "x", "channel": "#alertes"}
    # Modifié sans URL : l'URL reste, le salon change ; vide, plus de `channel`.
    webhooks.update_out("a", out["id"], {"name": "S", "channel": ""})   # `tool` absent : inchangé
    go(slack.TOOL.run({"text": "y"}, Call(client="a")))
    assert posted[-1] == ("https://chat.example/h", {"text": "y"})


def test_base_d_avant_le_salon(tmp_path, monkeypatch):
    """Une table webhooks_out d'avant la colonne `channel` la reçoit."""
    import sqlite3
    path = str(tmp_path / "vieille.db")
    with sqlite3.connect(path) as c:
        c.execute("""CREATE TABLE webhooks_out (owner TEXT NOT NULL, id TEXT NOT NULL,
            name TEXT NOT NULL, url TEXT NOT NULL, created REAL NOT NULL, last_used REAL,
            calls INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (owner, id))""")
        c.execute("INSERT INTO webhooks_out (owner, id, name, url, created) "
                  "VALUES ('a', 'o1', 'S', 'https://chat.example/h', 1)")
    monkeypatch.setattr(conversations, "DB_PATH", path)
    assert webhooks.list_out("a")[0]["channel"] == ""


def test_lien_recopiable(proxy, monkeypatch):
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-a", "sk-b"])
    c = proxy.client
    c.post("/ui/login", json={"key": "sk-a"})
    r = c.post("/ui/webhooks/in", json={"name": "H"}).json()
    hid, token = r["webhook"]["id"], r["token"]
    assert r["webhook"]["copyable"] is True and "token" not in c.get("/ui/webhooks").text
    assert c.get(f"/ui/webhooks/in/{hid}/token").json()["token"] == token
    fresh = c.post(f"/ui/webhooks/in/{hid}/rotate", json={}).json()["token"]
    assert c.get(f"/ui/webhooks/in/{hid}/token").json()["token"] == fresh != token
    # Un entrant d'avant le jeton gardé : 409, à régénérer.
    conversations._run(lambda conn: conn.execute("UPDATE webhooks_in SET token = ''"))
    assert c.get(f"/ui/webhooks/in/{hid}/token").status_code == 409
    assert c.get("/ui/webhooks").json()["incoming"][0]["copyable"] is False
    # Un autre compte n'y a pas accès.
    c.cookies.clear()
    c.post("/ui/login", json={"key": "sk-b"})
    assert c.get(f"/ui/webhooks/in/{hid}/token").status_code == 404


def test_passe_plat(proxy, posted):
    with pytest.raises(webhooks.WebhookError, match="passe plat"):
        webhooks.create_in("", {"name": "P", "target": "none"}, None, "")
    out = webhooks.create_out("", {"name": "S", "url": "https://chat.example/h"})
    hook, _ = webhooks.create_in("", {"name": "P", "model": "essai/m", "target": "none",
                                      "system": "Traduis en anglais.", "reply": out["id"]},
                                 None, "")
    proxy.replies = [answer("Hello")]
    go(webhook_turn.handle("", hook, "Bonjour", {}))
    assert proxy.sent[-1]["messages"] == [{"role": "system", "content": "Traduis en anglais."},
                                          {"role": "user", "content": "Bonjour"}]
    assert posted[-1] == ("https://chat.example/h", {"text": "Hello"})
    assert conversations.list_all("") == []
    assert webhooks.get_in("", hook["id"])["calls"] == 1


def test_reponse_en_cours_visible(proxy, posted, monkeypatch):
    """Pendant le tour, la conversation porte la question ET une réponse
    « en cours » (serverTurn), que la réponse remplace ensuite (même id)."""
    hook, _ = webhooks.create_in("", {"name": "H", "model": "essai/m"}, None, "")
    seen = {}
    real = webhook_turn.complete

    async def spy(owner, h, body):
        [conv] = conversations.list_all("")
        seen["during"] = conv["messages"][-1]
        assert [m["role"] for m in body["messages"]] == ["user"]   # pas la réponse en cours
        return await real(owner, h, body)
    monkeypatch.setattr(webhook_turn, "complete", spy)
    proxy.replies = [answer("fini")]
    go(webhook_turn.handle("", hook, "x", {}))
    assert seen["during"]["serverTurn"] is True and seen["during"]["content"] == ""
    [conv] = conversations.list_all("")
    q, a = conv["messages"]
    assert a["id"] == seen["during"]["id"] and a["content"] == "fini" and "serverTurn" not in a


def test_sortant_non_expose_a_l_outil(posted):
    webhooks.create_out("a", {"name": "Privé", "url": "https://chat.example/p", "tool": False})
    with pytest.raises(ToolError, match="no outgoing webhook available"):
        go(slack.TOOL.run({"text": "x"}, Call(client="a")))
    assert webhooks.create_out("a", {"name": "Défaut", "url": "https://chat.example/d"})["tool"] is False
    o = webhooks.create_out("a", {"name": "Public", "url": "https://chat.example/q", "tool": True})
    assert o["tool"] is True
    go(slack.TOOL.run({"text": "x"}, Call(client="a")))
    assert posted[-1][0] == "https://chat.example/q"
    with pytest.raises(ToolError, match="Public"):        # Privé n'est pas proposé
        go(slack.TOOL.run({"text": "x", "webhook": "Privé"}, Call(client="a")))


def test_passe_plat_garde_son_sortant(proxy, posted):
    """Le sortant d'un passe plat ne se supprime pas (409) ; un passe plat
    resté sans sortant n'appelle pas le modèle."""
    out = webhooks.create_out("", {"name": "S", "url": "https://chat.example/h"})
    hook, _ = webhooks.create_in("", {"name": "P", "model": "essai/m", "target": "none",
                                      "reply": out["id"]}, None, "")
    with pytest.raises(webhooks.WebhookError, match="passe plat") as exc:
        webhooks.delete_out("", out["id"])
    assert exc.value.status == 409
    conversations._run(lambda c: c.execute("UPDATE webhooks_in SET reply = ''"))
    go(webhook_turn.handle("", hook, "x", {}))
    assert proxy.sent == [] and posted == []
    assert "ignoré" in webhooks.get_in("", hook["id"])["last_error"]


def test_reponse_non_rangee_ne_reste_pas_en_cours(proxy, posted, monkeypatch):
    """Si la réponse ne peut pas être rangée, une réponse courte en erreur
    prend la place de celle « en cours »."""
    hook, _ = webhooks.create_in("", {"name": "H", "model": "essai/m"}, None, "")
    real, calls = webhook_turn._replace, []

    def flaky(owner, cid, question, message):
        calls.append(message)
        if len(calls) == 1:
            raise conversations.HistoryError("historique plein", 413)
        return real(owner, cid, question, message)
    monkeypatch.setattr(webhook_turn, "_replace", flaky)
    proxy.replies = [answer("une très longue réponse")]
    go(webhook_turn.handle("", hook, "x", {}))
    a = conversations.list_all("")[0]["messages"][-1]
    assert "serverTurn" not in a and a["error"].startswith("réponse non rangée")
    assert "historique plein" in webhooks.get_in("", hook["id"])["last_error"]


def test_question_retiree_reponse_non_ajoutee(proxy, posted, monkeypatch):
    """La page a retiré la question (message modifié) : la réponse du
    webhook n'est pas ajoutée seule à la fin."""
    hook, _ = webhooks.create_in("", {"name": "H", "model": "essai/m"}, None, "")
    real = webhook_turn.complete

    async def page_edits(owner, h, body):
        [conv] = conversations.list_all("")
        conv["messages"] = []
        conversations.save("", conv["id"], conv)
        return await real(owner, h, body)
    monkeypatch.setattr(webhook_turn, "complete", page_edits)
    proxy.replies = [answer("ok")]
    go(webhook_turn.handle("", hook, "x", {}))
    assert conversations.list_all("")[0]["messages"] == []
    assert "retirée" in webhooks.get_in("", hook["id"])["last_error"]


def test_conversation_dediee_supprimee_recreee(proxy, posted):
    hook, _ = webhooks.create_in("", {"name": "F", "model": "essai/m", "target": "conversation"},
                                 None, "")
    proxy.replies = [answer("un"), answer("deux")]
    go(webhook_turn.handle("", hook, "premier", {}))
    first = webhooks.get_in("", hook["id"])["conversation"]
    conversations.delete("", first)
    go(webhook_turn.handle("", hook, "second", {}))
    second = webhooks.get_in("", hook["id"])["conversation"]
    assert second and second != first
    assert [m["content"] for m in conversations.get("", second)["messages"]] == ["second", "deux"]


def test_tool_est_un_booleen():
    with pytest.raises(webhooks.WebhookError, match="true ou false"):
        webhooks.create_out("a", {"name": "S", "url": "https://chat.example/h", "tool": "true"})



def test_passe_plat_en_incognito(proxy, posted, monkeypatch):
    """Le passe plat ne garde rien : son tour est INCOGNITO."""
    from llm_proxy.core.settings import INCOGNITO
    out = webhooks.create_out("", {"name": "S", "url": "https://chat.example/h"})
    hook, _ = webhooks.create_in("", {"name": "P", "model": "essai/m", "target": "none",
                                      "reply": out["id"]}, None, "")
    seen = []
    real = webhook_turn.complete

    async def spy(owner, h, body):
        seen.append(INCOGNITO.get())
        return await real(owner, h, body)
    monkeypatch.setattr(webhook_turn, "complete", spy)
    proxy.replies = [answer("ok")]
    go(webhook_turn.handle("", hook, "x", {}))
    assert seen == [True] and INCOGNITO.get() is False
