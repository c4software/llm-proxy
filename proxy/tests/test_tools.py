"""Les outils hébergés (llm_proxy/tools/) : garde-fou réseau, lecture de
page, recherche, HTML → texte, mémoire et exécution bornée. Sur des
octets, SANS réseau : HTTP passe par httpx.MockTransport, le DNS par une
table (toute adresse hors table est refusée avant de quitter la machine).
La configuration est posée par monkeypatch sur les constantes des
modules, jamais lue dans config.example.toml.

Un test par comportement : les familles d'entrées sont des tables
parcourues dans le test, une entrée par classe (plus celles qui ont déjà
révélé un défaut), et l'assertion nomme l'entrée fautive."""

import asyncio
import inspect
import io
import os
import socket
import threading

import conftest
import httpx
import pytest

from fakes import (FOUND, RESULTS, SEARCHED, Echo, FakeUpstream, chat_doc,
                   outil)
from llm_proxy import tools
from llm_proxy.tools import html_text, net, web_fetch, web_search, webcache

PUBLIC = "93.184.216.34"
PUBLIC6 = "2606:4700:4700::1111"
T = html_text.html_to_text


def go(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Résolution sans réseau. La table donne nom → adresses (ou None :
    introuvable ; ou une fonction, pour un nom qui change de réponse).
    Hors table, seul un littéral NUMÉRIQUE est résolu, par le vrai
    getaddrinfo et AI_NUMERICHOST (aucune requête DNS) : c'est lui qui
    dit ce que valent «2130706433» ou «0x7f.1»."""
    table = {
        "site.test": [PUBLIC],
        "site6.test": [PUBLIC6],
        "autre.test": ["1.1.1.1"],
        "intern.test": ["10.0.0.5"],
        "mixte.test": [PUBLIC, "192.168.1.10"],
        "absent.test": None,
    }
    real = socket.getaddrinfo
    seen = []

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        seen.append(host)
        if host in table:
            ips = table[host]
            if callable(ips):
                ips = ips()
            if ips is None:
                raise socket.gaierror(socket.EAI_NONAME, "introuvable")
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                     socket.SOCK_STREAM, 6, "",
                     (ip, port, 0, 0) if ":" in ip else (ip, port))
                    for ip in ips]
        return real(host, port, family, type, proto,
                    flags | socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    table["__seen__"] = seen
    return table


@pytest.fixture(autouse=True)
def reglages(monkeypatch):
    """Les réglages dont dépendent les tests, quels que soient ceux du
    fichier de configuration chargé à l'import."""
    monkeypatch.setattr(net, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 2_000_000)
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 20_000)
    monkeypatch.setattr(web_fetch, "PDF_MAX_BYTES", 20_000_000)
    monkeypatch.setattr(web_fetch, "PDF_MAX_PAGES", 500)
    # Cache web vidé et COUPÉ : les tests resservent les mêmes URL avec des
    # pages différentes. Ceux du cache le rallument (ttl).
    webcache.CACHE.clear()
    monkeypatch.setattr(webcache.CACHE, "ttl", 0)
    monkeypatch.setattr(net, "ALLOWED_DOMAINS", [])
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", [])
    monkeypatch.setattr(web_search, "SEARXNG_URL", "http://searx.test:8080")
    monkeypatch.setattr(web_search, "LIMIT", 8)
    monkeypatch.setattr(web_search, "LANGUAGE", "")
    monkeypatch.setattr(web_search, "CATEGORIES", "")
    monkeypatch.setattr(tools, "MAX_CALLS", 8)
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 60)
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 24_000)


def rendu(coro) -> tools.Result:
    """Ce que l'exécuteur (tools.Hosted) fait d'un `run` appelé à la
    main : son Result, ou celui de l'échec prévu (ToolError)."""
    try:
        return go(coro)
    except tools.ToolError as exc:
        return tools.failure(exc.code, exc.message)


def blocked(url, **kw):
    """Le message du refus ; échoue en nommant l'URL si elle est acceptée."""
    try:
        go(net.public_target(url, **kw))
    except net.Blocked as exc:
        return str(exc)
    pytest.fail(f"cible acceptée : {url!r}")


class Web:
    """Un faux web : `pages[(hôte, chemin)]` → réponse (ou fonction).
    Garde chaque requête reçue, pour vérifier où elle est partie."""

    def __init__(self, pages=None, default=None):
        self.pages, self.default, self.requests = pages or {}, default, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.headers["host"], request.url.path)
        page = self.pages.get(key, self.default)
        if page is None:
            return httpx.Response(404, text="rien")
        return page(request) if callable(page) else page

    def fetch(self, url, **args) -> str:
        """Le texte rendu au modèle ; le résultat entier reste dans `last`."""
        self.last = rendu(web_fetch.TOOL.run(
            {"url": url, **args}, tools.Call(),
            transport=httpx.MockTransport(self.handler)))
        return self.last.text


def page(text="bonjour", ct="text/plain", status=200, **headers):
    h = dict(headers)
    if ct is not None:
        h["content-type"] = ct
    return httpx.Response(status, content=text.encode() if isinstance(text, str)
                          else text, headers=h)


def redirect(location, status=302):
    return httpx.Response(status, headers={"location": location})


def pdf(*pages, password=None):
    """Un PDF écrit à la main, une page par texte (ASCII ; «» : une page
    sans couche texte, comme un scan). `password` : chiffré par pypdf, en
    RC4 — le seul algorithme qu'il porte sans autre dépendance."""
    n = len(pages)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count %d /Kids [%s] >>" % (
                n, b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(n))),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        flux = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text.encode("ascii") \
            if text else b""
        objs += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                 b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
                 % (5 + 2 * i),
                 b"<< /Length %d >>\nstream\n%s\nendstream" % (len(flux), flux)]
    out, places = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objs, 1):
        places.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % place for place in places)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    if password is None:
        return bytes(out)
    import pypdf
    writer = pypdf.PdfWriter(clone_from=io.BytesIO(bytes(out)))
    writer.encrypt(password, "proprio", algorithm="RC4-128")
    chiffre = io.BytesIO()
    writer.write(chiffre)
    return chiffre.getvalue()


def search(args, reponse=None):
    """(texte rendu, requêtes reçues par le faux SearXNG). `reponse` : le
    JSON servi en 200, une httpx.Response, ou une fonction."""
    requests = []

    def handler(request):
        requests.append(request)
        if callable(reponse):
            return reponse(request)
        if isinstance(reponse, httpx.Response):
            return reponse
        return httpx.Response(200, json=reponse or {"results": []})

    out = rendu(web_search.TOOL.run(args, tools.Call(),
                                    transport=httpx.MockTransport(handler)))
    return out.text, requests


def res(n, **kw):
    return {"url": f"https://r.test/{n}", "title": f"Titre {n}",
            "content": f"extrait {n}", **kw}


def hosted(*outils):
    return tools.Hosted(list(outils), tools.Memory(4, 60))


def texte(h, *a, **kw) -> str:
    """Le texte que rend Hosted.run."""
    return go(h.run(*a, **kw)).text


# ── net ─────────────────────────────────────────────────────────────────

def test_is_public_ne_garde_que_les_adresses_routables():
    for ip in ["10.0.0.1", "172.16.0.1", "192.168.0.1", "127.0.0.1", "0.0.0.0",
               "169.254.169.254",                       # métadonnées cloud
               "100.64.0.1", "100.127.255.254",         # CGNAT, Tailscale
               "224.0.0.1", "192.0.2.1", "240.0.0.1",   # multicast, réservées
               "::1", "::", "fe80::1", "fc00::1", "2001:db8::1",
               "ff0e::1",                               # multicast « global »
               "2002:a00:1::1",                         # 6to4 autour de 10.0.0.1
               "", "pas-une-ip", "10.0.0.1/8", " 8.8.8.8"]:
        assert net.is_public(ip) is False, ip
    # Publiques, bords des plages privées compris.
    for ip in ["8.8.8.8", PUBLIC, "100.63.255.255", "100.128.0.1",
               "172.15.255.255", "172.32.0.1", PUBLIC6]:
        assert net.is_public(ip) is True, ip


def test_is_public_juge_l_ipv4_emballee_dans_une_ipv6():
    """NAT64, « compatibles IPv4 » et mappées : `is_global` seul les tient
    pour publiques alors qu'elles désignent 10.0.0.1 ou les métadonnées."""
    for ip in ["64:ff9b::a00:1", "64:ff9b::a9fe:a9fe", "::10.0.0.1",
               "::ffff:10.0.0.1", "::ffff:169.254.169.254"]:
        assert net.is_public(ip) is False, ip
    for ip in ["64:ff9b::808:808", "::ffff:8.8.8.8"]:
        assert net.is_public(ip) is True, ip


def test_public_target_rend_schema_adresse_verifiee_et_port():
    for url, attendu in [
        ("http://site.test/x", ("http", PUBLIC, 80)),
        ("HTTPS://SITE.TEST/", ("https", PUBLIC, 443)),
        ("https://site.test:0/", ("https", PUBLIC, 443)),   # port 0 : celui du schéma
        # Les identifiants ne changent pas l'hôte résolu.
        ("http://user:pass@site.test:81/", ("http", PUBLIC, 81)),
        (f"https://[{PUBLIC6}]:8443/x", ("https", PUBLIC6, 8443)),
        # 8.8.8.8 en entier décimal : c'est bien 8.8.8.8 qui est joint.
        ("http://134744072/", ("http", "8.8.8.8", 80)),
    ]:
        assert go(net.public_target(url)) == attendu, url


def test_public_target_refuse_avant_toute_resolution(dns):
    """Schéma autre que http(s), hôte absent, URL que urlsplit ou `.port`
    refusent (ValueError) : un Blocked, et rien n'est résolu."""
    for url in ["file:///etc/passwd", "ftp://site.test/", "javascript:alert(1)",
                "site.test/chemin", "//site.test/", "http://", "https://:443/", "",
                "http://site.test:99999/", "http://site.test:abc/", "http://[::1/"]:
        blocked(url)
        assert dns["__seen__"] == [], url


def test_public_target_exige_que_toutes_les_adresses_soient_publiques(dns):
    dns["mixte6.test"] = ["::1", PUBLIC]
    dns["vide.test"] = []
    # UNE adresse privée suffit, que la première soit publique ou non.
    for url in ["http://intern.test/", "http://mixte.test/", "http://mixte6.test/"]:
        assert "private or local address" in blocked(url), url
    for url in ["http://absent.test/", "http://jamais-vu.test/", "http://vide.test/"]:
        assert "host not found" in blocked(url), url
    # Nom tordu : octet nul, espace, étiquette vide, hôte en %XX.
    for url in ["http://a\x00b.test/", "http://exa mple.test/", "http://a..b.test/",
                "http://%31%32%37.0.0.1/"]:
        blocked(url)


def test_public_target_adresse_non_publique_sous_toutes_ses_ecritures():
    for url in [
        "http://127.0.0.1:8009/v1/models", "http://169.254.169.254/latest/",
        "http://[::1]:8080/", "http://[fe80::1%25eth0]/", "http://[::ffff:7f00:1]/",
        "http://[64:ff9b::10.0.0.1]/",
        # 127.0.0.1 selon inet_aton : entier décimal, hexadécimal, octal,
        # forme courte, point final.
        "http://2130706433/", "http://0x7f.1/", "http://0177.0.0.1/",
        "http://127.1/", "http://0/", "http://127.0.0.1./",
        # Chiffres et points Unicode, que la résolution replie sur l'ASCII.
        "http://１２７.０.０.１/", "http://127。0。0。1/",
        # Un nom public placé en « identifiant » ne masque pas la cible.
        "http://site.test@127.0.0.1/", "http://site.test:80@10.0.0.1:8009/",
        "http://site.test\\@127.0.0.1/", "http://127.0.0.1#@site.test/",
    ]:
        blocked(url)


# ── web_fetch : où part la requête ──────────────────────────────────────

def test_fetch_se_connecte_a_l_adresse_verifiee_sous_le_nom_demande(dns):
    web = Web(default=page("contenu"))
    out = web.fetch("http://user:secret@site.test/a/b?x=1&y=é#fragment")
    assert out.startswith("URL: http://user:secret@site.test/a/b?x=1&y=é#fragment\n")
    assert out.endswith("\n---\ncontenu")
    (req,) = web.requests
    assert req.method == "GET"
    assert str(req.url) == f"http://{PUBLIC}/a/b?x=1&y=%C3%A9"
    assert req.headers["host"] == req.extensions["sni_hostname"] == "site.test"
    assert req.headers["user-agent"] == web_fetch.USER_AGENT
    # Les identifiants de l'URL ne partent pas.
    assert "authorization" not in req.headers and not req.url.userinfo
    # Une seule résolution : celle qui est contrôlée est celle qui est jointe.
    assert dns["__seen__"] == ["site.test"]


def test_fetch_cible_et_host_selon_port_chemin_et_ipv6(dns):
    dns["www.site.test"] = dns["exämple.test"] = [PUBLIC]
    web = Web(default=page())
    for url, cible, host in [
        ("http://site.test:8080/p", f"http://{PUBLIC}:8080/p", "site.test:8080"),
        # 443 n'est pas le port par défaut de http.
        ("http://site.test:443/p", f"http://{PUBLIC}:443/p", "site.test:443"),
        ("http://site.test?q=1", f"http://{PUBLIC}/?q=1", "site.test"),
        ("http://site6.test:8080/x", f"http://[{PUBLIC6}]:8080/x", "site6.test:8080"),
        # Une IPv6 littérale garde ses crochets dans Host.
        (f"https://[{PUBLIC6}]:8443/x", f"https://[{PUBLIC6}]:8443/x",
         f"[{PUBLIC6}]:8443"),
        # «www.» sans schéma : lu en https.
        ("  www.site.test/doc  ", f"https://{PUBLIC}/doc", "www.site.test"),
        # Un Host non ASCII faisait lever UnicodeEncodeError par httpx.
        ("http://exämple.test/", f"http://{PUBLIC}/", "xn--exmple-cua.test"),
    ]:
        assert not web.fetch(url).startswith("Error"), url
        req = web.requests[-1]
        assert (str(req.url), req.headers["host"]) == (cible, host), url
    assert req.extensions["sni_hostname"] == "xn--exmple-cua.test"


def test_fetch_cible_absente_ou_refusee_erreur_sans_aucune_requete():
    web = Web(default=page("SECRET"))
    for url in ["", "   ", None, 5, ["http://site.test/"]]:
        assert web.fetch(url) == "Error: `url` is required.", url
    # Un par motif de refus (les écritures d'adresses : tests de net).
    for url in ["site.test", "file:///etc/passwd", "http://site.test:99999/",
                "http://127.0.0.1/", "http://[64:ff9b::a00:1]/", "http://mixte.test/",
                "http://site.test@10.0.0.1/", "http://absent.test/"]:
        out = web.fetch(url)
        assert out.startswith("Error:") and "SECRET" not in out, url
    # Caractère de contrôle, URL trop longue : httpx.InvalidURL n'est pas
    # une HTTPError, elle sortait de run().
    assert "InvalidURL" in web.fetch("http://site.test/a\x01b")
    assert web.fetch("http://site.test/" + "a" * 70_000).startswith("Error:")
    assert web.requests == []


def test_fetch_allow_private(monkeypatch):
    monkeypatch.setattr(net, "ALLOW_PRIVATE", True)
    web = Web(default=page("interne"))
    assert web.fetch("http://intern.test:8009/").endswith("interne")
    assert str(web.requests[0].url) == "http://10.0.0.5:8009/"
    assert web.fetch("file:///etc/passwd").startswith("Error:")


# ── web_fetch : redirections ────────────────────────────────────────────

def test_fetch_suit_les_redirections_vers_une_cible_publique():
    for status in (301, 302, 303, 307, 308):
        web = Web({("site.test", "/"): redirect("https://autre.test:8443/p", status),
                   ("autre.test:8443", "/p"): page("là")})
        out = web.fetch("http://site.test/")
        assert out.startswith("URL: https://autre.test:8443/p\n"), status
        # Le nouveau saut est joint à SON adresse, sous SON nom.
        assert str(web.requests[1].url) == "https://1.1.1.1:8443/p", status
        assert web.requests[1].extensions["sni_hostname"] == "autre.test"
    web = Web({("site.test:8080", "/a/c"): redirect("../b?x=1"),
               ("site.test:8080", "/b"): page("fin"),
               # Pas suivis : 3xx sans Location, statut hors liste.
               ("site.test", "/a"): page("corps", status=302),
               ("site.test", "/b"): page("choix", status=300,
                                         location="http://intern.test/")})
    # Location relatif : même hôte, même port.
    assert web.fetch("http://site.test:8080/a/c") == (
        "URL: http://site.test:8080/b?x=1\nContent-Type: text/plain\n\n---\nfin")
    assert str(web.requests[1].url) == f"http://{PUBLIC}:8080/b?x=1"
    assert web.fetch("http://site.test/a").endswith("corps")
    assert web.fetch("http://site.test/b").endswith("choix")
    assert len(web.requests) == 4


def test_fetch_redirection_recontrolee_a_chaque_saut():
    """Le garde-fou s'applique au Location comme à l'URL de départ : rien
    ne part ailleurs que vers l'hôte public d'origine."""
    def suivi(location, status=302):
        web = Web({("site.test", "/"): redirect(location, status)},
                  default=page("SECRET"))
        return web.fetch("http://site.test/"), web.requests

    for status in (301, 302, 303, 307, 308):
        out, requests = suivi("http://intern.test/admin", status)
        assert out == ("Error: intern.test is a private or local address, "
                       "which this proxy does not read."), status
        assert len(requests) == 1, status
    for location in ["http://127.0.0.1:8009/v1/models", "http://[64:ff9b::a00:1]/",
                     "http://2130706433/", "//intern.test/x", "http://mixte.test/",
                     "http://site.test@10.0.0.1/", "http://absent.test/",
                     "file:///etc/passwd", "http://site.test:99999/", "http://[::1"]:
        out, requests = suivi(location)
        assert out.startswith("Error:") and "SECRET" not in out, location
        assert len(requests) == 1, location
    # Antislashs : un chemin relatif pour urljoin, on reste sur l'hôte.
    for location in ["\\\\127.0.0.1/x", "http:\\\\127.0.0.1\\x"]:
        assert all(r.url.host == PUBLIC and r.headers["host"] == "site.test"
                   for r in suivi(location)[1]), location


def test_fetch_listes_de_domaines_a_chaque_saut(monkeypatch, dns):
    """`allowed_domains` : seuls ces domaines sont lus ; `blocked_domains` :
    jamais. Les sous-domaines suivent, et une redirection n'en sort pas."""
    web = Web({("site.test", "/sortie"): redirect("http://autre.test/x")},
              default=page("ok"))
    dns["docs.site.test"] = [PUBLIC]
    monkeypatch.setattr(net, "ALLOWED_DOMAINS", ["site.test"])
    assert web.fetch("http://site.test/").endswith("ok")
    assert web.fetch("http://docs.site.test/").endswith("ok")
    refus = "Error: autre.test is not a domain this proxy is allowed to read."
    assert web.fetch("http://autre.test/") == refus
    assert len(web.requests) == 2          # rien n'est parti vers autre.test
    assert web.fetch("http://site.test/sortie") == refus
    assert [r.headers["host"] for r in web.requests[2:]] == ["site.test"]

    monkeypatch.setattr(net, "ALLOWED_DOMAINS", [])
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", ["autre.test"])
    assert web.fetch("http://site.test/").endswith("ok")
    assert web.fetch("http://sous.autre.test/") == refus.replace(
        "autre.test is", "sous.autre.test is")
    assert web.fetch("http://site.test/sortie") == refus


def test_fetch_rebinding_entre_deux_sauts(dns):
    """Le même nom, public au premier saut, privé au second : chaque saut
    refait la résolution ET le contrôle, une seule fois."""
    reponses = iter([[PUBLIC], ["127.0.0.1"], [PUBLIC]])
    dns["rebind.test"] = lambda: next(reponses)
    web = Web({("rebind.test", "/"): redirect("/2")}, default=page("SECRET"))
    out = web.fetch("http://rebind.test/")
    assert out.startswith("Error:") and "SECRET" not in out
    assert len(web.requests) == 1 and web.requests[0].url.host == PUBLIC
    assert dns["__seen__"] == ["rebind.test", "rebind.test"]


def test_fetch_nombre_de_redirections_borne():
    def chaine(sauts):
        """`sauts` redirections puis la page (chaîne finie : sans la
        borne, le test échoue au lieu de tourner sans fin)."""
        web = Web(default=lambda req: redirect(f"/{len(web.requests)}")
                  if len(web.requests) <= sauts else page("arrivé"))
        return web.fetch("http://site.test/"), len(web.requests)

    maxi = net.MAX_REDIRECTS
    out, requetes = chaine(maxi)
    assert out.endswith("arrivé") and requetes == maxi + 1
    assert chaine(maxi + 20) == ("Error: too many redirects.", maxi + 1)


# ── web_fetch : contenu ─────────────────────────────────────────────────

def test_fetch_ne_lit_que_les_types_textuels():
    for ct in ["image/png", "application/zip", "IMAGE/PNG; q=1"]:
        out = Web(default=page(b"\x89PNG\r\n", ct=ct)).fetch("http://site.test/f")
        assert out.startswith("Error: http://site.test/f is "), ct
        assert "cannot read" in out and "PNG" not in out, ct
    # Texte, JSON, XML : tels quels, sans passer par le rendu HTML.
    brut = '{"a": "<b>pas du html</b>"}'
    for ct in ["text/markdown; charset=utf-8", "application/json",
               "application/rss+xml", "TEXT/Plain"]:
        out = Web(default=page(brut, ct=ct)).fetch("http://site.test/")
        assert out.endswith("\n---\n" + brut), ct
    # Charset annoncé, inconnu de Python (LookupError sortait de run()),
    # octets invalides.
    for corps, ct, attendu in [
        ("café".encode("latin-1"), "text/plain; charset=iso-8859-1", "café"),
        ("café".encode(), "text/plain; charset=inexistant-42", "café"),
        (b"ok \xff\xfe fin", "text/plain", "ok �� fin"),
    ]:
        assert Web(default=page(corps, ct=ct)).fetch("http://site.test/").endswith(
            attendu), ct


def test_fetch_html_rendu_en_texte_avec_titre():
    html = ("<html><head><title> Ma  page </title><style>p{}</style></head>"
            "<body><h1>Titre</h1><p>Un <a href='https://a.test/x'>lien</a>.</p>"
            "<script>alert(1)</script></body></html>")
    web = Web({("site.test", "/"): page(html, ct="text/html; charset=utf-8"),
               # Sans Content-Type : HTML s'il en a l'air, texte sinon.
               ("site.test", "/h"): page("<HTML><body><p>vu</p></body>", ct=None),
               ("site.test", "/t"): page("a < b", ct=None),
               ("site.test", "/vide"): page("", ct="text/html")})
    assert web.fetch("http://site.test/") == (
        "URL: http://site.test/\nTitle: Ma page\nContent-Type: text/html\n\n"
        "---\n# Titre\n\nUn lien (https://a.test/x).")
    assert web.fetch("http://site.test/h") == "URL: http://site.test/h\n\n---\nvu"
    assert web.fetch("http://site.test/t") == "URL: http://site.test/t\n\n---\na < b"
    assert web.fetch("http://site.test/vide").endswith("\n---\n(empty page)")


def test_fetch_page_coupee_a_max_chars_et_reprise_par_offset(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 10)
    texte = "abcdefghijklmnopqrstuvwxy"         # 25 caractères
    web = Web({("site.test", "/court"): page("0123456789")}, default=page(texte))

    def lit(**args):
        tete, corps = web.fetch("http://site.test/", **args).split("\n\n---\n")
        return tete.split("\n")[-1], corps

    assert lit() == (
        "Characters: 0-10 of 25 (truncated: pass offset=10 to continue)", "abcdefghij")
    assert lit(offset=10) == (
        "Characters: 10-20 of 25 (truncated: pass offset=20 to continue)", "klmnopqrst")
    assert lit(offset=20) == ("Characters: 20-25 of 25", "uvwxy")
    assert lit(offset=10**9) == ("Characters: 25-25 of 25", "(empty page)")
    # Offset qui n'est pas un entier positif : depuis le début.
    for bizarre in (-5, "10", 10.0, True, None):
        assert lit(offset=bizarre)[1] == "abcdefghij", bizarre
    # Page qui tient en entier : pas de ligne Characters.
    assert "Characters" not in web.fetch("http://site.test/court")


def test_fetch_arrete_de_lire_a_max_bytes(monkeypatch):
    """Le flux n'est pas lu jusqu'au bout (un corps sans fin ne bloque
    pas), et la sortie dit qu'elle est partielle."""
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 100)
    servis = []

    async def sans_fin():
        while True:
            servis.append(1)
            yield b"y" * 40

    web = Web(default=lambda req: httpx.Response(
        200, content=sans_fin(), headers={"content-type": "text/plain"}))
    out = web.fetch("http://site.test/")
    assert "Note: only the first 100 bytes were downloaded." in out
    assert out.endswith("\n---\n" + "y" * 100) and len(servis) == 3
    # Coupé au milieu d'un caractère : remplacé, pas d'exception.
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 3)
    assert Web(default=page("éé")).fetch("http://site.test/").endswith("\n---\né�")


def test_fetch_pdf_texte_extrait_decoupe_et_garde_en_cache(monkeypatch):
    """Un PDF se lit comme une page : texte extrait (hors de la boucle
    asyncio), morceaux par `offset`, plage affichée, cache. Reconnu à son
    type OU à ses premiers octets, et téléchargé sous SA borne."""
    doc = pdf("Page un", "Page deux", "Page trois")
    web = Web({("site.test", "/a.pdf"): page(doc, ct="application/pdf"),
               ("site.test", "/b"): page(doc, ct="Application/PDF; qs=0.9"),
               # Type faux ou absent : les premiers octets tranchent.
               ("site.test", "/c"): page(doc, ct="application/octet-stream"),
               ("site.test", "/d"): page(doc, ct="text/plain"),
               ("site.test", "/e"): page(doc, ct=None)})
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 100)    # la borne des pages HTML
    for chemin in ("/a.pdf", "/b", "/c", "/d", "/e"):
        assert web.fetch("http://site.test" + chemin) == (
            f"URL: http://site.test{chemin}\nContent-Type: application/pdf\n\n"
            "---\nPage un\n\nPage deux\n\nPage trois"), chemin
    # Chiffré sans mot de passe d'ouverture (droits restreints) : lisible.
    assert Web(default=page(pdf("Ouvert", password=""))).fetch(
        "http://site.test/").endswith("\n---\nOuvert")
    # Morceaux : une seule requête, une seule extraction, dans un autre fil.
    monkeypatch.setattr(webcache.CACHE, "ttl", 600)
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 12)
    fils, extrait = [], web_fetch.pdf_text

    def espion(body):
        fils.append(threading.current_thread())
        return extrait(body)
    monkeypatch.setattr(web_fetch, "pdf_text", espion)
    u, n = "http://site.test/a.pdf", len(web.requests)
    debut = web.fetch(u)
    assert debut.endswith("Characters: 0-12 of 30 (truncated: pass offset=12 to "
                          "continue)\n\n---\nPage un\n\nPag")
    assert web_fetch.TOOL.summary({"url": u}, web.last)["url"] == u + " [0, 12]"
    assert web.fetch(u, offset=12).endswith("\n---\ne deux\n\nPage")
    assert web.fetch(u, offset=24).endswith("Characters: 24-30 of 30\n\n---\n trois")
    assert len(web.requests) == n + 1 and len(fils) == 1
    assert fils[0] is not threading.main_thread()
    # Au-delà de PDF_MAX_PAGES : le début, et un mot qui le dit.
    monkeypatch.setattr(web_fetch, "PDF_MAX_PAGES", 2)
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 20_000)
    assert web.fetch("http://site.test/b").endswith(
        "\n---\nPage un\n\nPage deux\n\n[only the first 2 of 3 pages were extracted]")


def test_fetch_pdf_illisible_rendu_en_erreur_jamais_garde(monkeypatch):
    """Chiffré, abîmé, sans couche texte, trop gros : un texte « Error: »
    qui dit pourquoi, pas une exception, et rien en cache."""
    monkeypatch.setattr(webcache.CACHE, "ttl", 600)
    doc = pdf("Texte")
    for nom, corps, ct, attendu in [
        ("chiffré", pdf("Secret", password="sésame"), "application/pdf",
         "is an encrypted PDF (a password is required)"),
        ("scan", pdf("", ""), "application/pdf",
         "is a PDF with no extractable text (2 of 2 pages read, probably scanned "
         "images)"),
        ("coupé", doc[:len(doc) // 2], "application/octet-stream",
         "is not a readable PDF"),
        ("en-tête seul", b"%PDF-1.7\n", "text/plain", "is not a readable PDF"),
        ("pas un PDF", b"<html>connexion requise</html>", "application/pdf",
         "is not a readable PDF"),
        ("vide", b"", "application/pdf", "is not a readable PDF"),
    ]:
        out = Web(default=page(corps, ct=ct)).fetch("http://site.test/f")
        assert out.startswith("Error: http://site.test/f " + attendu), (nom, out)
        assert "Secret" not in out, nom
    # Plus gros que sa borne : refusé sans tenter l'extraction (la fin d'un
    # PDF porte sa table des objets), et le flux n'est pas lu jusqu'au bout.
    monkeypatch.setattr(web_fetch, "PDF_MAX_BYTES", 300)
    monkeypatch.setattr(web_fetch, "pdf_text", lambda body: pytest.fail("extrait"))
    servis = []

    async def sans_fin():
        yield b"%PDF-1.4\n"
        while True:
            servis.append(1)
            yield b"y" * 100

    assert Web(default=lambda req: httpx.Response(
        200, content=sans_fin())).fetch("http://site.test/f") == (
        "Error: http://site.test/f is a PDF larger than 300 bytes, which this "
        "tool does not download.")
    assert len(servis) == 3 and len(webcache.CACHE) == 0


def test_fetch_echec_http_ou_reseau_rendu_en_texte():
    def casse(req):
        raise httpx.ReadTimeout("lent")

    web = Web({("site.test", "/404"): page("SECRET", status=404),
               ("site.test", "/500"): page("SECRET", status=500),
               ("site.test", "/r"): redirect("http://autre.test/y")}, default=casse)
    for status in (404, 500):
        assert web.fetch(f"http://site.test/{status}") == (
            f"Error: http://site.test/{status} returned HTTP {status}.")
    assert web.fetch("http://site.test/x") == (
        "Error: could not fetch http://site.test/x (ReadTimeout).")
    # Au deuxième saut, c'est l'URL de ce saut qui est nommée.
    assert web.fetch("http://site.test/r") == (
        "Error: could not fetch http://autre.test/y (ReadTimeout).")


# ── web_search ──────────────────────────────────────────────────────────

def test_search_requete_envoyee_a_searxng(monkeypatch):
    _, (req,) = search({"query": "  llama.cpp  rocm "})
    assert req.method == "GET"
    assert str(req.url.copy_with(query=None)) == "http://searx.test:8080/search"
    assert dict(req.url.params) == {"q": "llama.cpp  rocm", "format": "json",
                                    "pageno": "1"}
    # La requête du modèle ne peut pas changer les paramètres.
    _, (req,) = search({"query": "x&format=html&pageno=9"})
    assert req.url.params["q"] == "x&format=html&pageno=9"
    assert req.url.params.get_list("format") == ["json"]
    assert req.url.params.get_list("pageno") == ["1"]
    # Langue et catégories de la configuration ; instance privée jointe
    # (adresse de configuration : le garde-fou réseau ne s'y applique pas).
    monkeypatch.setattr(web_search, "LANGUAGE", "fr-FR")
    monkeypatch.setattr(web_search, "CATEGORIES", "general,it")
    monkeypatch.setattr(web_search, "SEARXNG_URL", "http://10.0.0.7:8888")
    _, (req,) = search({"query": 'site:a.test "phrase" -x #é', "recency": "week"})
    assert req.url.host == "10.0.0.7"
    assert dict(req.url.params) == {
        "q": 'site:a.test "phrase" -x #é', "format": "json", "pageno": "1",
        "time_range": "week", "language": "fr-FR", "categories": "general,it"}
    # `recency` hors liste : ignorée, pas transmise.
    for recency in ("decade", "", None, 7, "Week", ["day"], "day&format=html"):
        _, (req,) = search({"query": "x", "recency": recency})
        assert "time_range" not in req.url.params, recency


def test_search_sans_requete_ou_non_configuree_rien_n_est_envoye(monkeypatch):
    for args in [{}, {"query": "   \n"}, {"query": None}, {"query": 3}, {"q": "a"}]:
        assert search(args) == ("Error: `query` is required.", []), args
    monkeypatch.setattr(web_search, "SEARXNG_URL", "")
    assert search({"query": "q"}) == (
        "Error: web search is not configured on this proxy.", [])


def test_search_mise_en_forme_des_resultats():
    out, _ = search({"query": "q"}, {"results": [
        res(1, publishedDate="2026-01-02T10:20:30"),
        res(2, title="  Titre\n sur  deux\tlignes ", content=" a\n\n b  c "),
        # Sans URL exploitable : ignorés.
        {"title": "sans url", "content": "x"}, {"url": ""}, {"url": 42}, "chaîne", None,
        res(3, content="", publishedDate=None),
        {"url": "https://r.test/4"},
        # Champs qui ne sont pas du texte.
        {"url": "https://r.test/5", "title": 404, "content": ["a", "b"],
         "publishedDate": 20260102},
        # Extrait coupé à 240 caractères, espace final retiré.
        res(6, content="m" * 239 + " " + "n" * 50),
    ]})
    assert out == (
        "[1] Titre 1 (2026-01-02)\n    https://r.test/1\n    extrait 1\n"
        "[2] Titre sur deux lignes\n    https://r.test/2\n    a b c\n"
        "[3] Titre 3\n    https://r.test/3\n"
        "[4] https://r.test/4\n    https://r.test/4\n"
        "[5] 404 (20260102)\n    https://r.test/5\n    ['a', 'b']\n"
        "[6] Titre 6\n    https://r.test/6\n    " + "m" * 239 + "…")
    for corps in ({"results": []}, {"results": None}, {}, {"results": [{"x": 1}]}):
        assert search({"query": " q "}, httpx.Response(200, json=corps))[0] == (
            "No results for «q»."), corps


def test_search_limit_bornee_et_defaut_si_pas_un_entier(monkeypatch):
    monkeypatch.setattr(web_search, "LIMIT", 3)
    trente = {"results": [res(n) for n in range(1, 31)]}
    absent = object()
    for limit, attendu in [(1, 1), (20, 20), (21, 20), (10**9, 20), (0, 1), (-3, 1),
                           (absent, 3), (None, 3), ("5", 3), (5.0, 3), (True, 3)]:
        args = {"query": "q"} if limit is absent else {"query": "q", "limit": limit}
        assert search(args, trente)[0].count("\n    https://") == attendu, limit


def test_search_moteurs_indisponibles_n_est_pas_aucun_resultat():
    """SearXNG sans résultat parce que ses moteurs sont bloqués : une
    erreur qui dit de ne pas relancer, pas « No results » (le modèle
    reformulerait et relancerait). Des résultats malgré un moteur en panne :
    rendus normalement."""
    panne = [["brave", "Suspended: too many requests"], ["duckduckgo", "CAPTCHA"]]
    out, _ = search({"query": "q"}, {"results": [], "unresponsive_engines": panne})
    assert out.startswith("Error: the search engines are temporarily unavailable "
                          "(brave: Suspended: too many requests, duckduckgo: CAPTCHA).")
    assert "Do not retry" in out
    out, _ = search({"query": "q"}, {"results": [res(1)], "unresponsive_engines": panne})
    assert out.startswith("[1] ")
    out, _ = search({"query": "q"}, {"results": [], "unresponsive_engines": []})
    assert out == "No results for «q»."


def test_search_reponse_illisible():
    """Pas du JSON, ou du JSON valide d'une autre forme (liste, `results`
    qui n'en est pas une) : une erreur, pas une exception."""
    for corps in [b"<html>pas du json</html>", b"", b"\xff\xfe", b"[1, 2]", b"null",
                  b'{"results": 5}', b'{"results": {"0": {"url": "https://r.test/"}}}']:
        out, _ = search({"query": "q"}, httpx.Response(
            200, content=corps, headers={"content-type": "application/json"}))
        assert out == "Error: unreadable search engine response.", corps


def test_search_moteur_en_erreur_ou_injoignable():
    out, _ = search({"query": "q"}, httpx.Response(403, text="Forbidden"))
    assert out.startswith("Error:") and "search.formats" in out and "json" in out
    for status in (204, 302, 500):
        out, _ = search({"query": "q"}, httpx.Response(
            status, json={"results": [res(1)]}, headers={"location": "http://x.test/"}))
        assert out == f"Error: search engine returned HTTP {status}."

    def casse(req):
        raise httpx.ConnectTimeout("lent")

    assert search({"query": "q"}, casse)[0] == (
        "Error: search engine unreachable (ConnectTimeout).")


# ── html_text ───────────────────────────────────────────────────────────

def test_html_ignore_ce_que_le_lecteur_ne_voit_pas():
    html = ("<html><head><title>T</title><style>body{color:red}</style>"
            "<script>var a = '</p><p>piège';</script></head><body>"
            "<nav><a href='https://a.test/'>Menu</a></nav><p>Vu</p>"
            "<script type='x'>if (a < b) { document.write('<p>non</p>') }</script>"
            "<noscript>activez js</noscript><svg><path d='M0'/><text>svg</text></svg>"
            "<template><p>gabarit</p></template><iframe src='x'>cadre</iframe>"
            "<button>Cliquer</button><select><option>choix</option></select>"
            "<footer>pied</footer><p>Aussi</p></body></html>")
    assert T(html) == ("T", "Vu\n\nAussi")
    # Titre de la page : le premier seulement, pas celui d'un <svg> du corps.
    assert T("<head><title> Ma \n page &amp; &eacute;</title></head><body><svg>"
             "<title>Icône</title></svg><p>x</p></body>") == ("Ma page & é", "x")
    # ASP.NET : toute la page dans <form>. Elle sortait vide.
    assert T("<body><form action='/'><input type=hidden name=v value=x>"
             "<h1>Titre</h1><p>Texte</p><button>Envoyer</button></form></body>") == (
        "", "# Titre\n\nTexte")


def test_html_structure_titres_listes_blocs_tableaux():
    assert T("<h1>Un</h1><h2>Deux</h2><h6>  Six \n fois </h6><p>fin</p>") == (
        "", "# Un\n\n## Deux\n\n###### Six fois\n\nfin")
    assert T("<ul><li>a</li><li>b <b>gras</b></li></ul><ol><li>c</ol>")[1] == (
        "- a\n- b gras\n\n- c")
    assert T("<div>a</div><div>b</div><p>c<br>d</p>\n\n\n<section>e</section>")[1] == (
        "a\n\nb\n\nc\nd\n\ne")
    assert T("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr>"
             "</table>")[1] == "| A | B\n\n| 1 | 2"
    assert T("<p>a   b\n\n\t caf&eacute; &lt;b&gt; &amp; &#233; &#x41; &nbsp;fin "
             "&inconnue; &copy</p>")[1] == "a b café <b> & é A fin &inconnue; ©"


def test_html_liens():
    assert T("<p>Voir <a href='https://a.test/x?y=1&amp;z=2'><b>la</b> doc</a> ici")[1] == (
        "Voir la doc (https://a.test/x?y=1&z=2) ici")
    # Libellé identique à l'URL, ou vide : l'URL seule.
    assert T("<a href='https://a.test/'>https://a.test/</a>")[1] == "https://a.test/"
    assert T("<a href='https://a.test/'><img src='x.png'></a>")[1] == "https://a.test/"
    # Lien relatif, ancre, javascript:, sans href : le libellé seul.
    assert T("<a href='/x'>rel</a> <a href='#h'>ancre</a> "
             "<a href='javascript:void(0)'>js</a> <a>nu</a> <a href>vide</a>")[1] == (
        "rel ancre js nu vide")


def test_html_pre_garde_tel_quel():
    html = ("<p>Avant</p><pre>def f():\n    return  1\n\n\nx = [\n\t1,\n]\n</pre>"
            "<p>Après   coup</p>")
    assert T(html)[1] == ("Avant\n\n```\ndef f():\n    return  1\n\n\nx = [\n\t1,\n]\n"
                          "```\n\nAprès coup")
    assert T("<pre><code>\n  a &lt; b &amp;&amp; c\n</code></pre>")[1] == (
        "```\n  a < b && c\n```")


def test_html_balises_non_fermees_ou_mal_forme():
    # </head> omis (HTML valide) : le corps n'est pas avalé.
    assert T("<html><head><title>T</title><meta charset=utf-8><body><p>corps") == (
        "T", "corps")
    # <a> jamais fermé : le texte qui suit n'est pas perdu.
    assert T("<p>début <a href='https://a.test/'>lien <p>suite</p><p>fin</p>")[1] \
        .split() == ["début", "lien", "suite", "fin", "(https://a.test/)"]
    assert T("<a href='https://a.test/'>un<a href='https://b.test/'>deux</a>")[1] == (
        "un (https://a.test/)deux (https://b.test/)")
    assert T("<p>a<p>b<li>c<li>d<h2>e")[1] == "a\nb\n- c\n- d\n\n## e"
    assert T("<pre>code\n  sans fin")[1] == "```\ncode\n  sans fin"
    # Une fermante en trop, un imbriquement profond : le texte reste lu.
    assert T("</nav><p>x</p>")[1] == "x"
    assert T("<div>" * 5000 + "x")[1] == "x"
    for html in ["", "   \n\t ", "<html></html>", "<!doctype html><!-- rien -->"]:
        assert T(html) == ("", ""), html
    # Tordu : jamais d'exception.
    for html in ["<", "<p <b>x", "<a href=", "<![CDATA[x]]>", "<!-- jamais fermé",
                 "&#99999999999;", "<script>jamais fermé", "\x00<p>\x00</p>",
                 "<a href='https://a.test/'>" * 500, "<pre>" * 50 + "x" + "</pre>" * 3]:
        assert all(isinstance(part, str) for part in T(html)), html[:40]


# ── Memory ──────────────────────────────────────────────────────────────

def test_memory_rend_ce_qui_a_ete_range():
    m = tools.Memory(2, 60)
    assert m.recall("a") is None and len(m) == 0
    m.store("a", "web_search", '{"query": "x"}', "v1")
    assert m.recall("a") == {"name": "web_search", "arguments": '{"query": "x"}',
                             "result": "v1"}
    m.store("a", "autre", '{"x": 1}', "v2")    # même clé : écrasée
    assert len(m) == 1
    assert m.recall("a") == {"name": "autre", "arguments": '{"x": 1}', "result": "v2"}


def test_memory_evince_l_entree_la_moins_recemment_utilisee():
    m = tools.Memory(3, 60)
    for k in "abc":
        m.store(k, "n", "{}", k)
    assert m.recall("a")["result"] == "a"      # «a» redevient la plus récente
    m.store("d", "n", "{}", "d")
    assert len(m) == 3 and m.recall("b") is None
    assert m.recall("a") and m.recall("c") and m.recall("d")
    m.store("a", "n", "{}", "a2")               # une réécriture rafraîchit aussi
    m.store("e", "n", "{}", "e")
    assert m.recall("c") is None and m.recall("a")["result"] == "a2"
    vide = tools.Memory(0, 60)                  # capacité nulle : rien n'est gardé
    vide.store("a", "n", "{}", "a")
    assert len(vide) == 0 and vide.recall("a") is None


def test_memory_cloisonnee_par_client():
    """Le client fait partie de la clé : le même identifiant, pour un autre
    client, n'existe pas. La borne, elle, est commune."""
    alice, bob = tools.owner("clé-alice"), tools.owner("clé-bob")
    # Un condensé, stable dans le processus, qui ne contient pas la clé ;
    # pas de clé = le client unique d'un proxy ouvert.
    assert alice == tools.owner("clé-alice") != bob
    assert len(alice) == 32 and "alice" not in alice and tools.owner("") == ""
    m = tools.Memory(3, 60)
    m.store("ws_1", "n", "{}", "pour alice", alice)
    assert m.recall("ws_1", alice)["result"] == "pour alice"
    assert m.recall("ws_1", bob) is None and m.recall("ws_1") is None
    m.store("ws_1", "n", "{}", "pour bob", bob)   # même id, autre client
    assert len(m) == 2 and m.recall("ws_1", alice)["result"] == "pour alice"
    # LRU commune : les entrées d'un client peuvent pousser dehors celles
    # d'un autre (son rejeu retombe alors sur le résultat «expiré»).
    m.recall("ws_1", bob)
    m.store("ws_2", "n", "{}", "b2", bob)
    m.store("ws_3", "n", "{}", "b3", bob)
    assert m.recall("ws_1", alice) is None and m.recall("ws_1", bob)


def test_memory_expiration(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: t[0])
    m = tools.Memory(4, 60)
    m.store("a", "n", "{}", "a")
    m.store("b", "n", "{}", "b")
    t[0] += 60
    assert m.recall("a")["result"] == "a"      # pile la durée : encore là
    m.store("b", "n", "{}", "b2")               # réécrite : repart de zéro
    t[0] += 0.5
    # Relire «a» n'a pas prolongé sa durée ; expirée, elle est retirée.
    assert m.recall("a") is None and len(m) == 1
    assert m.recall("b")["result"] == "b2"


# ── Hosted ──────────────────────────────────────────────────────────────

def test_hosted_run_passe_les_arguments_du_modele():
    h = hosted(outil())
    assert texte(h, "echo", '{"b": 2, "a": "é"}', 0) == 'reçu {"a": "\\u00e9", "b": 2}'
    # Sans arguments («», None) : objet vide, pas une exception.
    assert texte(h, "echo", "", 0) == texte(h, "echo", None, 0) == "reçu {}"


def test_hosted_appel_invalide_refuse_sans_executer():
    appels = []

    async def run(args):
        appels.append(args)
        return "x"

    h = hosted(outil(run=run))
    # Inconnu, même hors limite : c'est « inconnu » qui est dit.
    assert texte(h, "rm_rf", "{}", 99) == "Error: unknown tool rm_rf."
    for arguments in ["pas du json", "{", "[1, 2]", '"chaîne"', "null", "{'a': 1}",
                      '{"a": 1} trop', {"query": "déjà un dict"}, 12]:
        assert texte(h, "echo", arguments, 0) == (
            "Error: the tool arguments are not a JSON object."), arguments
    assert appels == []


def test_hosted_limite_d_appels_par_reponse(monkeypatch):
    monkeypatch.setattr(tools, "MAX_CALLS", 3)
    appels = []

    async def run(args):
        appels.append(1)
        return "ok"

    h = hosted(outil(run=run))
    assert [texte(h, "echo", "{}", n) for n in (0, 1, 2)] == ["ok"] * 3
    for n in (3, 100):
        assert texte(h, "echo", "{}", n).startswith(
            "Error: the limit of 3 tool calls for one answer"), n
    # Un outil web garde son texte, à l'octet près.
    assert texte(hosted(web_search.TOOL), "web_search", "{}", 3) == (
        "Error: the limit of 3 web tool calls for one answer is reached. "
        "Answer now with what you already have.")
    # La limite du client (`max_uses`) ne peut que l'abaisser.
    assert texte(h, "echo", "{}", 1, limit=1).startswith("Error: the limit of 1 ")
    assert texte(h, "echo", "{}", 3, limit=50).startswith("Error: the limit of 3 ")
    assert len(appels) == 3


def test_hosted_echec_de_l_outil_rendu_en_texte(monkeypatch):
    for exc in (RuntimeError("boum"), httpx.ConnectError("x"), KeyError("k"),
                UnicodeEncodeError("ascii", "é", 0, 1, "x"), MemoryError()):
        async def casse(args):
            raise exc

        assert texte(hosted(outil("casse", run=casse)), "casse", "{}", 0) == (
            f"Error: casse failed ({type(exc).__name__}).")
    # Délai dépassé : la coroutine est annulée, pas abandonnée.
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.05)
    fini = []

    async def lent(args):
        try:
            await asyncio.sleep(30)
        finally:
            fini.append("annulé")

    assert texte(hosted(outil("lent", run=lent)), "lent", "{}", 0) == (
        "Error: lent timed out after 0 s.")
    assert fini == ["annulé"]


def test_hosted_resultat_tronque(monkeypatch):
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 10)

    async def bavard(args):
        return "x" * args["n"]

    h = hosted(outil("bavard", run=bavard))
    assert texte(h, "bavard", '{"n": 10}', 0) == "x" * 10
    assert texte(h, "bavard", '{"n": 11}', 0) == "x" * 10 + "\n[truncated]"


def test_hosted_retrouve_les_outils_par_type_d_outil_et_par_element():
    search, fetch = web_search.TOOL, web_fetch.TOOL
    h = hosted(search, fetch)
    for kind in ("web_search", "web_search_preview", "web_search_2025_08_26"):
        assert h.for_kind(kind) == h.for_responses(kind) == [search, fetch]
    # Type inconnu, ou correspondance partielle : rien.
    assert h.for_kind("function") == [] and h.for_kind("web") == []
    # L'outil serveur Anthropic, toutes versions datées — et elles seules.
    assert h.for_server("web_search_20250305") is search
    assert h.for_server("web_fetch_20260318") is fetch
    for kind in ("web_search", "web_search_", "web_search_v2", "fetch_20250910"):
        assert h.for_server(kind) is None, kind
    # L'élément rejoué est attribué d'après son action.
    for tool, args in [(search, {"query": "x"}), (fetch, {"url": "u"})]:
        item = {"type": "web_search_call", "action": tool.summary(args)}
        assert h.for_item(item) is tool, item
        assert args.items() <= item["action"].items()
    for item in [{"type": "web_search_call", "action": {"type": "find_in_page"}},
                 {"type": "web_search_call", "action": "search"},
                 {"type": "function_call", "action": {"type": "search"}}, {}]:
        assert h.for_item(item) is None, item
    # Sans l'outil de lecture, son élément n'est attribué à personne.
    assert hosted(search).for_item(
        {"type": "web_search_call", "action": {"type": "open_page"}}) is None


def test_hosted_par_defaut_les_outils_actives(monkeypatch):
    monkeypatch.setattr(web_search, "ENABLED", True)
    monkeypatch.setattr(web_fetch, "ENABLED", False)
    h = tools.Hosted()
    assert h.tools == [web_search.TOOL] and h.memory is tools.MEMORY and bool(h)
    monkeypatch.setattr(web_search, "ENABLED", False)
    assert not tools.Hosted()           # aucun outil : app.py n'en présente pas


# ── web_search : le texte du modèle et ses sources ──────────────────────
# (la surface Anthropic fait ses blocs `web_search_result` des sources, et
# son client les rejoue : le texte doit s'en refaire à l'identique)

def test_search_text_and_sources_say_the_same_thing():
    """Le résultat porte le texte ET les sources, qui disent la même
    chose : `render` refait l'un des autres, à l'octet près — c'est ce
    qui permet au client de rejouer ses blocs sans mémoire côté proxy."""
    sources = lambda *dicts: tuple(tools.Source(**d) for d in dicts)
    assert SEARCHED.sources == sources(*RESULTS) and SEARCHED.error is None
    assert web_search.TOOL.render(
        {"query": " llama.cpp latest release "}, SEARCHED.sources) == FOUND
    assert FOUND.split("\n") == [
        "[1] Releases · ggml-org/llama.cpp (2026-10-03)",
        "    https://github.com/ggml-org/llama.cpp/releases",
        "    LLM inference in C/C++ — b6789, «latest»…",
        "[2] llama.cpp (blog)",
        "    https://example.org/blog/llama"]
    # «Aucun résultat» : un texte, aucune source — pas une erreur.
    assert web_search.found("q", []) == tools.Result("No results for «q».")
    # Les sources tirées des résultats bruts de SearXNG.
    raw = [{"title": " Un  titre ", "url": "https://a.test/x",
            "publishedDate": "2026-01-02T03:04:05", "content": " du\ntexte "},
           {"title": "sans url"}]
    assert web_search.entries(raw, 5) == sources({
        "title": "Un titre", "url": "https://a.test/x", "date": "2026-01-02",
        "snippet": "du texte"})
    # La date est collée au titre dans le texte : il n'y en a qu'UNE
    # lecture. Un titre qui finit de lui-même par une date est daté, une
    # date d'une autre forme reste dans le titre — et le texte, lui, est
    # celui qu'on attend dans les deux cas.
    for title, date, attendu, ligne in (
            ("Notes (2024-05-01)", "", ("Notes", "2024-05-01"),
             "[1] Notes (2024-05-01)"),
            ("Notes", "Jan 5, 2025", ("Notes (Jan 5, 202)", ""),
             "[1] Notes (Jan 5, 202)")):
        (odd,) = web_search.entries(
            [{"title": title, "publishedDate": date, "url": "https://x.test",
              "content": "s"}], 5)
        assert (odd.title, odd.date) == attendu
        assert web_search.render("q", [odd]).split("\n")[0] == ligne


def test_search_domain_filters(monkeypatch):
    raw = [{"title": str(n), "url": u} for n, u in enumerate([
        "https://github.com/ggml-org/llama.cpp",
        "https://docs.github.com/en/rest",
        "https://notgithub.com/x",
        "https://example.org/blog/post-1",
        "https://example.org/shop",
    ])]
    urls = lambda **kw: [e.url for e in web_search.entries(raw, 20, **kw)]
    # Sous-domaines couverts, pas les homonymes ; un sous-domaine précis ne
    # couvre pas son parent ; un chemin restreint à ce qui le prolonge.
    assert urls(allowed=["github.com"]) == [raw[0]["url"], raw[1]["url"]]
    assert urls(allowed=["docs.github.com"]) == [raw[1]["url"]]
    assert urls(allowed=["example.org/blog"]) == [raw[3]["url"]]
    assert urls(allowed=["https://Example.org/blog/*"]) == [raw[3]["url"]]
    assert urls(blocked=["github.com", "example.org/shop"]) == [
        raw[2]["url"], raw[3]["url"]]
    # Le filtre passe AVANT la limite.
    assert [e.url for e in web_search.entries(
        raw, 1, allowed=["example.org"])] == [raw[3]["url"]]

    def handler(request):
        assert request.url.params["q"] == "x"   # la requête n'est pas réécrite
        return httpx.Response(200, json={"results": raw})

    go = lambda **kw: asyncio.run(web_search.TOOL.run(
        {"query": "x"}, tools.Call(settings=kw),
        transport=httpx.MockTransport(handler))).text
    monkeypatch.setattr(web_search, "SEARXNG_URL", "http://searxng.test")
    assert go(allowed_domains=["example.org"], blocked_domains=[
        "example.org/shop"]) == "[1] 3\n    https://example.org/blog/post-1"
    assert go(allowed_domains=["nulle-part.test"]) == "No results for «x»."
    assert go().count("https://") == 5


# ── routes /v1/tools (appel direct, pour pi et omp) ─────────────────────

@pytest.fixture
def routes(monkeypatch):
    """(client, arguments reçus par l'outil) : recherche active et
    remplacée par un faux, lecture désactivée, proxy sans clé."""
    from fastapi.testclient import TestClient
    from llm_proxy import app as A

    monkeypatch.setattr(A, "PROXY_API_KEYS", frozenset())
    monkeypatch.setattr(web_search, "ENABLED", True)
    monkeypatch.setattr(web_fetch, "ENABLED", False)
    seen = []

    async def run(self, args, call):
        seen.append(args)
        if args.get("query") == "panne":
            raise tools.ToolError("unavailable", "moteur éteint.")
        return web_search.found("é", [tools.Source(
            "https://e.org", "Titre", snippet="extrait")])
    monkeypatch.setattr(web_search.WebSearch, "run", run)
    return TestClient(A.app), seen


def test_route_tools_liste_les_outils_actifs_et_les_execute(routes):
    client, seen = routes
    (actif,) = client.get("/v1/tools").json()["data"]
    assert actif["name"] == "web_search" and "description" in actif
    assert actif["parameters"]["required"] == ["query"]
    r = client.post("/v1/tools/web_search", json={"query": "é", "limit": 3})
    assert r.status_code == 200
    # L'enveloppe : `name`, `result`, `is_error` (ce que lisent les
    # clients), puis le code d'erreur, les sources, `meta`, les fichiers.
    assert r.json() == {"name": "web_search", "is_error": False,
                        "result": "[1] Titre\n    https://e.org\n    extrait",
                        "error": None, "meta": {}, "files": [], "sources": [{
                            "url": "https://e.org", "title": "Titre",
                            "date": "", "snippet": "extrait"}]}
    assert seen == [{"query": "é", "limit": 3}]
    # Échec de l'outil : 200 quand même, c'est un texte pour le modèle.
    r = client.post("/v1/tools/web_search", json={"query": "panne"})
    assert r.status_code == 200 and r.json() == {
        "name": "web_search", "result": "Error: moteur éteint.",
        "is_error": True, "error": "unavailable", "sources": [], "meta": {},
        "files": []}


def test_route_tools_run_refuse_outil_inactif_et_corps_non_objet(routes):
    client, seen = routes
    for name in ("web_fetch", "rm_rf"):         # désactivé, inconnu
        r = client.post(f"/v1/tools/{name}", json={"url": "https://e.org"})
        assert r.status_code == 404, name
        assert r.json()["error"]["type"] == "unknown_tool"
    for body in ("[1]", "pas du json", ""):
        r = client.post("/v1/tools/web_search", content=body)
        assert r.status_code == 400, body
    assert seen == []


def test_route_tools_exige_la_cle_du_proxy(routes, monkeypatch):
    from llm_proxy import app as A
    client, seen = routes
    monkeypatch.setattr(A, "PROXY_API_KEYS", frozenset({"secret"}))
    assert client.get("/v1/tools").status_code == 401
    assert client.post("/v1/tools/web_search",
                       json={"query": "x"}).status_code == 401
    assert seen == []
    r = client.post("/v1/tools/web_search", json={"query": "x"},
                    headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200 and seen == [{"query": "x"}]


def test_cle_du_proxy_non_ascii_ne_casse_pas_le_controle(routes, monkeypatch):
    """Une clé configurée avec un accent : le contrôle compare en octets
    (compare_digest refuse une chaîne non ASCII) — 401 pour un autre jeton,
    pas un 500."""
    from llm_proxy import app as A
    client, _ = routes
    monkeypatch.setattr(A, "PROXY_API_KEYS", frozenset({"clé-secrète"}))
    r = client.get("/v1/tools", headers={"Authorization": "Bearer cle-fausse"})
    assert r.status_code == 401


def test_type_d_un_modele_sans_type_au_catalogue():
    """Un catalogue qui ne dit pas ce que sont ses modèles (llama-swap) :
    le nom tranche, un motif de configuration l'emporte, et ce que le
    backend déclare n'est jamais deviné à sa place."""
    from llm_proxy import app as A
    from llm_proxy.backends import Backend
    b = Backend("essai", {"url": "http://x", "model_types": {"*-voice-chat": "text-generation"}})
    for model, attendu in [
        ({"id": "qwen3.8-flash-next"}, "text-generation"),
        ({"id": "Qwen-Image-2.1-heretic"}, "text-to-image"),
        ({"id": "qwen3-tts-12hz-1.7b-voice-design"}, "text-to-speech"),
        ({"id": "qwen3-asr-1.7b"}, "automatic-speech-recognition"),
        ({"id": "bge-m3"}, "text-embeddings-inference"),
        # « imagine » n'est pas « image » : un mot entier, pas une sous-chaîne.
        ({"id": "imagine-7b"}, "text-generation"),
        ({"id": "mon-voice-chat"}, "text-generation"),                 # motif
        ({"id": "whisper-large-v3", "type": "text-generation"}, "text-generation"),
        ({"id": "qwen-image-edit", "architecture": {
            "input_modalities": ["text", "image"], "output_modalities": ["text"]}},
         "image-text-to-text"),
    ]:
        assert A._model_type(model, b) == attendu, model["id"]
    assert set(A.CHAT_TYPES) == {"text-generation", "image-text-to-text"}


def test_fetch_action_distingue_les_morceaux_d_une_page_longue(monkeypatch):
    """Le client n'affiche que l'URL de chaque ouverture : la plage rendue
    la suit pour une page lue par morceaux, une page courte garde son URL
    nue. La plage est celle du résultat réel, pas un calcul sur l'offset."""
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 20)
    u, summary = "http://site.test/doc", web_fetch.TOOL.summary
    web = Web(default=page("x" * 50))
    for args, plage in [({}, " [0, 20]"), ({"offset": 20}, " [20, 40]"),
                        ({"offset": 40}, " [40, 50]")]:
        web.fetch(u, **args)
        assert summary({"url": u, **args}, web.last) == {
            "type": "open_page", "url": u + plage}, args
    court = Web(default=page("bonjour"))
    court.fetch(u)
    assert summary({"url": u}, court.last) == {"type": "open_page", "url": u}
    # Sans résultat (élément en cours, outil interrogé à vide) ou en erreur.
    assert summary({"url": u, "offset": 20}) == {"type": "open_page", "url": u}
    assert summary({"url": u}, tools.failure("unavailable", "x"))["url"] == u
    assert summary({}) == {"type": "open_page", "url": ""}


# ── cache web ───────────────────────────────────────────────────────────

def test_cache_web_une_page_longue_n_est_telechargee_qu_une_fois(monkeypatch):
    """Les morceaux d'une page (`offset`) et une relecture dans les minutes
    qui suivent sortent du cache ; passé la durée, la page est redemandée."""
    monkeypatch.setattr(webcache.CACHE, "ttl", 600)
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 20)
    web = Web(default=page("x" * 50))
    u = "http://site.test/long"
    assert "Characters: 0-20 of 50" in web.fetch(u)
    assert "Characters: 20-40 of 50" in web.fetch(u, offset=20)
    assert "Characters: 40-50 of 50" in web.fetch(u + "#ancre", offset=40)
    assert len(web.requests) == 1
    horloge = webcache.time.monotonic()
    monkeypatch.setattr(webcache.time, "monotonic", lambda: horloge + 601)
    web.fetch(u)
    assert len(web.requests) == 2


def test_cache_web_ne_garde_ni_les_echecs_ni_ce_que_les_listes_interdisent(monkeypatch):
    monkeypatch.setattr(webcache.CACHE, "ttl", 600)
    web = Web({("site.test", "/ko"): page("non", status=500),
               ("site.test", "/pdf"): page(b"%PDF", ct="application/pdf")},
              default=page("ok"))
    for chemin in ("/ko", "/pdf"):
        for _ in range(2):
            assert web.fetch("http://site.test" + chemin).startswith("Error:")
    assert len(web.requests) == 4 and len(webcache.CACHE) == 0
    # Une page en cache reste soumise aux listes de domaines.
    assert web.fetch("http://site.test/").endswith("ok")
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", ["site.test"])
    assert web.fetch("http://site.test/").startswith("Error: site.test is not")
    # Désactivé : tout repart sur le réseau.
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", [])
    monkeypatch.setattr(webcache.CACHE, "ttl", 0)
    webcache.CACHE.clear()
    n = len(web.requests)
    web.fetch("http://site.test/a")
    web.fetch("http://site.test/a")
    assert len(web.requests) == n + 2


def test_cache_web_recherche_identique_et_bornes(monkeypatch):
    """Même requête : SearXNG n'est interrogé qu'une fois, limite et
    domaines appliqués à chaque appel. Une recherche vide ou des moteurs
    en panne ne sont pas gardés. Bornes : entrées et taille cumulée."""
    monkeypatch.setattr(webcache.CACHE, "ttl", 600)
    donnees = {"results": [res(1), res(2), res(3)]}
    appels = []

    def searx(request):
        appels.append(request)
        return httpx.Response(200, json=donnees)
    out1, _ = search({"query": "q"}, searx)
    out2, _ = search({"query": "q", "limit": 1}, searx)
    assert len(appels) == 1 and "[3]" in out1 and "[2]" not in out2
    search({"query": "q", "recency": "day"}, searx)       # autre requête
    assert len(appels) == 2
    vide = []

    def panne(request):
        vide.append(request)
        return httpx.Response(200, json={
            "results": [], "unresponsive_engines": [["brave", "x"]]})
    for _ in range(2):
        search({"query": "rien"}, panne)
    assert len(vide) == 2

    c = webcache.Cache(ttl=60, entries=2, max_bytes=10)
    c.put("a", 1, 4)
    c.put("b", 2, 4)
    c.put("c", 3, 4)
    assert c.get("a") is None and c.get("c") == 3 and len(c) == 2 and c.size == 8
    c.put("gros", 0, 11)
    assert c.get("gros") is None and (c.hits, c.misses) == (1, 2)


# ── le contrat d'un outil (tools/contract.py, docs/outils.md) ───────────

def test_contrat_outil_minimal_sans_liaison(proxy, monkeypatch):
    """L'outil minimal de docs/outils.md, enregistré. Sans liaison de
    protocole il est listé et exécuté par /v1/tools, et présenté sur
    /v1/chat/completions, où son appel reste caché du client ; les
    surfaces Responses et Anthropic, qui n'auraient rien pour en rendre
    compte, l'ignorent."""
    from llm_proxy import anthropic_api, chat_api, responses_api
    monkeypatch.setattr(tools, "REGISTRY", [])
    echo = tools.register(Echo())
    with pytest.raises(ValueError):             # le nom est la clé
        tools.register(Echo())
    assert tools.enabled() == [echo] and tools.kinds() == {"echo"}
    # L'annuaire que montent les routes : ce que le registre active (la
    # fixture `proxy` a remplacé tools.Hosted par le sien).
    proxy.hosted = h = type(proxy.hosted)(tools.enabled(), tools.Memory(4, 60))
    monkeypatch.setattr(chat_api, "ENABLED", True)
    monkeypatch.setattr(chat_api, "MEMORY", chat_api.Memory(8, 60, 100_000))
    spec = echo.spec({"echo"})
    # L'exemple de la documentation EST cet outil, à la lettre.
    with open(os.path.join(conftest.ROOT, "docs", "outils.md"),
              encoding="utf-8") as f:
        assert inspect.getsource(Echo) in f.read()

    # Appel direct : la déclaration, puis l'enveloppe du résultat.
    assert proxy.client.get("/v1/tools").json()["data"] == [{
        "name": "echo", "description": spec["function"]["description"],
        "parameters": spec["function"]["parameters"]}]
    r = proxy.client.post("/v1/tools/echo", json={"text": "bonjour"})
    assert r.json() == {"name": "echo", "result": "bonjour", "is_error": False,
                        "error": None, "sources": [], "meta": {"chars": 7},
                        "files": []}
    r = proxy.client.post("/v1/tools/echo", json={})
    assert r.status_code == 200 and r.json() == {
        "name": "echo", "result": "Error: `text` is required.",
        "is_error": True, "error": "invalid_input", "sources": [], "meta": {},
        "files": []}

    # chat/completions : déclaré par son nom, présenté en fonction, exécuté
    # par le proxy — le client ne voit ni l'appel ni son résultat.
    proxy.replies = [
        FakeUpstream(chat_doc({"content": None, "tool_calls": [{
            "id": "c", "function": {"name": "echo",
                                    "arguments": "{\"text\": \"bonjour\"}"}}]},
            "tool_calls", 1, 1)),
        FakeUpstream(chat_doc({"content": "Fait."}, "stop", 1, 1))]
    r = proxy.client.post("/v1/chat/completions", json={
        "model": "essai/qwen", "tools": [{"type": "echo"}],
        "messages": [{"role": "user", "content": "Répète."}]})
    message = r.json()["choices"][0]["message"]
    assert r.status_code == 200 and message["content"] == "Fait."
    assert "tool_calls" not in message and "annotations" not in message
    one, two = proxy.sent
    assert one["tools"] == two["tools"] == [spec]
    assert two["messages"][-1] == {"role": "tool", "tool_call_id": "c",
                                   "content": "bonjour"}

    # Sans liaison : ni type Responses, ni outil serveur Anthropic.
    ctx = responses_api.to_chat({"model": "m", "input": "x", "tools": [
        {"type": "echo"}]}, hosted=h)[1]
    assert not ctx.hosted and ctx.ignored == ["echo"]
    assert not anthropic_api.Context({"tools": [
        {"type": "echo_20260101", "name": "echo"}]}, h).hosted


def test_contrat_tout_echec_est_un_result_avec_son_code(monkeypatch):
    """Ce que l'exécuteur fait de chaque issue de `run` : un Result, dont
    le CODE (jamais le texte) décide de l'issue des statistiques. Rien ne
    remonte. Et `run` reçoit dans `call` ce qui ne vient pas du modèle."""
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.05)
    lines, calls = [], []
    monkeypatch.setattr(tools.stats, "record_tool", lambda *a: lines.append(a))

    class Cas(Echo):
        name = "cas"

        async def run(self, args, call):
            calls.append(call)
            cas = args.get("cas")
            if cas == "prévu":
                raise tools.ToolError("not_accessible", "the target is down.")
            if cas == "quota":
                raise tools.ToolError("limit", "quota spent.")
            if cas == "panne":
                raise RuntimeError("secret")
            if cas == "lent":
                await asyncio.sleep(30)
            if cas == "hors liste":
                return tools.failure("mystère", "x.")
            if cas == "pas un Result":
                return "du texte"
            return tools.Result("Error: une page peut commencer ainsi")

    h = hosted(Cas())
    attendu = [  # (arguments, appels déjà faits, code, texte, issue)
        ("{}", 0, None, "Error: une page peut commencer ainsi", "ok"),
        ('{"cas": "prévu"}', 0, "not_accessible",
         "Error: the target is down.", "error"),
        ('{"cas": "panne"}', 0, "unavailable",
         "Error: cas failed (RuntimeError).", "error"),
        ('{"cas": "lent"}', 0, "timeout",
         "Error: cas timed out after 0 s.", "error"),
        ('{"cas": "hors liste"}', 0, "unavailable", "Error: x.", "error"),
        ('{"cas": "pas un Result"}', 0, "unavailable",
         "Error: cas failed (TypeError).", "error"),
        ("[1]", 0, "invalid_input",
         "Error: the tool arguments are not a JSON object.", "error"),
        ('{"cas": "quota"}', 0, "limit", "Error: quota spent.", "limit"),
        ("{}", tools.MAX_CALLS, "limit",
         f"Error: the limit of {tools.MAX_CALLS} tool calls for one "
         "answer is reached. Answer now with what you already have.", "limit"),
    ]
    for arguments, used, code, text, _ in attendu:
        result = go(h.run("cas", arguments, used, endpoint="/v1/x",
                          model="essai/qwen", client="abc",
                          options={"cas": {"max_chars": 5}, "autre": {"x": 1}}))
        assert (result.error, result.text) == (code, text), arguments
        assert code is None or code in tools.ERRORS
    assert [line[:4] for line in lines] == [
        ("cas", "/v1/x", "essai/qwen", issue) for *_, issue in attendu]
    # Les réglages du client sur CET outil, la route, le modèle, le client.
    assert len(calls) == 7 and all(c == tools.Call(
        {"max_chars": 5}, "/v1/x", "essai/qwen", "abc", "") for c in calls)
    # `session` : ce que la surface en dit, «» tant qu'aucune n'en a.
    go(h.run("cas", "{}", 0, session="conv-1"))
    assert calls.pop().session == "conv-1" and lines.pop()
    # Un nom qui n'est celui d'aucun outil : une erreur, sans statistiques.
    assert go(h.run("rm_rf", "{}", 0)) == tools.failure(
        "invalid_input", "unknown tool rm_rf.")
    assert len(lines) == len(attendu)



def test_contrat_delai_et_nombre_d_appels_propres_a_un_outil(monkeypatch):
    """Tool.timeout et Tool.max_calls : None = les bornes communes de
    [tools] ; posés, ils les REMPLACENT pour cet outil — un délai plus
    long ou plus court, un compte d'appels à lui."""
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.05)
    monkeypatch.setattr(tools, "MAX_CALLS", 2)

    async def lent(args):
        await asyncio.sleep(args.get("s", 0))
        return "fini"

    commun, patient, presse, compte = (
        outil("commun", run=lent), outil("patient", run=lent),
        outil("presse", run=lent), outil("compte", run=lent))
    patient.timeout, presse.timeout, compte.max_calls = 5, 0.01, 5
    h = hosted(commun, patient, presse, compte)
    assert texte(h, "commun", '{"s": 0.2}', 0) == (
        "Error: commun timed out after 0 s.")
    assert texte(h, "patient", '{"s": 0.2}', 0) == "fini"
    assert texte(h, "presse", '{"s": 0.03}', 0) == (
        "Error: presse timed out after 0 s.")
    # Le compte propre : sa limite, son texte ; la limite du client ne
    # peut que l'abaisser.
    assert not h.own("commun") and h.own("compte") and not h.own("inconnu")
    assert (h.cap(), h.cap(name="commun"), h.cap(name="compte"),
            h.cap(3, "compte"), h.cap(50, "compte")) == (2, 2, 5, 3, 5)
    assert texte(h, "compte", "{}", 4) == "fini"
    result = go(h.run("compte", "{}", 5))
    assert result == tools.failure("limit", (
        "the limit of 5 compte calls for one answer is reached. "
        "Answer now with what you already have."))


def test_contrat_fichiers_en_liens_et_code_failed(proxy, monkeypatch):
    """Result.files : rendus en LIENS par l'enveloppe de /v1/tools quand
    le proxy a une adresse publique ([files].public_url), ignorés sinon ;
    les mémoires et le tour suivant ne portent que `text`. Et `failed`,
    le code d'un outil qui a tourné et dit avoir échoué : une erreur comme
    une autre, comptée `error`."""
    from llm_proxy import anthropic_api, chat_api, files
    monkeypatch.setattr(files, "PUBLIC_URL", "")
    monkeypatch.setattr(files, "STORE", files.Store(60, 10_000, 5_000))
    lines = []
    monkeypatch.setattr(tools.stats, "record_tool", lambda *a: lines.append(a))
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 30)
    png = tools.Artifact("courbe.png", "image/png", b"\x89PNG")

    class Produit(Echo):
        name = "produit"

        async def run(self, args, call):
            if args.get("rate"):
                return tools.failure("failed", "exit status 1.")
            return tools.Result("x" * int(args.get("n", 3)), files=(png,),
                                meta={"files": [png.name]})

    monkeypatch.setattr(tools, "REGISTRY", [Produit()])
    proxy.hosted = h = type(proxy.hosted)(tools.enabled(), tools.Memory(4, 60))
    # L'exécuteur ne touche pas aux fichiers, même en coupant le texte.
    assert go(h.run("produit", '{"n": 40}', 0)) == tools.Result(
        "x" * 30 + "\n[truncated]", files=(png,), meta={"files": [png.name]})
    r = proxy.client.post("/v1/tools/produit", json={})
    assert r.json() == {"name": "produit", "result": "xxx", "is_error": False,
                        "error": None, "sources": [], "files": [],
                        "meta": {"files": ["courbe.png"]}}
    assert len(files.STORE) == 0    # sans adresse publique, rien n'est gardé
    r = proxy.client.post("/v1/tools/produit", json={"rate": True})
    assert r.json()["error"] == "failed" and r.json()["is_error"] is True
    assert r.json()["result"] == "Error: exit status 1."
    assert [line[3] for line in lines] == ["ok", "ok", "error"]
    assert "failed" in tools.ERRORS \
        and anthropic_api.ERROR_CODES["failed"] == "unavailable"

    # chat/completions : le tour suivant et la mémoire des échanges cachés
    # ne portent que le texte.
    monkeypatch.setattr(chat_api, "ENABLED", True)
    memory = chat_api.Memory(8, 60, 100_000)
    monkeypatch.setattr(chat_api, "MEMORY", memory)
    proxy.replies = [
        FakeUpstream(chat_doc({"content": None, "tool_calls": [{
            "id": "c", "function": {"name": "produit", "arguments": "{}"}}]},
            "tool_calls", 1, 1)),
        FakeUpstream(chat_doc({"content": "Fait."}, "stop", 1, 1))]
    r = proxy.client.post("/v1/chat/completions", json={
        "model": "essai/qwen", "tools": [{"type": "produit"}],
        "messages": [{"role": "user", "content": "Trace."}]})
    assert r.json()["choices"][0]["message"]["content"] == "Fait."
    assert proxy.sent[1]["messages"][-1] == {
        "role": "tool", "tool_call_id": "c", "content": "xxx"}
    assert len(memory) == 1 and "PNG" not in repr(memory.__dict__)

    # Avec une adresse publique : le fichier est gardé, et rendu en lien.
    monkeypatch.setattr(files, "PUBLIC_URL", "https://proxy.test")
    r = proxy.client.post("/v1/tools/produit", json={})
    (stored,) = files.STORE._data.values()
    # Le type servi se lit dans les OCTETS, pas dans ce que l'outil en
    # dit : quatre octets ne font pas un PNG, il sera téléchargé.
    assert r.json()["files"] == [{
        "name": "courbe.png", "media_type": "application/octet-stream",
        "size": 4,
        "url": f"https://proxy.test/v1/files/{stored.token}/courbe.png"}]


def test_boucle_un_outil_a_compte_propre_ne_pese_pas_sur_le_commun(
        proxy, monkeypatch):
    """Dans la boucle : les appels d'un outil à `max_calls` sont comptés
    contre SA limite, ceux des autres contre le plafond commun — et un
    modèle qui insiste au-delà de sa limite est arrêté de même."""
    from llm_proxy import app as A, chat_api
    monkeypatch.setattr(tools, "MAX_CALLS", 1)
    monkeypatch.setattr(A, "HOSTED_HARD_LIMIT", 1 + A.HOSTED_EXTRA_CALLS)
    monkeypatch.setattr(chat_api, "ENABLED", True)
    monkeypatch.setattr(chat_api, "MEMORY", chat_api.Memory(8, 60, 100_000))
    propre, commun = outil("propre"), outil("commun")
    propre.max_calls = 2
    monkeypatch.setattr(tools, "REGISTRY", [propre, commun])
    proxy.hosted = type(proxy.hosted)(tools.enabled(), tools.Memory(4, 60))

    def tour(*noms):
        return FakeUpstream(chat_doc({"content": None, "tool_calls": [
            {"id": f"c{i}", "function": {"name": n, "arguments": "{}"}}
            for i, n in enumerate(noms)]}, "tool_calls", 1, 1))

    def post():
        return proxy.client.post("/v1/chat/completions", json={
            "model": "essai/qwen",
            "tools": [{"type": "propre"}, {"type": "commun"}],
            "messages": [{"role": "user", "content": "Va."}]})

    proxy.replies = [tour("propre", "commun", "propre", "propre", "commun"),
                     FakeUpstream(chat_doc({"content": "Fini."}, "stop", 1, 1))]
    assert post().json()["choices"][0]["message"]["content"] == "Fini."
    rendus = [m["content"] for m in proxy.sent[1]["messages"]
              if m["role"] == "tool"]
    assert rendus == [
        "reçu {}", "reçu {}", "reçu {}",
        "Error: the limit of 2 propre calls for one answer is reached. "
        "Answer now with what you already have.",
        "Error: the limit of 1 tool calls for one answer is reached. "
        "Answer now with what you already have."]
    # Il insiste : 2 exécutés + 4 refus, puis la réponse est close.
    proxy.sent.clear()
    proxy.replies = [tour("propre") for _ in range(9)]
    assert post().status_code == 200 and len(proxy.sent) == 6


# ── le garde-fou commun ([tools.net]) et le téléchargement partagé ──────

def test_net_table_commune_et_repli_sur_l_ancienne_place(monkeypatch):
    """[tools.net] règle le garde-fou de tous les outils qui téléchargent.
    Un déploiement d'avant, dont les clés sont dans [tools.web_fetch],
    continue de marcher : elles y sont lues, et le démarrage le dit."""
    from llm_proxy import config

    def lu(conf):
        monkeypatch.setattr(config, "CONFIG", {"tools": conf})
        monkeypatch.setattr(net, "LEGACY", [])
        monkeypatch.setattr(net, "SHADOWED", [])
        return (net._setting("allow_private", config.flag),
                net._setting("allowed_domains", config.strings),
                net._setting("blocked_domains", config.strings),
                net.LEGACY, net.SHADOWED)

    assert lu({}) == (False, [], [], [], [])
    assert lu({"net": {"allow_private": True, "blocked_domains": ["x.test"]}}) \
        == (True, [], ["x.test"], [], [])
    # L'ancienne place, seule : lue, signalée.
    assert lu({"web_fetch": {"allow_private": True,
                             "allowed_domains": ["a.test"]}}) == (
        True, ["a.test"], [], ["allow_private", "allowed_domains"], [])
    # Les deux : [tools.net] fait foi, clé par clé.
    assert lu({"net": {"allow_private": False},
               "web_fetch": {"allow_private": True,
                             "blocked_domains": ["b.test"]}}) == (
        False, [], ["b.test"], ["blocked_domains"], ["allow_private"])
    # L'exemple du dépôt ne porte que la table commune.
    monkeypatch.undo()
    assert net.LEGACY == [] and net.SHADOWED == []
    assert config.get("tools.net.allow_private") is False
    assert config.get("tools.web_fetch.allow_private") is None


def test_net_download_bornes_de_l_outil_et_erreurs_du_contrat(monkeypatch):
    """net.download : le saut gardé et la boucle de redirections pour tout
    outil, qui n'apporte que ses bornes — un nombre d'octets, ou une
    fonction qui décide aux en-têtes puis aux premiers octets."""
    vus = []

    async def flux():
        for _ in range(10):
            vus.append(1)
            yield b"x" * 10

    web = Web({
        ("site.test", "/"): lambda q: httpx.Response(
            200, content=flux(), headers={"content-type": "audio/x"}),
        ("site.test", "/va"): httpx.Response(302, headers={"location": "/"}),
        ("site.test", "/429"): httpx.Response(429),
        ("site.test", "/sortie"): httpx.Response(
            302, headers={"location": "http://ailleurs.test/"}),
    })

    def dl(url, limit, settings=None):
        vus.clear()
        return go(net.download(
            url, settings, timeout=5, limit=limit, user_agent="essai",
            accept="audio/*", transport=httpx.MockTransport(web.handler)))

    url, r, body = dl("http://site.test/va", 25)
    assert (url, r.status_code, body) == ("http://site.test/", 200, b"x" * 25)
    assert len(vus) == 3                 # le flux n'est pas lu jusqu'au bout
    assert web.requests[-1].headers["user-agent"] == "essai"
    assert web.requests[-1].headers["accept"] == "audio/*"
    # Une fonction : rien lu sur la foi des en-têtes, ou assez vu.
    assert dl("http://site.test/", lambda r, body: 0)[2] == b"" and not vus
    assert dl("http://site.test/", lambda r, body: len(body) or 1)[2] == b"x" * 10

    def trop(r, body):
        raise tools.ToolError("unsupported", "too big.")

    with pytest.raises(tools.ToolError) as exc:
        dl("http://site.test/", trop)
    assert exc.value.code == "unsupported" and not vus
    # Les erreurs : celles du contrat, et elles seules.
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", ["ailleurs.test"])
    for url, settings, code, message in [
        ("http://site.test/429", None, "too_many_requests",
         "http://site.test/429 returned HTTP 429."),
        ("http://site.test/rien", None, "not_accessible",
         "http://site.test/rien returned HTTP 404."),
        ("http://127.0.0.1/", None, "not_allowed",
         "127.0.0.1 is a private or local address, which this proxy does "
         "not read."),
        ("ftp://site.test/", None, "invalid_input",
         "only http(s) URLs are read."),
        # Une redirection ne sort pas des listes : celle de [tools.net]…
        ("http://site.test/sortie", None, "not_allowed",
         "ailleurs.test is not a domain this proxy is allowed to read."),
        # … ni de celles du client, qui s'y ajoutent.
        ("http://site.test/", {"allowed_domains": ["autre.test"]},
         "not_allowed",
         "site.test is not a domain this proxy is allowed to read."),
    ]:
        with pytest.raises(tools.ToolError) as exc:
            dl(url, 100, settings)
        assert (exc.value.code, exc.value.message) == (code, message), url
