"""L'outil hébergé `ocr` (llm_proxy/tools/ocr.py) : téléchargement sous le
garde-fou, images d'un PDF, requête au modèle de vision, mise en forme,
cache, quotas et statistiques. SANS réseau, comme test_tools.py : le web
est un httpx.MockTransport, le DNS une table, et le backend du modèle de
vision un Backend dont le client httpx parle à un faux (`Vision`), qui
garde ce qu'il reçoit. Aucune image n'est décodée : le faux « lit » une
image en retrouvant ses octets dans une table.

Un test par comportement ; les familles d'entrées sont des tables
parcourues dans le test, et l'assertion nomme l'entrée fautive."""

import asyncio
import base64
import json
import socket
import threading
import zlib

import httpx
import pytest

from llm_proxy import albert, backends, tools
from llm_proxy.tools import ocr, webcache

PUBLIC = "93.184.216.34"
PNG = b"\x89PNG\r\n\x1a\n" + b"image png"
JPEG = b"\xff\xd8\xff\xe0" + b"image jpeg"
GIF = b"GIF89a" + b"image gif"
WEBP = b"RIFF\x10\x00\x00\x00WEBP" + b"image webp"


def go(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Résolution sans réseau : une table nom → adresses (None :
    introuvable) ; hors table, seul un littéral numérique est résolu
    (AI_NUMERICHOST)."""
    table = {"site.test": [PUBLIC], "autre.test": ["1.1.1.1"],
             "intern.test": ["10.0.0.5"], "absent.test": None}
    real = socket.getaddrinfo

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        if host in table:
            if table[host] is None:
                raise socket.gaierror(socket.EAI_NONAME, "introuvable")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                    for ip in table[host]]
        return real(host, port, family, type, proto,
                    flags | socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return table


class Vision:
    """Le faux modèle de vision. `texts` : octets d'une image → ce qu'il y
    « lit » — un texte, une httpx.Response, une exception à lever, ou une
    fonction asynchrone. `seen` : les corps reçus ; `images` : celles de
    chaque requête, [(type, octets)…]."""

    def __init__(self, texts=None, default="texte lu"):
        self.texts, self.default = texts or {}, default
        self.seen, self.images, self.headers = [], [], []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        doc = json.loads(request.content)
        self.seen.append(doc)
        self.headers.append(request.headers)
        images = []
        for part in doc["messages"][0]["content"][1:]:
            head, data = part["image_url"]["url"].split(";base64,")
            images.append((head.removeprefix("data:"), base64.b64decode(data)))
        self.images.append(images)
        said = self.texts.get(images[0][1], self.default)
        if callable(said):
            said = await said()
        if isinstance(said, Exception):
            raise said
        if isinstance(said, httpx.Response):
            return said
        return answer(said)


def answer(text, finish="stop", **usage):
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": text}}],
        "usage": usage or {"prompt_tokens": 900, "completion_tokens": 40,
                           "prompt_tokens_details": {"cached_tokens": 7}}})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Les réglages dont dépendent les tests, un backend «vision» qui
    accepte les images et dont le client parle à `env.vision`, les lignes
    de statistiques dans `env.lines`, et le cache web vidé et COUPÉ (les
    tests du cache le rallument)."""
    for name, value in dict(
            MODEL="vision/lecteur", TIMEOUT=50, MAX_BYTES=20_000_000,
            MAX_IMAGE_BYTES=5_000_000, MAX_PAGES=4, MAX_CHARS=20_000,
            MAX_TOKENS=4096, CONCURRENCY=2).items():
        monkeypatch.setattr(ocr, name, value)
    # Le garde-fou commun ([tools.net]).
    for name, value in dict(ALLOW_PRIVATE=False, ALLOWED_DOMAINS=[],
                            BLOCKED_DOMAINS=[]).items():
        monkeypatch.setattr(ocr.net, name, value)
    webcache.CACHE.clear()
    monkeypatch.setattr(webcache.CACHE, "ttl", 0)
    vision = Vision()
    backend = backends.Backend("vision", {
        "url": "http://vision.invalid", "images": True, "api_key": "clef"})
    backend.client = httpx.AsyncClient(
        base_url=backend.url, transport=httpx.MockTransport(vision.handler))
    monkeypatch.setitem(backends.BACKENDS, "vision", backend)
    vision.backend, vision.lines = backend, []
    monkeypatch.setattr(ocr.stats, "record",
                        lambda *a: vision.lines.append(a))
    return vision


class Web:
    """Un faux web : `pages[chemin]` → réponse (ou fonction) sur
    site.test ; garde les requêtes reçues."""

    def __init__(self, pages=None, default=None):
        self.pages, self.default, self.requests = pages or {}, default, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        got = self.pages.get(request.url.path, self.default)
        if got is None:
            return httpx.Response(404, text="rien")
        return got(request) if callable(got) else got

    def ocr(self, url, call=None, **args) -> str:
        """Le texte rendu au modèle — ce que l'exécuteur ferait d'un
        échec prévu compris ; le résultat entier reste dans `last`."""
        try:
            self.last = go(ocr.TOOL.run(
                {"url": url, **args}, call or tools.Call(),
                transport=httpx.MockTransport(self.handler)))
        except tools.ToolError as exc:
            self.last = tools.failure(exc.code, exc.message)
        return self.last.text


def fichier(body, ct="application/octet-stream", status=200):
    return httpx.Response(status, content=body, headers={"content-type": ct})


def image_pdf(width=100, height=80, space=b"/DeviceGray", bits=8,
              filtre=b"/DCTDecode", data=JPEG, extra=b""):
    """Le dictionnaire et le flux d'une image de PDF. `filtre` None : des
    pixels bruts, compressés ici en Flate."""
    if filtre is None:
        filtre, data = b"/FlateDecode", zlib.compress(data)
    return (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/ColorSpace %s /BitsPerComponent %d /Filter %s %s /Length %d >>"
            b"\nstream\n%s\nendstream" % (width, height, space, bits, filtre,
                                          extra, len(data), data))


def scan(*pages):
    """Un PDF écrit à la main, sans couche texte : chaque page est la
    liste de ses images (image_pdf) — vide : une page sans image."""
    kids, objs, n = [], [], 3
    for images in pages:
        refs = b" ".join(b"/Im%d %d 0 R" % (i, n + 1 + i)
                         for i in range(len(images)))
        kids.append(n)
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    b"/Resources << /XObject << %s >> >> >>" % refs)
        objs += images
        n += 1 + len(images)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count %d /Kids [%s] >>" % (
                len(kids), b" ".join(b"%d 0 R" % k for k in kids))] + objs
    out, places = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objs, 1):
        places.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % place for place in places)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    return bytes(out)


def jpeg(n):
    """Le JPEG (factice) de la page `n` d'un scan."""
    return JPEG + b" page %d" % n


def scan_jpeg(pages):
    return scan(*([image_pdf(data=jpeg(n))] for n in range(1, pages + 1)))


def lit(env, pages):
    """Le faux modèle lit «Texte de la page n» sur le JPEG de la page n."""
    env.texts.update({jpeg(n): f"Texte de la page {n}"
                      for n in range(1, pages + 1)})


# ── une image : ce qui part au modèle de vision, ce qui est rendu ───────

def test_ocr_image_envoyee_au_modele_de_vision_et_texte_rendu(env):
    env.texts[PNG] = "  FACTURE n° 12\nTotal : 42,00 €  "
    env.backend.max_tokens = 1000
    # Le type annoncé est faux : ce sont les premiers octets qui décident.
    web = Web({"/scan": fichier(PNG, ct="text/plain")})
    out = web.ocr("http://site.test/scan?x=1#bas")
    assert out == ("URL: http://site.test/scan?x=1#bas\nContent-Type: image/png"
                   "\n\n---\nFACTURE n° 12\nTotal : 42,00 €")
    assert web.last.error is None
    assert web.last.meta == {"url": "http://site.test/scan?x=1#bas",
                             "content_type": "image/png", "pages": [1],
                             "model": "vision/lecteur"}
    assert web.last.sources == (tools.Source(
        "http://site.test/scan?x=1#bas", "http://site.test/scan?x=1#bas"),)
    # Le téléchargement : vers l'adresse vérifiée, sous le nom demandé.
    (req,) = web.requests
    assert str(req.url) == f"http://{PUBLIC}/scan?x=1"
    assert req.headers["host"] == req.extensions["sni_hostname"] == "site.test"
    assert req.headers["user-agent"] == ocr.USER_AGENT
    # La requête au backend : une requête chat/completions ordinaire, modèle
    # sans préfixe, clé du backend, `max_tokens` sous le plafond du backend.
    (doc,) = env.seen
    assert env.images == [[("image/png", PNG)]]
    assert doc["model"] == "lecteur" and doc["stream"] is False
    assert doc["temperature"] == 0 and doc["max_tokens"] == 1000
    assert doc["messages"][0]["content"][0] == {"type": "text",
                                                "text": ocr.PROMPT}
    assert env.headers[0]["authorization"] == "Bearer clef"
    # Et sa ligne de statistiques, avec l'usage rendu par le backend.
    (line,) = env.lines
    assert line[:5] == ("vision/lecteur", "vision", "lecteur",
                        "/v1/tools/ocr", 200)
    assert line[6:] == (900, 40, True, False, 7)
    assert ocr.TOOL.summary({"url": "http://site.test/scan"}, web.last) == {
        "type": "ocr", "url": "http://site.test/scan"}


def test_ocr_types_d_image_reconnus_a_leurs_premiers_octets(env):
    for corps, kind in [(PNG, "image/png"), (JPEG, "image/jpeg"),
                        (GIF, "image/gif"), (WEBP, "image/webp")]:
        out = Web(default=fichier(corps)).ocr("http://site.test/i")
        assert out.endswith("\n---\ntexte lu"), kind
        assert env.images[-1] == [(kind, corps)], kind


def test_ocr_reponses_du_modele_de_vision(env):
    """Sans texte, coupée par `max_tokens`, contenu en parties."""
    web = Web(default=fichier(PNG))
    for nom, dit, attendu in [
        ("sans texte", ocr.NO_TEXT, "(no text on this page)"),
        ("coupée", answer("début", finish="length"),
         "début\n[transcription cut: the page is longer than the output limit]"),
        ("parties", answer([{"type": "text", "text": "en "},
                            {"type": "text", "text": "parties"}]), "en parties"),
    ]:
        env.texts[PNG] = dit
        out = web.ocr("http://site.test/i")
        assert out.endswith("\n---\n" + attendu), nom
        assert web.last.error is None, nom


# ── le garde-fou : rien ne part quand la cible est refusée ──────────────

def test_ocr_cible_refusee_ni_telechargement_ni_lecture(env, monkeypatch):
    web = Web({"/vers-prive": httpx.Response(
                   302, headers={"location": "http://intern.test/secret.png"}),
               "/vers-autre": httpx.Response(
                   302, headers={"location": "http://autre.test/i.png"})},
              default=fichier(PNG))
    for url in ["", None, 5]:
        assert web.ocr(url) == "Error: `url` is required.", url
    for url, code in [("file:///etc/passwd", "invalid_input"),
                      ("site.test/i.png", "invalid_input"),
                      ("http://127.0.0.1/i.png", "not_allowed"),
                      ("http://intern.test/i.png", "not_allowed"),
                      ("http://absent.test/i.png", "not_accessible")]:
        assert web.ocr(url).startswith("Error:"), url
        assert web.last.error == code, url
    assert web.requests == []
    # Une redirection est recontrôlée : l'adresse privée n'est pas jointe.
    assert web.ocr("http://site.test/vers-prive").startswith("Error:")
    assert web.last.error == "not_allowed" and len(web.requests) == 1
    # Les listes de domaines : celles de la configuration, et celles que
    # le client pose (call.settings), qui s'y ajoutent — à chaque saut.
    monkeypatch.setattr(ocr.net, "BLOCKED_DOMAINS", ["autre.test"])
    refus = "Error: autre.test is not a domain this proxy is allowed to read."
    assert web.ocr("http://autre.test/i.png") == refus
    assert web.ocr("http://site.test/vers-autre") == refus
    assert web.last.error == "not_allowed"
    monkeypatch.setattr(ocr.net, "BLOCKED_DOMAINS", [])
    assert web.ocr("http://site.test/i.png", tools.Call(
        {"allowed_domains": ["autre.test"]})).startswith("Error: site.test is")
    assert web.ocr("http://site.test/i.png", tools.Call(
        {"blocked_domains": ["site.test"]})).startswith("Error: site.test is")
    # Rien n'a été donné à lire au modèle de vision.
    assert env.seen == [] and env.lines == []


def test_ocr_fichier_illisible_refuse_sans_lecture(env, monkeypatch):
    for nom, reponse, code, attendu in [
        ("page web", fichier(b"<html>bonjour</html>", ct="text/html; x=1"),
         "unsupported", "is text/html, not an image (PNG, JPEG, GIF, WebP) or "
                        "a PDF: there is nothing to OCR."),
        ("vide", fichier(b"", ct=""), "unsupported", "is of an unknown type,"),
        ("404", fichier(PNG, status=404), "not_accessible", "returned HTTP 404."),
        ("429", fichier(PNG, status=429), "too_many_requests",
         "returned HTTP 429."),
    ]:
        web = Web(default=reponse)
        out = web.ocr("http://site.test/f")
        assert out.startswith("Error: http://site.test/f ") and attendu in out, nom
        assert web.last.error == code, nom
    # Plus gros que la borne : refusé, et le flux n'est pas lu jusqu'au bout.
    monkeypatch.setattr(ocr, "MAX_BYTES", 300)
    servis = []

    async def sans_fin():
        yield PNG
        while True:
            servis.append(1)
            yield b"y" * 100

    web = Web(default=lambda req: httpx.Response(200, content=sans_fin()))
    assert web.ocr("http://site.test/f") == (
        "Error: http://site.test/f is larger than 300 bytes, which this tool "
        "does not download.")
    assert len(servis) == 3
    # Une image téléchargée mais trop grosse pour le modèle de vision.
    monkeypatch.setattr(ocr, "MAX_IMAGE_BYTES", 10)
    web = Web(default=fichier(PNG))
    assert web.ocr("http://site.test/f") == (
        "Error: http://site.test/f is an image larger than 10 bytes, which "
        "this tool does not read.")
    assert web.last.error == "unsupported" and env.seen == []


def test_ocr_non_configure_repond_sans_rien_telecharger(env, monkeypatch):
    web = Web(default=fichier(PNG))

    def essai(nom):
        assert web.ocr("http://site.test/i") == (
            "Error: ocr is not configured on this proxy."), nom
        assert web.last.error == "unavailable", nom

    for model in ["", "lecteur", "inconnu/lecteur"]:
        monkeypatch.setattr(ocr, "MODEL", model)
        essai(model)
    monkeypatch.setattr(ocr, "MODEL", "vision/lecteur")
    # `images = false` sur le backend : jamais d'image, OCR compris.
    monkeypatch.setattr(env.backend, "images", False)
    essai("images = false")
    monkeypatch.setattr(env.backend, "images", True)
    # Le catalogue du backend, quand il est connu, dit ce que voit le modèle.
    env.backend.model_types = {"lecteur": "text-generation"}
    essai("modèle texte seul")
    env.backend.model_types = {"lecteur": "image-text-to-text"}
    assert web.ocr("http://site.test/i").endswith("texte lu")
    # Le client du backend n'est ouvert qu'au démarrage de l'application.
    monkeypatch.setattr(env.backend, "client", None)
    essai("client fermé")
    assert len(web.requests) == 1


# ── PDF : les images des pages ──────────────────────────────────────────

def test_ocr_pdf_une_requete_par_page_avec_ses_images(env):
    """Un JPEG est repris tel quel ; des pixels bruts sont réécrits en
    PNG ; une page découpée en bandes part en une requête ; un masque ou
    une vignette n'est pas une page."""
    gris = bytes(range(64)) * 64          # 64 × 64, 8 bits
    doc = scan(
        [image_pdf(data=jpeg(1))],
        [image_pdf(64, 64, filtre=None, data=gris)],
        [image_pdf(data=jpeg(3)), image_pdf(data=JPEG + b" bande 2"),
         image_pdf(16, 16, data=JPEG + b" logo"),
         image_pdf(data=JPEG + b" masque", extra=b"/ImageMask true")])
    lit(env, 3)
    env.default = "Texte de la page 2"
    web = Web(default=fichier(doc, ct="application/pdf"))
    out = web.ocr("http://site.test/scan.pdf")
    assert out == (
        "URL: http://site.test/scan.pdf\nContent-Type: application/pdf\n"
        "Pages: 1-3 of 3\n\n---\n"
        "[Page 1]\nTexte de la page 1\n\n[Page 2]\nTexte de la page 2\n\n"
        "[Page 3]\nTexte de la page 3")
    assert web.last.meta["pages"] == [1, 2, 3]
    assert web.last.meta["total_pages"] == 3
    assert ocr.TOOL.summary({"url": "u"}, web.last)["url"] == "u [pages 1-3]"
    par_page = {images[0][1]: images for images in env.images}
    assert len(env.images) == 3 and len(env.lines) == 3
    assert par_page[jpeg(1)] == [("image/jpeg", jpeg(1))]
    assert [d for _, d in par_page[jpeg(3)]] == [jpeg(3), JPEG + b" bande 2"]
    # La page 2 : un PNG dont les lignes sont celles du PDF, précédées de
    # l'octet de filtre.
    (kind, png), = [i[0] for i in env.images if i[0][0] == "image/png"]
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png[16:26] == b"\x00\x00\x00\x40\x00\x00\x00\x40\x08\x00"
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    assert zlib.decompress(idat) == b"".join(
        b"\0" + gris[y * 64:(y + 1) * 64] for y in range(64))
    # RVB 8 bits et gris 1 bit se réécrivent aussi (type de couleur, bits).
    for space, bits, data, ihdr in [
        (b"/DeviceRGB", 8, b"\x01\x02\x03" * 64 * 64, b"\x08\x02"),
        (b"/DeviceGray", 1, b"\xaa" * 8 * 64, b"\x01\x00"),
    ]:
        env.images.clear()
        Web(default=fichier(scan([image_pdf(64, 64, space, bits, None, data)]))
            ).ocr("http://site.test/p")
        assert env.images[0][0][1][24:26] == ihdr, space


def test_ocr_pdf_pages_choisies_et_suite_a_redemander(env, monkeypatch):
    lit(env, 6)
    web = Web(default=fichier(scan_jpeg(6)))
    u = "http://site.test/scan.pdf"
    monkeypatch.setattr(ocr, "MAX_PAGES", 2)
    # Sans `pages` : les premières, et comment demander les suivantes.
    out = web.ocr(u)
    assert "\nPages: 1-2 of 6 (pass pages=\"3-6\" to continue)\n" in out
    assert out.endswith("[Page 2]\nTexte de la page 2")
    assert len(env.seen) == 2
    # `pages` : dans l'ordre demandé, au plus MAX_PAGES par appel.
    out = web.ocr(u, pages="5,2-3")
    assert "\nPages: 5,2 of 6 (pass pages=\"3\" to continue)\n" in out
    assert out.endswith("---\n[Page 5]\nTexte de la page 5\n\n"
                        "[Page 2]\nTexte de la page 2")
    assert web.ocr(u, pages=6).endswith("Pages: 6 of 6\n\n---\n[Page 6]\n"
                                        "Texte de la page 6")
    # Au-delà de la dernière page : ce qui existe est lu, sinon une erreur.
    assert "\nPages: 6 of 6\n" in web.ocr(u, pages="6-9")
    assert web.ocr(u, pages="7-9") == (
        f"Error: {u} has 6 pages: `pages` (7-9) is out of range.")
    assert web.last.error == "invalid_input"
    # Une plage illisible n'est pas devinée, et rien n'est téléchargé.
    n = len(web.requests)
    for pages in ["0", "3-1", "deux", "1;2", "-3", ["1"], 1.5]:
        assert web.ocr(u, pages=pages).startswith("Error: `pages` must be"), pages
    assert len(web.requests) == n
    # Trop long pour `max_chars` : les pages qui tiennent, la suite à
    # redemander ; une page seule trop longue est coupée, et le dit. Le
    # client (call.settings) peut abaisser la taille, pas la relever.
    monkeypatch.setattr(ocr, "MAX_PAGES", 4)
    monkeypatch.setattr(ocr, "MAX_CHARS", 40)
    out = web.ocr(u)
    assert "\nPages: 1-2 of 6 (pass pages=\"3-6\" to continue)\n" in out
    out = web.ocr(u, tools.Call({"max_chars": 10}))
    assert "\nPages: 1 of 6 (pass pages=\"2-6\" to continue)\n" in out
    assert out.endswith("[Page 1]\nTexte de l\n[transcription cut: the page is "
                        "longer than the output limit]")
    assert "Pages: 1-2 of 6" in web.ocr(u, tools.Call({"max_chars": 10 ** 6}))


def test_ocr_pdf_sans_image_lisible(env):
    """Codages de fax, espace de couleur non converti, page sans image :
    la page est sautée en le disant ; s'il ne reste rien, une erreur.
    Chiffré, abîmé : une erreur aussi, jamais une exception."""
    fax = image_pdf(filtre=b"/CCITTFaxDecode", data=b"fax")
    doc = scan([fax], [image_pdf(data=jpeg(2))], [],
               [image_pdf(64, 64, b"/DeviceCMYK", 8, None, b"\0" * 64 * 64 * 4)])
    lit(env, 2)
    out = Web(default=fichier(doc)).ocr("http://site.test/d.pdf")
    assert out.endswith(
        "Pages: 2 of 4\n\n---\n"
        "[Page 1: not read, its image is encoded as CCITT fax]\n\n"
        "[Page 2]\nTexte de la page 2\n\n"
        "[Page 3: not read, it holds no image]\n\n"
        "[Page 4: not read, its image is in a colour space this tool does "
        "not convert]")
    assert len(env.seen) == 1
    web = Web(default=fichier(scan([fax], [])))
    assert web.ocr("http://site.test/d.pdf") == (
        "Error: http://site.test/d.pdf has no scanned page this tool can read "
        "on pages 1-2 (page 1: its image is encoded as CCITT fax; page 2: it "
        "holds no image). This tool reads the images of a PDF, not its text "
        "layer.")
    assert web.last.error == "unsupported"
    for nom, corps, attendu in [
        ("coupé", doc[:len(doc) // 2], "is not a readable PDF"),
        ("en-tête seul", b"%PDF-1.7\n", "is not a readable PDF"),
    ]:
        web = Web(default=fichier(corps))
        assert web.ocr("http://site.test/d.pdf").startswith(
            "Error: http://site.test/d.pdf " + attendu), nom
        assert web.last.error == "unsupported", nom
    assert len(env.seen) == 1


def test_ocr_pdf_ouvert_hors_de_la_boucle_asyncio(env, monkeypatch):
    fils, vrai = [], ocr.pdf_images

    def espion(*a):
        fils.append(threading.current_thread())
        return vrai(*a)
    monkeypatch.setattr(ocr, "pdf_images", espion)
    Web(default=fichier(scan_jpeg(1))).ocr("http://site.test/d.pdf")
    assert len(fils) == 1 and fils[0] is not threading.main_thread()


# ── le modèle de vision en échec, en retard ─────────────────────────────

def test_ocr_modele_de_vision_en_echec_rendu_en_texte(env):
    web = Web(default=fichier(PNG))
    for nom, dit, code, attendu, statut in [
        ("éteint", httpx.ConnectError("refusé"), "unavailable",
         "Error: the OCR model is unreachable (ConnectError).", 503),
        ("500", httpx.Response(500, text="SECRET du backend"), "unavailable",
         "Error: the OCR model returned HTTP 500.", 500),
        ("429", httpx.Response(429), "too_many_requests",
         "Error: the OCR model returned HTTP 429.", 429),
        ("pas du JSON", httpx.Response(200, text="<html>"), "unavailable",
         "Error: unreadable answer from the OCR model.", 200),
        ("autre forme", httpx.Response(200, json={"choices": []}),
         "unavailable", "Error: unreadable answer from the OCR model.", 200),
        ("muet", answer(""), "unavailable",
         "Error: the OCR model returned no text.", 200),
    ]:
        env.texts[PNG] = dit
        assert web.ocr("http://site.test/i") == attendu, nom
        assert web.last.error == code, nom
        # Chaque requête partie a sa ligne, avec le statut de son issue.
        assert env.lines[-1][4] == statut, nom
    assert len(env.lines) == 6


def test_ocr_pages_lues_rendues_quand_une_autre_echoue_ou_tarde(env, monkeypatch):
    """Une page en échec ou pas finie au délai : celles d'avant sont
    rendues, la suite est à redemander — et ce qui a été lu après reste
    en cache."""
    monkeypatch.setattr(webcache.CACHE, "ttl", 600)
    lit(env, 3)
    web = Web(default=fichier(scan_jpeg(3)))
    env.texts[jpeg(2)] = httpx.Response(500)
    out = web.ocr("http://site.test/a.pdf")
    assert "\nPages: 1 of 3 (page 2 failed: the OCR model returned HTTP 500; " \
           "pass pages=\"2-3\" to continue)\n" in out
    assert out.endswith("---\n[Page 1]\nTexte de la page 1")
    assert web.last.error is None
    # La suite : seule la page en échec est relue, la 3 sort du cache.
    env.texts[jpeg(2)] = "Texte de la page 2"
    n = len(env.seen)
    assert web.ocr("http://site.test/a.pdf", pages="2-3").endswith(
        "[Page 2]\nTexte de la page 2\n\n[Page 3]\nTexte de la page 3")
    assert len(env.seen) == n + 1 and len(web.requests) == 1
    # Le délai : la page lente est abandonnée (ligne de statistiques 504).
    monkeypatch.setattr(ocr, "TIMEOUT", 0.2)
    lente = asyncio.sleep

    async def tarde():
        await lente(5)
    env.texts[jpeg(2)] = tarde
    out = web.ocr("http://site.test/b.pdf")
    assert "\nPages: 1 of 3 (pass pages=\"2-3\" to continue)\n" in out
    assert [line[4] for line in env.lines[-3:]].count(504) == 1
    # Rien de lu à temps : une erreur `timeout`.
    env.texts[jpeg(1)] = tarde
    assert web.ocr("http://site.test/c.pdf") == (
        "Error: the OCR model did not answer within 0 s.")
    assert web.last.error == "timeout"
    # La première page en échec : son erreur, avec son code.
    env.texts[jpeg(1)] = httpx.Response(429)
    assert web.ocr("http://site.test/d.pdf") == (
        "Error: the OCR model returned HTTP 429.")
    assert web.last.error == "too_many_requests"


# ── cache, quotas ───────────────────────────────────────────────────────

def test_ocr_cache_ni_retelechargement_ni_relecture(env, monkeypatch):
    monkeypatch.setattr(webcache.CACHE, "ttl", 600)
    lit(env, 3)
    web = Web({"/a.pdf": fichier(scan_jpeg(3)), "/i.png": fichier(PNG),
               "/x": fichier(b"<html>"), "/muet.png": fichier(GIF)})
    premier = web.ocr("http://site.test/a.pdf")
    assert web.ocr("http://site.test/a.pdf#page=2") == premier
    assert web.ocr("http://site.test/a.pdf", pages="3").endswith(
        "[Page 3]\nTexte de la page 3")
    assert len(web.requests) == 1 and len(env.seen) == 3
    # Une page lue l'est sous SON modèle : un autre modèle la relit.
    backends.BACKENDS["vision"].model_types = {}
    monkeypatch.setattr(ocr, "MODEL", "vision/autre")
    web.ocr("http://site.test/a.pdf", pages="1")
    assert len(web.requests) == 1 and len(env.seen) == 4
    # Les listes de domaines passent AVANT le cache.
    monkeypatch.setattr(ocr.net, "BLOCKED_DOMAINS", ["site.test"])
    assert web.ocr("http://site.test/a.pdf").startswith("Error: site.test is")
    monkeypatch.setattr(ocr.net, "BLOCKED_DOMAINS", [])
    # Un échec n'est jamais gardé : ni le fichier refusé, ni la lecture.
    env.texts[GIF] = answer("")
    for chemin in ("/x", "/muet.png"):
        n = len(web.requests)
        assert web.ocr("http://site.test" + chemin).startswith("Error:")
        assert web.ocr("http://site.test" + chemin).startswith("Error:")
        assert len(web.requests) >= n + 1, chemin
    assert not any(k[0] == "ocr" and k[1].endswith("muet.png")
                   for k in webcache.CACHE._data)
    assert ("ocr-file", "http://site.test/x") not in webcache.CACHE._data


def test_ocr_passe_par_le_limiteur_du_backend_a_quotas(env, monkeypatch):
    """Un backend à quotas : chaque requête au modèle de vision prend sa
    place au limiteur — celui que désigne le modèle SANS préfixe —, pour
    un coût qui ne dépend pas du poids de l'image ; quota épuisé, rien ne
    part au backend."""
    pris = []

    class Limiteur:
        attente = None

        async def acquire(self, cost, wait_gone=None):
            pris.append(cost)
            if self.attente:
                raise albert.QuotaWaitTooLong(self.attente, "minute")
            return 0.0

    limiteur = Limiteur()
    monkeypatch.setattr(env.backend, "quotas", True)
    monkeypatch.setattr(env.backend, "quota_state", type("Etat", (), {
        "get_limiter": lambda self, payload: (
            pris.append(payload["model"]), limiteur)[1]})())
    web = Web(default=fichier(scan([image_pdf(data=jpeg(1)),
                                    image_pdf(data=b"\xff\xd8\xff" * 100_000)])))
    assert web.ocr("http://site.test/a.pdf").endswith("texte lu")
    cout = len(ocr.PROMPT) // 4 + 2 * ocr.IMAGE_TOKENS
    assert pris == ["lecteur", cout] and len(env.seen) == 1
    limiteur.attente = 75.4
    assert web.ocr("http://site.test/b.pdf") == (
        "Error: the OCR model's quota is exhausted; retry in about 75 s.")
    assert web.last.error == "too_many_requests"
    assert len(env.seen) == 1 and len(env.lines) == 1
    # Un backend sans quota ne consulte aucun limiteur.
    monkeypatch.setattr(env.backend, "quotas", False)
    pris.clear()
    web.ocr("http://site.test/c.pdf")
    assert pris == []


# ── la fonction présentée, et l'outil derrière l'exécuteur ──────────────

def test_ocr_fonction_presentee_et_renvoi_depuis_web_fetch():
    seul = ocr.TOOL.spec(frozenset({"ocr"}))["function"]
    assert seul["name"] == ocr.TOOL.name == "ocr"
    assert seul["parameters"]["required"] == ["url"]
    assert set(seul["parameters"]["properties"]) == {"url", "pages"}
    # Le renvoi depuis `web_fetch` : seulement là où il est présenté aussi.
    avec = ocr.TOOL.spec(frozenset({"ocr", "web_fetch"}))["function"]
    assert "web_fetch" not in seul["description"]
    assert "web_fetch reports an image, or a PDF with no extractable text" \
        in avec["description"]
    # Sans liaison de protocole : déclaré par son nom sur chat/completions.
    assert ocr.TOOL.kinds == ("ocr",)
    assert ocr.TOOL.responses is None and ocr.TOOL.anthropic is None
    assert ocr.TOOL.summary({"url": "http://site.test/i"}) == {
        "type": "ocr", "url": "http://site.test/i"}


def test_ocr_plages_de_pages():
    for value, pages in [(None, None), ("", None), ("3", [3]), (3, [3]),
                         (" 1 - 3 ", [1, 2, 3]), ("2,5-7,2", [2, 5, 6, 7])]:
        assert ocr.parse_pages(value) == pages, value
    assert len(ocr.parse_pages("1-999999")) == 10_001
    for pages, text in [([1, 2, 3, 7], "1-3,7"), ([5, 2], "5,2"), ([4], "4"),
                        ([], "")]:
        assert ocr.spans(pages) == text, pages


def test_ocr_derriere_l_executeur(env, monkeypatch):
    """Par tools.Hosted, comme sur toutes les routes : l'échec prévu
    devient un Result avec son code, le succès garde son texte."""
    h = tools.Hosted([ocr.TOOL], tools.Memory(4, 60))
    web = Web(default=fichier(PNG))
    transport = httpx.MockTransport(web.handler)
    vrai = ocr.download
    monkeypatch.setattr(ocr, "download", lambda url, settings, _: vrai(
        url, settings, transport))
    ok = go(h.run("ocr", json.dumps({"url": "http://site.test/i"}), 0))
    assert ok.error is None and ok.text.endswith("\n---\ntexte lu")
    refus = go(h.run("ocr", json.dumps({"url": "http://10.0.0.1/i"}), 0))
    assert refus.error == "not_allowed" and refus.text.startswith("Error: ")
    # Son délai chez l'exécuteur : son propre budget (où il rend ce qu'il
    # a lu) plus une marge — à la place de [tools].run_timeout.
    monkeypatch.setattr(ocr, "TIMEOUT", 200)
    assert ocr.TOOL.timeout == 200 + ocr.GRACE and ocr.TOOL.max_calls is None


def test_ocr_au_registre_et_renvoi_de_web_fetch_vers_ocr(monkeypatch):
    """Enregistré après web_fetch, dont il est le recours ; web_fetch y
    renvoie le modèle là où `ocr` lui est présenté, et là seulement. Sans
    liaison : ignoré des surfaces Responses et Anthropic."""
    from llm_proxy import anthropic_api, responses_api
    from llm_proxy.tools import web_fetch
    names = [t.name for t in tools.REGISTRY]
    assert names.index("ocr") == names.index("web_fetch") + 1
    assert "ocr" in tools.kinds()
    seul = web_fetch.TOOL.spec(frozenset({"web_fetch"}))["function"]
    avec = web_fetch.TOOL.spec(frozenset({"web_fetch", "ocr"}))["function"]
    assert "ocr" not in seul["description"]
    assert ("For an image, or a PDF with no extractable text (a scan), use "
            "ocr with the same URL. ") in avec["description"]
    monkeypatch.setattr(ocr, "ENABLED", True)
    h = tools.Hosted()
    assert h.for_kind("ocr") == [ocr.TOOL]
    assert ocr.TOOL not in h.for_responses("web_search")
    assert h.for_server("ocr_20260101") is None
    ctx = responses_api.to_chat({"model": "m", "input": "x", "tools": [
        {"type": "ocr"}]}, hosted=h)[1]
    assert "ocr" not in ctx.hosted and ctx.ignored == ["ocr"]
    assert not anthropic_api.Context({"tools": [
        {"type": "ocr_20260101", "name": "ocr"}]}, h).hosted
