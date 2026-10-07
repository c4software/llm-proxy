"""L'outil hébergé `image_generation` (llm_proxy/tools/image_generation.py)
contre un FAUX backend d'images — httpx.MockTransport, aucun réseau : ce
que l'outil envoie, le texte qu'il écrit pour le modèle, le fichier qu'il
rend, ses codes d'erreur, d'où il accepte de lire une image rendue par
URL, et la retouche d'une image téléchargée sous le garde-fou. La
configuration est posée par monkeypatch sur les constantes des modules.

Un test par comportement ; les familles d'entrées sont des tables, et
l'assertion nomme l'entrée fautive."""

import asyncio
import base64
import json
import socket
import struct

import conftest  # noqa: F401 — pose CONFIG_PATH avant tout import du paquet
import httpx
import pytest

from llm_proxy import albert, backends, files, tools
from llm_proxy.tools import image_generation as ig
from llm_proxy.tools import net

# Un PNG de 512×512 (l'en-tête suffit : rien ne le décode) et un JPEG.
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 512, 512) \
    + bytes(2000)
JPEG = b"\xff\xd8\xff\xe0\x00\x04JF\xff\xc0\x00\x11\x08" \
    + struct.pack(">HH", 300, 400) + bytes(20)
NAME = "image-f53131.png"
TOOL = ig.TOOL


def go(coro):
    return asyncio.run(coro)


def b64(data: bytes) -> dict:
    return {"created": 1, "data": [{"b64_json": base64.b64encode(data).decode()}]}


class Backend:
    """Le faux backend d'images «img» : garde chaque requête reçue ; à un
    POST il répond `reply` (un JSON servi en 200, une httpx.Response, ou
    une exception à lever), à un GET `stored[chemin]`."""

    def __init__(self):
        self.requests, self.reply, self.stored = [], b64(PNG), {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            body = self.stored.get(request.url.path)
            return httpx.Response(404) if body is None \
                else httpx.Response(200, content=body)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, httpx.Response) \
            else httpx.Response(200, json=self.reply)


@pytest.fixture(autouse=True)
def img(monkeypatch):
    """Les réglages dont dépendent les tests, le backend «img» (sans
    quota, à une adresse PRIVÉE) vers lequel pointe
    [tools.image_generation].model, et un DNS sans réseau. `img.lines` :
    les lignes de statistiques des requêtes au backend."""
    env = Backend()
    b = backends.Backend("img", {"url": "http://10.0.0.9:8009", "api_key": "k"})
    b.client = httpx.AsyncClient(base_url=b.url,
                                 transport=httpx.MockTransport(env.handler))
    env.backend, env.lines = b, []
    monkeypatch.setitem(backends.BACKENDS, "img", b)
    for name, value in {
            "ENABLED": True, "MODEL": "img/qwen-image", "SIZE": "512x512",
            "SIZES": ["512x512", "1024x1024"], "TIMEOUT": 300,
            "DOWNLOAD_TIMEOUT": 30, "MAX_CALLS": 2, "MAX_BYTES": 10_000,
            "EDITS": False, "MAX_INPUT_BYTES": 10_000}.items():
        monkeypatch.setattr(ig, name, value)
    monkeypatch.setattr(ig.stats, "record", lambda *a: env.lines.append(a))
    monkeypatch.setattr(files, "PUBLIC_URL", "https://proxy.test")
    monkeypatch.setattr(files, "STORE", files.Store(3600, 100_000, 50_000))
    monkeypatch.setattr(net, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(net, "ALLOWED_DOMAINS", [])
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", [])
    table = {"site.test": "93.184.216.34", "cdn.test": "1.1.1.1",
             "intern.test": "10.0.0.5"}
    real = socket.getaddrinfo

    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (table[host], port))]
        return real(host, port, family, type, proto,
                    flags | socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return env


def rendu(args=None, web=None, seen=None, **more) -> tools.Result:
    """Ce que tools.Hosted fait d'un `run` appelé à la main. `web` : le
    faux web des téléchargements gardés, {(hôte, chemin): octets}."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        body = (web or {}).get((request.headers["host"], request.url.path))
        return httpx.Response(404) if body is None \
            else httpx.Response(200, content=body)

    try:
        return go(TOOL.run({"prompt": "un bateau rouge", **(args or {}), **more},
                           tools.Call(endpoint="/v1/tools"),
                           transport=httpx.MockTransport(handler)))
    except tools.ToolError as exc:
        return tools.failure(exc.code, exc.message)


def test_contrat_de_l_outil(monkeypatch):
    assert (TOOL.name, TOOL.kinds) == ("image_generation", ("image_generation",))
    assert TOOL.responses is None and TOOL.anthropic is None
    # Son délai couvre la génération et deux téléchargements ; ses appels
    # sont comptés à part, et peu nombreux.
    assert TOOL.timeout == 360 and TOOL.max_calls == 2 and TOOL.enabled
    fn = TOOL.spec({"image_generation"})["function"]
    assert fn["name"] == "image_generation"
    assert fn["parameters"]["required"] == ["prompt"]
    assert list(fn["parameters"]["properties"]) == ["prompt", "size"]
    assert fn["parameters"]["properties"]["size"]["enum"] == [
        "512x512", "1024x1024"]
    assert "must not write a link" in fn["description"]
    assert "2 at most" in fn["description"] and "edit" not in fn["description"]
    # La retouche n'est annoncée que si elle est active.
    monkeypatch.setattr(ig, "EDITS", True)
    fn = TOOL.spec({"image_generation"})["function"]
    assert "image_url" in fn["parameters"]["properties"]
    assert "or edit an existing image" in fn["description"]
    # Au registre, après code_execution ; déclarable par son nom.
    names = [t.name for t in tools.REGISTRY]
    assert names.index("image_generation") > names.index("code_execution")
    assert tools.Hosted().for_kind("image_generation") == [TOOL]
    assert tools.Hosted().for_responses("image_generation") == []


def test_requete_au_backend_et_image_rendue_en_fichier(img):
    r = rendu()
    (request,) = img.requests
    assert str(request.url) == "http://10.0.0.9:8009/v1/images/generations"
    assert request.headers["authorization"] == "Bearer k"
    # Préfixe retiré ; ni `n`, ni `response_format` : ce que le backend
    # réel accepte, rien de plus.
    assert json.loads(request.content) == {
        "model": "qwen-image", "prompt": "un bateau rouge", "size": "512x512"}
    assert r.error is None
    assert r.text == (
        f"Image generated: {NAME} (image/png, 512x512, 2 kB). It is shown "
        "to the user with your answer; do not write a link or a markdown "
        "image yourself. You cannot see it: do not describe details the "
        "prompt does not state.")
    assert r.files == (tools.Artifact(NAME, "image/png", PNG),)
    assert r.meta == {"model": "img/qwen-image", "file": NAME,
                      "media_type": "image/png", "size": "512x512",
                      "bytes": len(PNG)}
    # La ligne de statistiques « requête », sous la route de l'outil.
    assert [line[:5] for line in img.lines] == [
        ("img/qwen-image", "img", "qwen-image", "/v1/tools/image_generation",
         200)]
    # Le type et les dimensions se lisent dans les OCTETS : un JPEG rendu
    # pour une taille demandée en PNG est dit tel qu'il est.
    img.reply = b64(JPEG)
    r = rendu(size=" 1024×1024 ")
    assert json.loads(img.requests[-1].content)["size"] == "1024x1024"
    assert "(image/jpeg, 400x300, 1 kB)" in r.text
    assert r.files[0].name.endswith(".jpg") and r.files[0].data == JPEG


def test_arguments_refuses_sans_aucune_requete(img):
    for args, said in (
            ({"prompt": ""}, "`prompt` is required"),
            ({"prompt": 3}, "`prompt` is required"),
            ({"prompt": "x" * 4001}, "longer than 4000 characters"),
            ({"size": "640x480"}, "must be one of 512x512, 1024x1024."),
            ({"size": 512}, "`size` must be a string."),
            ({"image_url": "https://site.test/a.png"},
             "this proxy does not edit images")):
        r = rendu(args)
        assert r.error == "invalid_input" and said in r.text, args
    assert img.requests == [] and img.lines == []


def test_non_configure_ou_image_impossible_a_remettre_dit_avant_de_generer(
        monkeypatch, img):
    for model in ("", "sans-prefixe", "inconnu/qwen-image"):
        monkeypatch.setattr(ig, "MODEL", model)
        r = rendu()
        assert (r.error, r.text) == ("unavailable", (
            "Error: image generation is not configured on this proxy.")), model
    monkeypatch.setattr(ig, "MODEL", "img/qwen-image")
    # Pas d'adresse publique : aucun lien ne pourrait être écrit.
    monkeypatch.setattr(files, "PUBLIC_URL", "")
    r = rendu()
    assert r.error == "unavailable" and "no public URL is configured" in r.text
    assert img.requests == []
    # Une image plus grosse que la borne : générée, pas remise, et dit.
    monkeypatch.setattr(files, "PUBLIC_URL", "https://proxy.test")
    monkeypatch.setattr(ig, "MAX_BYTES", 1000)
    r = rendu()
    assert r.error == "unsupported" and r.files == ()
    assert "larger than 1000 bytes. Ask for a smaller size." in r.text


def test_echec_du_backend_rendu_avec_son_code_sans_son_message(img):
    secret = "CUDA out of memory at 10.0.0.9"
    for reply, code, said, status in (
            (httpx.ConnectError("refus"), "unavailable", "offline", 503),
            (httpx.ReadTimeout("lent"), "unavailable", "did not answer", 503),
            (httpx.Response(429, text=secret), "too_many_requests", "busy", 429),
            (httpx.Response(400, json={"error": {"message": secret}}),
             "invalid_input", "refused this request (HTTP 400)", 400),
            (httpx.Response(500, text=secret), "unavailable",
             "failed (HTTP 500)", 500),
            (httpx.Response(200, text="<html>"), "unavailable", "no image", 200),
            ({"data": []}, "unavailable", "no image", 200),
            ({"data": [{"revised_prompt": "x"}]}, "unavailable", "no image", 200),
            ({"data": [{"b64_json": "%%%="}]}, "unavailable", "unreadable", 200),
            (b64(b"<svg onload=alert(1)>"), "unavailable", "unreadable", 200)):
        img.reply, img.lines[:] = reply, []
        r = rendu()
        assert r.error == code and said in r.text, (reply, r.text)
        assert secret not in r.text and "10.0.0.9" not in r.text, reply
        assert r.files == () and img.lines[0][4] == status, reply


def test_image_rendue_par_url_lue_chez_le_backend_ou_sous_le_garde_fou(img):
    """Une `url` est écrite par le BACKEND : de son origine (ou relative),
    c'est l'adresse de configuration — privée, lue par son client, avec
    sa clé ; d'une autre origine, elle passe par le garde-fou des adresses
    publiques comme une cible du modèle."""
    img.stored["/out/a.png"] = PNG
    for url in ("http://10.0.0.9:8009/out/a.png", "/out/a.png"):
        img.reply, img.requests[:] = {"data": [{"url": url}]}, []
        r = rendu()
        assert r.error is None and r.files[0].data == PNG, url
        assert "http" not in r.text and "10.0.0.9" not in r.text, url
        get = img.requests[-1]
        assert (get.method, get.url.path) == ("GET", "/out/a.png"), url
        assert get.headers["authorization"] == "Bearer k", url
    # Autre origine, publique : téléchargement gardé, sans la clé du backend.
    seen = []
    img.reply = {"data": [{"url": "https://cdn.test/b.png"}]}
    r = rendu(web={("cdn.test", "/b.png"): PNG}, seen=seen)
    assert r.error is None and r.files[0].data == PNG
    assert "authorization" not in seen[0].headers
    # Autre origine, privée (un autre port du backend compris), ou image
    # introuvable : rien n'est lu, et l'adresse ne part pas au modèle.
    for url in ("http://intern.test/b.png", "http://10.0.0.9:9000/b.png",
                "http://169.254.169.254/latest", "file:///etc/passwd",
                "https://cdn.test/absente.png", "/out/absente.png"):
        seen[:] = []
        img.reply = {"data": [{"url": url}]}
        r = rendu(web={("cdn.test", "/b.png"): PNG}, seen=seen)
        assert r.error == "unavailable" and r.files == (), url
        assert "could not be fetched" in r.text and url not in r.text, url
        assert [s.url.path for s in seen] in ([], ["/absente.png"]), url


def test_backend_a_quotas_passe_par_son_limiteur(monkeypatch, img):
    """Ce que fait app.gate pour une génération relayée."""
    pris = []

    class Limiteur:
        async def acquire(self, cost, wait_gone=None):
            pris.append(cost)
            if len(pris) > 1:
                raise albert.QuotaWaitTooLong(1200, "minute")
            return 0.0

    monkeypatch.setattr(img.backend, "quotas", True)
    monkeypatch.setattr(img.backend, "quota_state", type("Q", (), {
        "get_limiter": lambda self, payload: Limiteur()})())
    assert rendu().error is None and pris == [1]
    assert rendu().error == "too_many_requests" and len(img.requests) == 1


def test_retouche_image_telechargee_sous_le_garde_fou_puis_envoyee(
        monkeypatch, img):
    monkeypatch.setattr(ig, "EDITS", True)
    web = {("site.test", "/photo.jpg"): JPEG, ("site.test", "/page"): b"<html>" * 9,
           ("site.test", "/gros.png"): PNG + bytes(10_000)}
    r = rendu(web=web, prompt="ajoute un chapeau",
              image_url="https://site.test/photo.jpg")
    (request,) = img.requests
    assert request.url.path == "/v1/images/edits"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    for part in (b'name="model"\r\n\r\nqwen-image\r\n',
                 b'name="prompt"\r\n\r\najoute un chapeau\r\n',
                 b'name="image"; filename="image.jpg"\r\n'
                 b'Content-Type: image/jpeg\r\n\r\n' + JPEG):
        assert part in body, part
    # La taille d'une retouche est celle de l'image, sauf demande.
    assert b'name="size"' not in body
    assert r.error is None and r.text.startswith(f"Image edited: {NAME} (")
    assert r.files == (tools.Artifact(NAME, "image/png", PNG),)
    rendu(web=web, image_url="https://site.test/photo.jpg", size="1024x1024")
    assert b'name="size"\r\n\r\n1024x1024\r\n' in img.requests[-1].content
    # Ce qui n'est pas lu : rien ne part au backend.
    img.requests[:] = []
    for url, code, said in (
            ("http://intern.test/a.png", "not_allowed", "private or local"),
            ("http://10.0.0.9:8009/out/a.png", "not_allowed", "private or local"),
            ("https://site.test/absente.png", "not_accessible", "HTTP 404"),
            ("https://site.test/page", "unsupported", "is not a PNG, JPEG"),
            ("https://site.test/gros.png", "unsupported", "larger than 10000")):
        r = rendu(web=web, image_url=url)
        assert r.error == code and said in r.text, (url, r.text)
    assert img.requests == []


def test_par_hosted_delai_et_compte_propres_et_lien_ecrit_par_le_proxy(
        monkeypatch, img):
    """L'outil tel que l'exécuteur l'appelle : son délai (Tool.timeout) à
    la place de [tools].run_timeout, son compte d'appels à part ; et son
    fichier, rangé par la surface, devient une image en markdown."""
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.01)
    h = tools.Hosted([TOOL], tools.Memory(4, 60))
    assert h.own("image_generation") and h.cap(None, "image_generation") == 2
    r = go(h.run("image_generation", '{"prompt": "un phare"}', 0))
    assert r.error is None and len(r.text) < 300
    (stored,) = files.keep(r.files)
    assert stored.markdown == (
        f"![{NAME}](https://proxy.test/v1/files/{stored.token}/{NAME})")
    assert (stored.inline, stored.media_type) == (True, "image/png")
    r = go(h.run("image_generation", '{"prompt": "un phare"}', 2))
    assert (r.error, r.text) == ("limit", (
        "Error: the limit of 2 image_generation calls for one answer is "
        "reached. Answer now with what you already have."))
    assert len(img.requests) == 1
