"""Les webhooks (storage/webhooks.py, ui/webhook_routes.py, ui/webhook_turn.py,
tools/webhook.py) : le magasin, la réception au format Slack, le tour mené
par le proxy, l'envoi sortant et l'outil `webhook`. Sans réseau : l'envoi
JSON (net.post), celui d'Apprise (requests, sous son vrai code) et le
backend (relaying.send_upstream) sont remplacés."""

import json
import sqlite3

import pytest
import requests

from fakes import FakeUpstream, everywhere, go

from llm_proxy.access import auth
from llm_proxy.core.settings import FORGE_SCOPE, INCOGNITO
from llm_proxy.storage import conversations, webhooks
from llm_proxy.tools import net, webhook as slack
from llm_proxy.tools.contract import Call, ToolError
from llm_proxy.ui import dashboard, webhook_routes, webhook_turn

URL = "https://chat.example/h"


@pytest.fixture(autouse=True)
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "DB_PATH", str(tmp_path / "conversations.db"))
    everywhere(monkeypatch, dashboard, "UI_CHAT", True)
    monkeypatch.setattr(slack, "ENABLED", True)
    webhook_routes.RECENT.clear()
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


class Sent(list):
    """Les envois vus à une frontière ; `status` : ce que le service répond
    (en 200, « ok », ce qu'attend un webhook Slack)."""
    status = 200


@pytest.fixture
def notified(monkeypatch):
    """Ce qu'Apprise envoie, à sa frontière (requests) : (url, corps)."""
    sent = Sent()

    def request(method, url, data=None, **kw):
        sent.append((url, json.loads(data)))
        r = requests.Response()
        r.status_code, r._content = sent.status, b"ok" if sent.status == 200 else b"{}"
        return r
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(requests, "post", lambda url, **kw: request("POST", url, **kw))
    return sent


@pytest.fixture
def started(monkeypatch):
    """Les tours que la réception lance — (compte, webhook, texte, méta) —
    sans les mener."""
    calls = []
    monkeypatch.setattr(webhook_routes, "_start", lambda *a: calls.append(a))
    return calls


@pytest.fixture
def page(proxy, monkeypatch):
    """La page, connectée par la clé sk-a ; `page("sk-b")` change de compte."""
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-a", "sk-b"])
    c = proxy.client

    def login(key):
        c.cookies.clear()
        assert c.post("/ui/login", json={"key": key}).status_code == 200
        return c
    login("sk-a")
    login.client = c
    return login


def old_channels(channel: str) -> None:
    """Le salon de tous les sortants, posé comme dans une base d'avant (la
    page ne le saisit plus)."""
    with sqlite3.connect(conversations.DB_PATH) as c:
        c.execute("UPDATE webhooks_out SET channel = ?", (channel,))


def outgoing(owner="", **settings) -> dict:
    return webhooks.create_out(owner, {"name": "S", "url": URL, **settings})


def incoming(owner="", **settings) -> dict:
    return webhooks.create_in(owner, {"name": "H", "model": "essai/m", **settings},
                              None, owner)[0]


def address(hook, owner="") -> str:
    """Le chemin où poster un message à cet entrant."""
    return "/hooks/" + webhooks.token_in(owner, hook["id"])


def turn(hook, text="x", **meta) -> None:
    """Le tour d'un message reçu, mené jusqu'au bout."""
    go(webhook_turn.handle("", hook, text, meta))


def answer(text, **message):
    return FakeUpstream(json.dumps({
        "id": "x", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text, **message}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}).encode())


def last_message(owner="") -> dict:
    return conversations.list_all(owner)[0]["messages"][-1]


def last_error(hook, owner="") -> str:
    return webhooks.get_in(owner, hook["id"])["last_error"]


# ── le format Slack ─────────────────────────────────────────────────────

def test_markdown_vers_mrkdwn():
    got = slack.to_mrkdwn("# Titre\n**gras** et ~~barré~~, [lien](https://x.fr) a<b\n"
                          "- un\n```python\n**pas touché**\n```")
    assert got == ("*Titre*\n*gras* et ~barré~, <https://x.fr|lien> a&lt;b\n"
                   "• un\n```\n**pas touché**\n```")


def test_texte_d_un_message_slack():
    """Le texte d'un message reçu : `text`, sinon ses blocs, sinon ses
    pièces jointes."""
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

def test_un_compte_ne_touche_pas_aux_webhooks_d_un_autre():
    out = outgoing("a")
    hook, token = webhooks.create_in("a", {"name": "Alertes", "system": "Sois bref.",
                                           "reply": out["id"]}, None, "a")
    owner, got = webhooks.by_token(token)
    assert owner == "a" and got["system"] == "Sois bref." and got["reply"] == out["id"]
    assert webhooks.by_token("wh-faux") is None
    assert webhooks.list_in("b") == [] and webhooks.list_out("b") == []
    with pytest.raises(webhooks.WebhookError):
        webhooks.update_in("b", hook["id"], {"name": "x"})
    with pytest.raises(webhooks.WebhookError, match="sortant"):    # le sortant de a
        webhooks.create_in("b", {"name": "x", "reply": out["id"]}, None, "b")


def test_service_du_sortant_et_urls_refusees():
    """Une URL Apprise (ou l'URL d'un webhook qu'Apprise reconnaît) : son
    service ; une autre URL http(s) : un sortant JSON, à ressaisir ; ni
    l'une ni l'autre : refusée."""
    for url, service in (("https://hooks.slack.com/services/T/B/secretsecret", "Slack"),
                         ("discord://123/secretsecret", "Discord"),
                         ("tgram://123456:secretsecret/987", "Telegram"),
                         ("json://chat.example/hooks/secretsecret", "JSON"),
                         ("ntfy://ntfy.example/secretsecret", "ntfy"),
                         ("https://chat.example/hooks/secretsecret", "JSON compatible Slack")):
        out = outgoing("a", name=service, url=url)
        assert (out["service"], out["legacy"]) == (service, service == "JSON compatible Slack"), url
    for url, why in (("file:///etc/passwd", "URL"), ("pas une url", "URL"), ("slack://", "URL"),
                     ("fcm://projet/appareil?keyfile=/etc/passwd", "fichier")):
        with pytest.raises(webhooks.WebhookError, match=why):
            outgoing("a", url=url)
    with pytest.raises(webhooks.WebhookError, match="true ou false"):
        outgoing("a", tool="true")


# ── la réception ────────────────────────────────────────────────────────

def test_reception_aux_formes_de_slack(proxy, started):
    """JSON, formulaire `payload=`, commande slash, texte brut : 200 « ok »
    tout de suite, et le tour est lancé avec le texte et ce qui sert à
    répondre."""
    hook = incoming()
    c, url = proxy.client, address(hook)
    r = c.post(url, json={"text": "Bonjour", "username": "zabbix", "thread_ts": "1.2"})
    assert (r.status_code, r.text) == (200, "ok")
    owner, got, text, meta = started[-1]
    assert (owner, got["id"], text) == ("", hook["id"], "Bonjour")
    assert (meta["user"], meta["thread_ts"]) == ("zabbix", "1.2")
    c.post(url, data={"payload": json.dumps({"text": "par formulaire"})})
    assert started[-1][2] == "par formulaire"
    c.post(url, data={"text": "/ask quoi", "user_name": "bob",
                      "response_url": "https://hooks.slack.com/commands/1"})
    assert started[-1][3]["response_url"] == "https://hooks.slack.com/commands/1"
    # Une adresse de réponse qui n'est pas en https n'est pas retenue.
    c.post(url, data={"text": "x", "response_url": "http://interne.lan/r"})
    assert started[-1][3]["response_url"] == ""
    c.post(url, content="en clair".encode(), headers={"content-type": "text/plain"})
    assert started[-1][2] == "en clair" and len(started) == 5


def test_reception_refuse_comme_slack(proxy, started, monkeypatch):
    """Jeton inconnu, corps illisible, vide, mal typé ou trop long : l'erreur
    de Slack en texte, jamais une panne, et aucun tour lancé."""
    c, url = proxy.client, address(incoming())

    def refus(**kw):
        r = c.post(kw.pop("url", url), **kw)
        return r.status_code, r.text
    assert refus(url="/hooks/wh-inconnu", json={"text": "x"}) == (404, "no_service")
    assert refus(content=b"{", headers={"content-type": "application/json"}) \
        == (400, "invalid_payload")
    assert refus(json=["text"]) == (400, "invalid_payload")
    for body in ({"blocks": []}, {"text": 5}, {"attachments": 5}, {"blocks": "x"},
                 {"blocks": [{"elements": 5}]}, {"blocks": [{"fields": 7}]}):
        assert refus(json=body) == (400, "no_text"), body
    monkeypatch.setattr(webhook_routes, "MAX_TEXT", 5)
    assert refus(json={"text": "trop long"}) == (400, "msg_too_long")
    assert started == []
    # Une partie mal typée n'empêche pas de lire le texte.
    assert refus(json={"text": "x", "attachments": [{"fields": 5}]}) == (200, "ok")


def test_modele_du_corps_seulement_si_le_webhook_le_permet(proxy, started):
    hook = incoming()
    c, url = proxy.client, address(hook)
    r = c.post(url, json={"text": "x", "model": "essai/autre"})
    assert (r.status_code, r.text) == (403, "action_prohibited")
    webhooks.update_in("", hook["id"], {"name": "H", "model_override": True})
    assert c.post(url, json={"text": "x", "model": "inconnu/m"}).text == "model_not_found"
    assert c.post(url, json={"text": "x", "model": "essai/autre"}).text == "ok"
    assert [s[3]["model"] for s in started] == ["essai/autre"]


def test_reception_bornee_et_fermee(proxy, started, monkeypatch):
    """Au-delà du débit par minute : 429 ; webhooks désactivés : 404."""
    url = address(incoming())
    monkeypatch.setattr(webhook_routes, "CALLS_PER_MINUTE", 2)
    codes = [proxy.client.post(url, json={"text": "x"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429] and len(started) == 2
    monkeypatch.setattr(slack, "ENABLED", False)
    assert proxy.client.post(url, json={"text": "x"}).status_code == 404


def test_reception_sans_cle_meme_auth_active(proxy, started, monkeypatch):
    """La clé du proxy n'est pas demandée (le jeton vaut droit d'accès) ;
    une clé du TOML retirée depuis ferme le webhook qu'elle a créé."""
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-a"])
    owner = auth.toml_digests().pop()
    url = address(incoming(owner), owner)
    assert proxy.client.post(url, json={"text": "x"}).text == "ok"
    assert proxy.client.post("/v1/models").status_code == 401
    monkeypatch.setattr(auth, "PROXY_API_KEYS", ["sk-b"])
    assert proxy.client.post(url, json={"text": "x"}).status_code == 403


def test_boucle_sortant_vers_entrant_refusee(proxy, started):
    """Un sortant ne peut pas viser un entrant du proxy, et un message posté
    par un proxy (son User-Agent) est refusé : la réponse en ferait un
    nouveau message, sans fin."""
    url = address(incoming())
    token = url.removeprefix("/hooks/")
    for loop, why in ((f"https://proxy.example{url}", "entrant"),
                      (f"mmost://proxy.example/{token}", "entrant"),
                      (f"json://proxy.example/%68ooks/{token}", "entrant"),
                      ("json://proxy.example/x?+User-Agent=autre", "User-Agent")):
        with pytest.raises(webhooks.WebhookError, match=why):
            outgoing(name="boucle", url=loop)
    # Apprise se présente comme le proxy : l'entrant le reconnaît.
    assert webhooks.ASSET.app_id == slack.USER_AGENT
    r = proxy.client.post(url, json={"text": "x"},
                          headers={"user-agent": slack.USER_AGENT})
    assert r.status_code == 403 and started == []


def test_corps_natif_d_une_forge_resume(proxy, started):
    """Le corps d'un webhook de GitHub, de Gitea (qui envoie aussi
    X-GitHub-Event) ou de GitLab, sans `text` : reconnu à son en-tête, il
    devient un message court (titre, lien, texte), son auteur en méta ; un
    événement non résumé passe en JSON."""
    c, url = proxy.client, address(incoming())
    issue = {"action": "opened",
             "repository": {"full_name": "o/r", "html_url": "https://forge.example/o/r"},
             "sender": {"login": "alice"},
             "issue": {"number": 7, "title": "Le build casse", "body": "Depuis hier.",
                       "html_url": "https://forge.example/o/r/issues/7"}}
    for headers, forge in (({"x-github-event": "issues"}, "[GitHub]"),
                           ({"x-github-event": "issues", "x-gitea-event": "issues"}, "[Gitea]")):
        assert c.post(url, json=issue, headers=headers).text == "ok", forge
        text, meta = started[-1][2], started[-1][3]
        assert text.startswith(forge + " issue opened #7 — o/r"), (forge, text)
        for part in ("Le build casse", "https://forge.example/o/r/issues/7", "Depuis hier."):
            assert part in text, (forge, part)
        assert meta["user"] == "alice", forge
        assert meta["forge"] == ("forge.example", "o/r"), forge
    c.post(url, headers={"x-gitlab-event": "Issue Hook"}, json={
        "object_kind": "issue", "user": {"username": "bob"},
        "project": {"path_with_namespace": "equipe/api"},
        "object_attributes": {"action": "open", "iid": 3, "title": "Panne",
                              "url": "https://gitlab.exemple.fr/equipe/api/-/issues/3",
                              "description": "Rien ne répond."}})
    text = started[-1][2]
    assert text.startswith("[GitLab] issue open #3 — equipe/api") and "Rien ne répond." in text
    assert started[-1][3]["user"] == "bob"
    c.post(url, headers={"x-gitlab-event": "Note Hook"}, json={
        "object_kind": "note", "user": {"username": "carole"},
        "project": {"path_with_namespace": "equipe/api"},
        "object_attributes": {"note": "x" * 9000, "noteable_type": "Issue",
                              "url": "https://gitlab.exemple.fr/equipe/api/-/issues/3#note_1"},
        "issue": {"iid": 3, "title": "Panne"}})
    text = started[-1][2]
    assert text.startswith("[GitLab] commentaire sur Issue #3") and text.endswith("x […]")
    assert len(text) < 8300                       # le commentaire coupé à 8 000 caractères
    c.post(url, data={"payload": json.dumps(issue)}, headers={"x-github-event": "issues"})
    assert "Le build casse" in started[-1][2]
    c.post(url, json={"action": "published", "repository": {"full_name": "o/r"}},
           headers={"x-github-event": "release"})
    assert "événement release" in started[-1][2] and '"published"' in started[-1][2]
    # Gitea pose ses en-têtes aussi sur un webhook de type Slack : son texte reste.
    c.post(url, json={"text": "Nouvelle issue"}, headers={"x-gitea-event": "issues",
                                                          "x-github-event": "issues"})
    assert started[-1][2] == "Nouvelle issue"
    assert len(started) == 7


def test_forge_essai_et_branches_du_proxy_ignores(proxy, started):
    """Le `ping` d'un webhook créé, et tout événement sur une branche du
    proxy ([repos].pr_branch_prefix : PR, push, création, CI, commentaire
    sur sa MR) : 200, aucun tour — sinon la PR ouverte par le modèle
    relancerait le webhook, sans fin."""
    c, url = proxy.client, address(incoming())
    ours = "llm-proxy/corrige-le-build-1a2b3c4d"
    for headers, body in (
            ({"x-github-event": "ping"}, {"zen": "x", "hook_id": 1}),
            ({"x-github-event": "pull_request"},
             {"action": "opened", "pull_request": {"head": {"ref": ours}, "title": "t"}}),
            ({"x-gitea-event": "push"}, {"ref": "refs/heads/" + ours, "commits": []}),
            ({"x-gitlab-event": "Merge Request Hook"},
             {"object_kind": "merge_request", "object_attributes": {"source_branch": ours}}),
            ({"x-gitlab-event": "Push Hook"},
             {"object_kind": "push", "ref": "refs/heads/" + ours, "commits": []}),
            ({"x-github-event": "create"}, {"ref": ours, "ref_type": "branch"}),
            ({"x-github-event": "check_suite"}, {"check_suite": {"head_branch": ours}}),
            ({"x-github-event": "workflow_run"}, {"workflow_run": {"head_branch": ours}}),
            ({"x-github-event": "status"}, {"branches": [{"name": ours}]}),
            ({"x-gitlab-event": "Pipeline Hook"},
             {"object_kind": "pipeline", "object_attributes": {"ref": ours}}),
            ({"x-gitlab-event": "Note Hook"},
             {"object_kind": "note", "merge_request": {"source_branch": ours}})):
        r = c.post(url, json=body, headers=headers)
        assert (r.status_code, r.text) == (200, "ok"), headers
    assert started == []
    c.post(url, headers={"x-github-event": "pull_request"}, json={
        "action": "opened", "pull_request": {"head": {"ref": "fix"}, "base": {"ref": "main"},
                                             "title": "Un humain"}})
    assert "Un humain" in started[-1][2] and "fix → main" in started[-1][2]


# ── le tour ─────────────────────────────────────────────────────────────

def test_tour_nouvelle_conversation_et_reponse_sortante(proxy, posted):
    out = outgoing(name="Équipe", url="https://chat.example/hooks/abc")
    hook = incoming(name="Alertes", system="Réponds en une phrase.", reply=out["id"])
    proxy.replies = [answer("**Rien** de grave.", reasoning_content="je lis")]
    turn(hook, "Disque plein sur srv1 ?", user="zabbix", thread_ts="1.2")
    sent = proxy.sent[-1]
    assert sent["model"] == "m" and sent["stream"] is False
    assert sent["messages"] == [{"role": "system", "content": "Réponds en une phrase."},
                                {"role": "user", "content": "Disque plein sur srv1 ?"}]
    [conv] = conversations.list_all("")
    assert conv["title"].startswith("Alertes · Disque plein") and conv["model"] == "essai/m"
    # Le prompt du webhook sert à ses tours, pas à la conversation.
    assert conv["system"] == ""
    q, a = conv["messages"]
    assert q["webhook"]["from"] == "zabbix" and q["role"] == "user"
    assert (a["content"], a["reasoning"], a["error"]) == ("**Rien** de grave.", "je lis", "")
    assert a["usage"]["total_tokens"] == 13 and a["duration"] > 0
    # La réponse part en mrkdwn, dans le fil du message reçu.
    assert posted == [("https://chat.example/hooks/abc", {"text": "*Rien* de grave.", "thread_ts": "1.2"})]
    assert webhooks.get_in("", hook["id"])["calls"] == 1
    assert webhooks.list_out("")[0]["calls"] == 1


def test_conversation_dediee_reprise_sans_historique_et_recreee(proxy, posted):
    """target « conversation » : tous les messages vont dans la même, mais
    le modèle ne reçoit QUE le message du tour ; supprimée, elle est
    recréée."""
    hook = incoming(target="conversation")
    proxy.replies = [answer("un"), answer("deux"), answer("trois")]
    turn(hook, "premier")
    turn(hook, "second")
    [conv] = conversations.list_all("")
    assert conv["id"] == webhooks.get_in("", hook["id"])["conversation"]
    assert [m["content"] for m in conv["messages"]] == ["premier", "un", "second", "deux"]
    assert [m["content"] for m in proxy.sent[-1]["messages"]] == ["second"]
    conversations.delete("", conv["id"])
    turn(hook, "troisième")
    fresh = webhooks.get_in("", hook["id"])["conversation"]
    assert fresh and fresh != conv["id"]
    assert [m["content"] for m in conversations.get("", fresh)["messages"]] == ["troisième", "trois"]


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
    hook = incoming(model="", target="conversation", conversation="c1")
    proxy.replies = [answer("ok")]
    turn(hook, "suite")
    assert proxy.sent[-1]["messages"] == [{"role": "system", "content": "Prompt de la page."},
                                          {"role": "user", "content": "suite"}]
    assert [m["content"] for m in conversations.get("", "c1")["messages"][-2:]] == ["suite", "ok"]


def test_erreur_du_backend_dite_partout(proxy, posted):
    """Le backend refuse : l'erreur est dans la conversation, part au
    sortant, et reste sur le webhook."""
    hook = incoming(reply=outgoing()["id"])
    proxy.replies = [(502, "proxy_error", "backend injoignable")]
    turn(hook)
    assert "injoignable" in last_message()["error"]
    assert posted[-1][1]["text"].startswith("Erreur : ")
    assert "injoignable" in last_error(hook)


def test_panne_du_tour_envoyee_quand_meme(proxy, posted, monkeypatch):
    """Une panne en route : l'erreur part quand même au sortant, et la
    conversation dédiée créée est gardée."""
    hook = incoming(target="conversation", reply=outgoing()["id"])

    async def boom(*a):
        raise RuntimeError("explosion")
    monkeypatch.setattr(webhook_turn, "complete", boom)
    turn(hook)
    assert "explosion" in posted[-1][1]["text"]
    cid = webhooks.get_in("", hook["id"])["conversation"]
    assert cid and conversations.get("", cid)["messages"][-1]["error"]


def test_redirection_du_sortant_est_un_echec(monkeypatch):
    async def post(url, content, content_type, **kw):
        return 301, b""
    monkeypatch.setattr(net, "post", post)
    with pytest.raises(ToolError, match="redirection"):
        go(slack.send("http://chat.example/h", "x"))


def test_response_url_jamais_privee(proxy, monkeypatch):
    """Le `response_url` vient de l'appelant : même si les sortants peuvent
    viser le réseau privé, lui ne le peut pas."""
    seen = []

    async def post(url, content, content_type, **kw):
        seen.append((url, kw["allow_private"]))
        return 200, b"ok"
    monkeypatch.setattr(net, "post", post)
    monkeypatch.setattr(slack, "ALLOW_PRIVATE", True)
    proxy.replies = [answer("ok")]
    turn(incoming(), response_url="https://slack.example/r")
    assert seen == [("https://slack.example/r", False)]


def test_le_tour_d_un_evenement_de_forge_borne_les_outils_a_son_projet(monkeypatch):
    """Le projet de l'événement (meta `forge`) devient FORGE_SCOPE pendant
    tout le tour, et seulement lui ; un message ordinaire n'en a pas."""
    seen = []

    async def turn_(owner, hook, text, meta):
        seen.append(FORGE_SCOPE.get())
    monkeypatch.setattr(webhook_turn, "_turn", turn_)
    hook = incoming()
    go(webhook_turn.handle("", hook, "x", {"forge": ("forge.example", "o/r")}))
    go(webhook_turn.handle("", hook, "x", {}))
    assert seen == [("forge.example", "o/r"), None]


# ── la réponse « en cours » ─────────────────────────────────────────────

def during_the_turn(monkeypatch, then) -> None:
    """`then()` est appelé quand le modèle est interrogé, la question et la
    réponse « en cours » déjà rangées."""
    real = webhook_turn.complete

    async def spy(owner, hook, body):
        then(body)
        return await real(owner, hook, body)
    monkeypatch.setattr(webhook_turn, "complete", spy)


def test_reponse_en_cours_visible_puis_remplacee(proxy, posted, monkeypatch):
    """Pendant le tour, la conversation porte la question ET une réponse
    « en cours » (serverTurn), que la réponse remplace ensuite (même id) ;
    le modèle, lui, ne la reçoit pas."""
    seen = {}

    def look(body):
        seen["pending"] = last_message()
        seen["roles"] = [m["role"] for m in body["messages"]]
    during_the_turn(monkeypatch, look)
    proxy.replies = [answer("fini")]
    turn(incoming())
    assert seen["pending"]["serverTurn"] is True and seen["pending"]["content"] == ""
    assert seen["roles"] == ["user"]
    q, a = conversations.list_all("")[0]["messages"]
    assert a["id"] == seen["pending"]["id"] and a["content"] == "fini" and "serverTurn" not in a


def test_reponse_non_rangee_ne_reste_pas_en_cours(proxy, posted, monkeypatch):
    """Si la réponse ne peut pas être rangée (historique plein), une réponse
    courte en erreur prend la place de celle « en cours »."""
    hook = incoming()
    real, calls = webhook_turn._replace, []

    def full_once(owner, cid, question, message):
        calls.append(message)
        if len(calls) == 1:
            raise conversations.HistoryError("historique plein", 413)
        return real(owner, cid, question, message)
    monkeypatch.setattr(webhook_turn, "_replace", full_once)
    proxy.replies = [answer("une très longue réponse")]
    turn(hook)
    a = last_message()
    assert "serverTurn" not in a and a["error"].startswith("réponse non rangée")
    assert "historique plein" in last_error(hook)


def test_question_retiree_reponse_non_ajoutee(proxy, posted, monkeypatch):
    """La page a retiré la question (message modifié) pendant le tour : la
    réponse du webhook n'est pas ajoutée seule à la fin."""
    hook = incoming()

    def page_edits(body):
        [conv] = conversations.list_all("")
        conv["messages"] = []
        conversations.save("", conv["id"], conv)
    during_the_turn(monkeypatch, page_edits)
    proxy.replies = [answer("ok")]
    turn(hook)
    assert conversations.list_all("")[0]["messages"] == []
    assert "retirée" in last_error(hook)


# ── le passe plat ───────────────────────────────────────────────────────

def test_passe_plat_ne_garde_rien(proxy, posted, monkeypatch):
    """target « none » : le modèle ne reçoit que le prompt du webhook et le
    message, la réponse ne va qu'au sortant, rien n'est rangé et le tour
    est incognito."""
    incognito = []
    during_the_turn(monkeypatch, lambda body: incognito.append(INCOGNITO.get()))
    hook = incoming(name="P", target="none", system="Traduis en anglais.",
                    reply=outgoing()["id"])
    proxy.replies = [answer("Hello")]
    turn(hook, "Bonjour")
    assert proxy.sent[-1]["messages"] == [{"role": "system", "content": "Traduis en anglais."},
                                          {"role": "user", "content": "Bonjour"}]
    assert posted == [(URL, {"text": "Hello"})]
    assert conversations.list_all("") == []
    assert incognito == [True] and INCOGNITO.get() is False
    assert webhooks.get_in("", hook["id"])["calls"] == 1


def test_passe_plat_exige_et_garde_son_sortant(proxy, posted):
    """Pas de passe plat sans sortant, et ce sortant ne se supprime pas
    (409) — celui d'un entrant ordinaire, si : l'entrant n'y répond plus.
    Un passe plat resté sans sortant n'appelle pas le modèle."""
    with pytest.raises(webhooks.WebhookError, match="passe plat"):
        incoming(target="none")
    out = outgoing()
    ordinary = incoming(reply=out["id"])
    webhooks.delete_out("", out["id"])
    assert webhooks.get_in("", ordinary["id"])["reply"] == ""
    out = outgoing()
    hook = incoming(target="none", reply=out["id"])
    with pytest.raises(webhooks.WebhookError, match="passe plat") as exc:
        webhooks.delete_out("", out["id"])
    assert exc.value.status == 409
    conversations._run(lambda c: c.execute("UPDATE webhooks_in SET reply = ''"))
    turn(hook)
    assert proxy.sent == [] and posted == []
    assert "ignoré" in last_error(hook)


# ── l'envoi par Apprise ─────────────────────────────────────────────────

def test_sortant_apprise_envoie_et_note_l_erreur(proxy, notified, posted):
    """Une URL Apprise : le message part par Apprise, en Markdown, dans le
    salon de l'URL, sans fil Slack ; un échec du service est noté sur le
    sortant et rendu à l'outil."""
    out = outgoing("a", url="slack://T1/B2/C3/#alertes", tool=True)
    hook = incoming("a", reply=out["id"])
    proxy.replies = [answer("**Rien** de grave.")]
    go(webhook_turn.handle("a", hook, "x", {"thread_ts": "1.2"}))
    url, body = notified[-1]
    assert url == "https://hooks.slack.com/services/T1/B2/C3" and posted == []
    assert body["channel"] == "#alertes" and "Rien" in json.dumps(body)
    assert "thread_ts" not in body
    assert webhooks.list_out("a")[0]["last_error"] == ""
    assert "Message posted" in tool({"text": "x"}).text
    notified.status = 500
    with pytest.raises(ToolError) as exc:
        tool({"text": "x"})
    error = webhooks.list_out("a")[0]["last_error"]
    assert error and "C3" not in error + exc.value.message       # pas l'URL


def test_journal_du_proxy_sans_url_de_sortant(caplog, notified):
    """Une URL refusée, ou un envoi en échec : ni le jeton ni l'URL dans
    le journal du proxy."""
    with pytest.raises(webhooks.WebhookError):
        outgoing("a", url="mmost://secretsecret")
    outgoing("a", url="slack://T1/B2/secretsecret", tool=True)
    notified.status = 500
    with pytest.raises(ToolError):
        tool({"text": "x"})
    assert "secretsecret" not in caplog.text


def test_sortant_json_garde_son_salon_jusqu_a_l_url_ressaisie(posted):
    """Un sortant JSON garde son salon (`channel`) ; une
    URL redonnée l'efface : avec Apprise, le salon est dans l'URL."""
    out = outgoing("a", tool=True)
    old_channels("#alertes")
    tool({"text": "x"})
    assert posted[-1] == (URL, {"text": "x", "channel": "#alertes"})
    webhooks.update_out("a", out["id"], {"name": "S"})             # sans URL : inchangé
    assert webhooks.list_out("a")[0]["channel"] == "#alertes"
    webhooks.update_out("a", out["id"], {"name": "S", "url": URL})
    tool({"text": "y"})
    assert posted[-1] == (URL, {"text": "y"})


# ── l'outil ─────────────────────────────────────────────────────────────

def tool(args, client="a"):
    return go(slack.TOOL.run(args, Call(client=client)))


def test_outil_choisit_parmi_les_sortants_du_compte(posted):
    with pytest.raises(ToolError, match="no outgoing webhook"):
        tool({"text": "x"})
    outgoing("a", name="Équipe", url="https://chat.example/1", tool=True)
    got = tool({"text": "Salut **toi**"})                   # un seul : pas besoin de le nommer
    assert "Équipe" in got.text and posted[-1] == ("https://chat.example/1", {"text": "Salut *toi*"})
    outgoing("a", name="Ops", url="https://chat.example/2", tool=True)
    with pytest.raises(ToolError, match="several"):
        tool({"text": "x"})
    tool({"text": "x", "webhook": "ops"})                   # sans casse
    assert posted[-1][0] == "https://chat.example/2"
    with pytest.raises(ToolError, match="available"):
        tool({"text": "x", "webhook": "autre"})
    with pytest.raises(ToolError, match="no outgoing"):     # un autre compte n'a rien
        tool({"text": "x", "webhook": "Ops"}, client="b")
    assert len(posted) == 2


def test_outil_ne_voit_que_les_sortants_exposes(posted):
    """Un sortant n'est offert à l'outil que si `tool` est coché — et ne
    l'est pas par défaut."""
    assert outgoing("a", name="Défaut")["tool"] is False
    outgoing("a", name="Privé", url="https://chat.example/p", tool=False)
    with pytest.raises(ToolError, match="no outgoing webhook available"):
        tool({"text": "x"})
    outgoing("a", name="Public", url="https://chat.example/q", tool=True)
    tool({"text": "x"})
    assert [url for url, _ in posted] == ["https://chat.example/q"]
    with pytest.raises(ToolError, match="Public"):          # seul Public est proposé
        tool({"text": "x", "webhook": "Privé"})


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


# ── la page ─────────────────────────────────────────────────────────────

def test_routes_de_la_page_par_compte(page, posted):
    c = page.client
    out = c.post("/ui/webhooks/out", json={"name": "S", "url": "https://chat.example/xyz1234"}).json()["webhook"]
    r = c.post("/ui/webhooks/in", json={"name": "H", "model": "essai/m", "reply": out["id"]})
    assert r.status_code == 200, r.text
    token, hook = r.json()["token"], r.json()["webhook"]
    assert token.startswith("wh-") and "api_key_id" not in hook
    j = c.get("/ui/webhooks").json()
    assert [h["id"] for h in j["incoming"]] == [hook["id"]] and j["path"] == "/hooks/"
    assert j["outgoing"][0]["url"] == "https://chat.example/xyz1234"   # réaffichée, à son compte
    assert c.post(f"/ui/webhooks/out/{out['id']}/test", json={}).json()["sent"] is True
    assert posted[-1][0] == "https://chat.example/xyz1234"
    assert c.get("/ui/session").json()["webhooks"] is True
    # Un autre compte ne voit ni ne supprime rien.
    page("sk-b")
    got = c.get("/ui/webhooks")
    assert got.json()["incoming"] == [] and got.json()["outgoing"] == []
    assert "xyz1234" not in got.text
    assert c.delete(f"/ui/webhooks/in/{hook['id']}").json()["deleted"] is False
    assert c.delete(f"/ui/webhooks/out/{out['id']}").json()["deleted"] is False
    assert webhooks.by_token(token) is not None


def test_adresse_reaffichable_par_son_seul_compte(page):
    """Le jeton d'un entrant se relit, jamais dans la liste ; régénéré,
    l'ancien ne vaut plus rien ; un entrant d'avant le jeton gardé répond 409."""
    c = page.client
    r = c.post("/ui/webhooks/in", json={"name": "H"}).json()
    hid, token = r["webhook"]["id"], r["token"]
    assert r["webhook"]["copyable"] is True and token not in c.get("/ui/webhooks").text
    assert c.get(f"/ui/webhooks/in/{hid}/token").json()["token"] == token
    fresh = c.post(f"/ui/webhooks/in/{hid}/rotate", json={}).json()["token"]
    assert c.get(f"/ui/webhooks/in/{hid}/token").json()["token"] == fresh != token
    assert webhooks.by_token(token) is None and webhooks.by_token(fresh) is not None
    page("sk-b")
    assert c.get(f"/ui/webhooks/in/{hid}/token").status_code == 404
    page("sk-a")
    conversations._run(lambda conn: conn.execute("UPDATE webhooks_in SET token = ''"))
    assert c.get(f"/ui/webhooks/in/{hid}/token").status_code == 409
    assert c.get("/ui/webhooks").json()["incoming"][0]["copyable"] is False


# ── les bases d'avant ───────────────────────────────────────────────────

def test_base_d_avant_le_salon(tmp_path, monkeypatch):
    """Une table webhooks_out d'avant la colonne `channel` la reçoit."""
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


def test_salon_d_un_sortant_slack_passe_dans_son_url_une_fois():
    """Au démarrage, une fois : le salon d'un sortant hooks.slack.com passe
    dans son URL Apprise ; un autre sortant JSON garde le sien."""
    slack_out = outgoing("a", name="Slack", url="https://hooks.slack.com/services/T/B/X")
    spaced = outgoing("a", name="Espacé", url="https://hooks.slack.com/services/T/B/Z")
    other = outgoing("a", name="Autre")
    old_channels("#alertes")
    with sqlite3.connect(conversations.DB_PATH) as c:
        c.execute("UPDATE webhooks_out SET channel = 'town square' WHERE id = ?", (spaced["id"],))
    assert webhooks.move_channels_to_urls() == 2
    moved = webhooks.get_out("a", slack_out["id"])
    assert (moved["url"], moved["channel"], moved["legacy"]) == ("slack://T/B/X/%23alertes", "", False)
    assert webhooks.get_out("a", spaced["id"])["url"] == "slack://T/B/Z/town%20square"
    assert webhooks.get_out("a", other["id"])["channel"] == "#alertes"
    old_channels("#autre")
    with sqlite3.connect(conversations.DB_PATH) as c:
        c.execute("UPDATE webhooks_out SET url = 'https://hooks.slack.com/services/T/B/Y'")
    assert webhooks.move_channels_to_urls() == 0


def test_prompts_herites_retires_une_fois():
    """Le prompt d'un webhook que des conversations avaient pris (créée par
    lui, ou ouverte depuis elle) en est retiré, une seule fois : un prompt
    identique choisi ensuite reste."""
    webhooks.create_in("a", {"name": "H", "system": "Sois analyste."}, None, "a")
    for cid, system in (("c1", "Sois analyste."), ("c2", " Sois analyste.\n"), ("c3", "Autre.")):
        conversations.save("a", cid, {"id": cid, "title": "t", "created": 1, "updated": 1,
                                      "system": system, "messages": []})
    conversations.save("b", "c1", {"id": "c1", "title": "t", "created": 1, "updated": 1,
                                   "system": "Sois analyste.", "messages": []})
    assert webhooks.clear_inherited_prompts() == 2
    assert [conversations.get("a", c)["system"] for c in ("c1", "c2", "c3")] == ["", "", "Autre."]
    assert conversations.get("b", "c1")["system"] == "Sois analyste."      # un autre compte
    conversations.save("a", "c4", {"id": "c4", "title": "t", "created": 1, "updated": 1,
                                   "system": "Sois analyste.", "messages": []})
    assert webhooks.clear_inherited_prompts() == 0
    assert conversations.get("a", "c4")["system"] == "Sois analyste."
