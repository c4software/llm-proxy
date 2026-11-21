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
    # Le second tour voit le premier échange.
    assert [m["content"] for m in proxy.sent[-1]["messages"]] == ["premier", "un", "second"]


def test_tour_dans_une_conversation_existante(proxy, posted):
    """Une conversation de la page : son prompt système (le webhook n'en a
    pas), ses tours d'outils de la page rejoués, son dernier résumé."""
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
    msgs = proxy.sent[-1]["messages"]
    assert msgs[0]["content"].startswith("Prompt de la page.\n\nRÉSUMÉ")
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "tool", "assistant", "user"]
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "get_date"
    assert conversations.get("", "c1")["messages"][-1]["content"] == "ok"


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
    webhooks.create_out("a", {"name": "Équipe", "url": "https://chat.example/1"})
    got = go(slack.TOOL.run({"text": "Salut **toi**"}, call))
    assert "Équipe" in got.text and posted[-1] == ("https://chat.example/1", {"text": "Salut *toi*"})
    webhooks.create_out("a", {"name": "Ops", "url": "https://chat.example/2"})
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
