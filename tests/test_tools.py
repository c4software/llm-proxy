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
import json
import socket
import types

import httpx
import pytest

from fakes import FOUND, RESULTS
from llm_proxy import tools
from llm_proxy.tools import html_text, net, web_fetch, web_search

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
    monkeypatch.setattr(web_fetch, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 2_000_000)
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 20_000)
    monkeypatch.setattr(web_fetch, "ALLOWED_DOMAINS", [])
    monkeypatch.setattr(web_fetch, "BLOCKED_DOMAINS", [])
    monkeypatch.setattr(web_search, "SEARXNG_URL", "http://searx.test:8080")
    monkeypatch.setattr(web_search, "LIMIT", 8)
    monkeypatch.setattr(web_search, "LANGUAGE", "")
    monkeypatch.setattr(web_search, "CATEGORIES", "")
    monkeypatch.setattr(tools, "MAX_CALLS", 8)
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 60)
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 24_000)


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
        return go(web_fetch.run({"url": url, **args},
                                transport=httpx.MockTransport(self.handler)))


def page(text="bonjour", ct="text/plain", status=200, **headers):
    h = dict(headers)
    if ct is not None:
        h["content-type"] = ct
    return httpx.Response(status, content=text.encode() if isinstance(text, str)
                          else text, headers=h)


def redirect(location, status=302):
    return httpx.Response(status, headers={"location": location})


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

    out = go(web_search.run(args, transport=httpx.MockTransport(handler)))
    return out, requests


def res(n, **kw):
    return {"url": f"https://r.test/{n}", "title": f"Titre {n}",
            "content": f"extrait {n}", **kw}


def outil(name="echo", run=None, kinds=("web_search",)):
    async def defaut(args):
        return "reçu " + json.dumps(args, sort_keys=True)

    return types.SimpleNamespace(
        NAME=name, KINDS=kinds, ITEM_TYPE="web_search_call", ENABLED=True,
        action=lambda args: {"type": "search"}, run=run or defaut)


def hosted(*modules):
    return tools.Hosted(list(modules), tools.Memory(4, 60))


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
        assert "privée ou locale" in blocked(url), url
    for url in ["http://absent.test/", "http://jamais-vu.test/", "http://vide.test/"]:
        assert "introuvable" in blocked(url), url
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
    monkeypatch.setattr(web_fetch, "ALLOW_PRIVATE", True)
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
        assert out == ("Error: intern.test désigne une adresse privée ou "
                       "locale : refusé."), status
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
    monkeypatch.setattr(web_fetch, "ALLOWED_DOMAINS", ["site.test"])
    assert web.fetch("http://site.test/").endswith("ok")
    assert web.fetch("http://docs.site.test/").endswith("ok")
    refus = "Error: autre.test is not a domain this proxy is allowed to read."
    assert web.fetch("http://autre.test/") == refus
    assert len(web.requests) == 2          # rien n'est parti vers autre.test
    assert web.fetch("http://site.test/sortie") == refus
    assert [r.headers["host"] for r in web.requests[2:]] == ["site.test"]

    monkeypatch.setattr(web_fetch, "ALLOWED_DOMAINS", [])
    monkeypatch.setattr(web_fetch, "BLOCKED_DOMAINS", ["autre.test"])
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

    maxi = web_fetch.MAX_REDIRECTS
    out, requetes = chaine(maxi)
    assert out.endswith("arrivé") and requetes == maxi + 1
    assert chaine(maxi + 20) == ("Error: too many redirects.", maxi + 1)


# ── web_fetch : contenu ─────────────────────────────────────────────────

def test_fetch_ne_lit_que_les_types_textuels():
    for ct in ["image/png", "application/pdf", "IMAGE/PNG; q=1"]:
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
    assert go(h.run("echo", '{"b": 2, "a": "é"}', 0)) == 'reçu {"a": "\\u00e9", "b": 2}'
    # Sans arguments («», None) : objet vide, pas une exception.
    assert go(h.run("echo", "", 0)) == go(h.run("echo", None, 0)) == "reçu {}"


def test_hosted_appel_invalide_refuse_sans_executer():
    appels = []

    async def run(args):
        appels.append(args)
        return "x"

    h = hosted(outil(run=run))
    # Inconnu, même hors limite : c'est « inconnu » qui est dit.
    assert go(h.run("rm_rf", "{}", 99)) == "Error: unknown tool rm_rf."
    for arguments in ["pas du json", "{", "[1, 2]", '"chaîne"', "null", "{'a': 1}",
                      '{"a": 1} trop', {"query": "déjà un dict"}, 12]:
        assert go(h.run("echo", arguments, 0)) == (
            "Error: the tool arguments are not a JSON object."), arguments
    assert appels == []


def test_hosted_limite_d_appels_par_reponse(monkeypatch):
    monkeypatch.setattr(tools, "MAX_CALLS", 3)
    appels = []

    async def run(args):
        appels.append(1)
        return "ok"

    h = hosted(outil(run=run))
    assert [go(h.run("echo", "{}", n)) for n in (0, 1, 2)] == ["ok"] * 3
    for n in (3, 100):
        assert go(h.run("echo", "{}", n)).startswith(
            "Error: the limit of 3 web tool calls"), n
    # La limite du client (`max_uses`) ne peut que l'abaisser.
    assert go(h.run("echo", "{}", 1, limit=1)).startswith("Error: the limit of 1 ")
    assert go(h.run("echo", "{}", 3, limit=50)).startswith("Error: the limit of 3 ")
    assert len(appels) == 3


def test_hosted_echec_de_l_outil_rendu_en_texte(monkeypatch):
    for exc in (RuntimeError("boum"), httpx.ConnectError("x"), KeyError("k"),
                UnicodeEncodeError("ascii", "é", 0, 1, "x"), MemoryError()):
        async def casse(args):
            raise exc

        assert go(hosted(outil("casse", run=casse)).run("casse", "{}", 0)) == (
            f"Error: casse failed ({type(exc).__name__}).")
    # Délai dépassé : la coroutine est annulée, pas abandonnée.
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.05)
    fini = []

    async def lent(args):
        try:
            await asyncio.sleep(30)
        finally:
            fini.append("annulé")

    assert go(hosted(outil("lent", run=lent)).run("lent", "{}", 0)) == (
        "Error: lent timed out after 0 s.")
    assert fini == ["annulé"]


def test_hosted_resultat_tronque(monkeypatch):
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 10)

    async def bavard(args):
        return "x" * args["n"]

    h = hosted(outil("bavard", run=bavard))
    assert go(h.run("bavard", '{"n": 10}', 0)) == "x" * 10
    assert go(h.run("bavard", '{"n": 11}', 0)) == "x" * 10 + "\n[truncated]"


def test_hosted_retrouve_les_modules_par_type_d_outil_et_par_element():
    h = hosted(web_search, web_fetch)
    for kind in ("web_search", "web_search_preview", "web_search_2025_08_26"):
        assert h.for_kind(kind) == [web_search, web_fetch]
    # Type inconnu, ou correspondance partielle : rien.
    assert h.for_kind("function") == [] and h.for_kind("web") == []
    # L'élément rejoué est attribué d'après son action.
    for module, args in [(web_search, {"query": "x"}), (web_fetch, {"url": "u"})]:
        item = {"type": "web_search_call", "action": module.action(args)}
        assert h.for_item(item) is module, item
        assert args.items() <= item["action"].items()
    for item in [{"type": "web_search_call", "action": {"type": "find_in_page"}},
                 {"type": "web_search_call", "action": "search"},
                 {"type": "function_call", "action": {"type": "search"}}, {}]:
        assert h.for_item(item) is None, item
    # Sans l'outil de lecture, son élément n'est attribué à personne.
    assert hosted(web_search).for_item(
        {"type": "web_search_call", "action": {"type": "open_page"}}) is None


def test_hosted_par_defaut_les_outils_actives(monkeypatch):
    monkeypatch.setattr(web_search, "ENABLED", True)
    monkeypatch.setattr(web_fetch, "ENABLED", False)
    h = tools.Hosted()
    assert h.modules == [web_search] and h.memory is tools.MEMORY and bool(h)
    monkeypatch.setattr(web_search, "ENABLED", False)
    assert not tools.Hosted()           # aucun outil : app.py n'en présente pas


# ── web_search : le texte du modèle et sa forme structurée ──────────────
# (la surface Anthropic tire ses blocs `web_search_result` de ce texte)

def test_search_text_and_structure_say_the_same_thing():
    """`parse` est l'inverse de `render` : c'est ce qui permet de tirer
    les blocs du client du texte du modèle, et l'inverse au rejeu."""
    assert web_search.parse(FOUND) == RESULTS
    assert web_search.render("q", web_search.parse(FOUND)) == FOUND
    assert FOUND.split("\n") == [
        "[1] Releases · ggml-org/llama.cpp (2026-10-03)",
        "    https://github.com/ggml-org/llama.cpp/releases",
        "    LLM inference in C/C++ — b6789, «latest»…",
        "[2] llama.cpp (blog)",
        "    https://example.org/blog/llama"]
    # Ni une erreur, ni «aucun résultat», ni la marque de troncature ne
    # sont des résultats ; une entrée coupée avant son URL est écartée.
    assert web_search.parse("Error: search engine returned HTTP 500.") == []
    assert web_search.parse(web_search.render("q", [])) == []
    assert web_search.parse(FOUND + "\n[3] coupé\n[truncated]") == RESULTS
    # Un titre qui finit de lui-même par une date est lu comme daté, une
    # date d'une autre forme reste dans le titre : dans les deux cas le
    # texte, lui, revient à l'identique.
    for title, date in (("Notes (2024-05-01)", ""), ("Notes", "Jan 5, 202")):
        odd = web_search.render("q", [{"title": title, "date": date,
                                       "url": "https://x.test", "snippet": "s"}])
        assert web_search.render("q", web_search.parse(odd)) == odd
    assert web_search.parse(odd)[0]["title"] == "Notes (Jan 5, 202)"
    # La même liste que format_results tire des résultats bruts de SearXNG.
    raw = [{"title": " Un  titre ", "url": "https://a.test/x",
            "publishedDate": "2026-01-02T03:04:05", "content": " du\ntexte "},
           {"title": "sans url"}]
    assert web_search.entries(raw, 5) == [{
        "title": "Un titre", "url": "https://a.test/x", "date": "2026-01-02",
        "snippet": "du texte"}]
    assert web_search.format_results("q", raw, 5) == web_search.render(
        "q", web_search.entries(raw, 5))


def test_search_domain_filters(monkeypatch):
    raw = [{"title": str(n), "url": u} for n, u in enumerate([
        "https://github.com/ggml-org/llama.cpp",
        "https://docs.github.com/en/rest",
        "https://notgithub.com/x",
        "https://example.org/blog/post-1",
        "https://example.org/shop",
    ])]
    urls = lambda **kw: [e["url"] for e in web_search.entries(raw, 20, **kw)]
    # Sous-domaines couverts, pas les homonymes ; un sous-domaine précis ne
    # couvre pas son parent ; un chemin restreint à ce qui le prolonge.
    assert urls(allowed=["github.com"]) == [raw[0]["url"], raw[1]["url"]]
    assert urls(allowed=["docs.github.com"]) == [raw[1]["url"]]
    assert urls(allowed=["example.org/blog"]) == [raw[3]["url"]]
    assert urls(allowed=["https://Example.org/blog/*"]) == [raw[3]["url"]]
    assert urls(blocked=["github.com", "example.org/shop"]) == [
        raw[2]["url"], raw[3]["url"]]
    # Le filtre passe AVANT la limite.
    assert [e["url"] for e in web_search.entries(
        raw, 1, allowed=["example.org"])] == [raw[3]["url"]]

    def handler(request):
        assert request.url.params["q"] == "x"   # la requête n'est pas réécrite
        return httpx.Response(200, json={"results": raw})

    go = lambda **kw: asyncio.run(web_search.run(
        {"query": "x"}, transport=httpx.MockTransport(handler), **kw))
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

    async def run(args, transport=None):
        seen.append(args)
        return "Error: moteur éteint." if args.get("query") == "panne" \
            else "[1] Titre\n    https://e.org\n    extrait"
    monkeypatch.setattr(web_search, "run", run)
    return TestClient(A.app), seen


def test_route_tools_liste_les_outils_actifs_et_les_execute(routes):
    client, seen = routes
    (actif,) = client.get("/v1/tools").json()["data"]
    assert actif["name"] == "web_search" and "description" in actif
    assert actif["parameters"]["required"] == ["query"]
    r = client.post("/v1/tools/web_search", json={"query": "é", "limit": 3})
    assert r.status_code == 200
    assert r.json() == {"name": "web_search", "is_error": False,
                        "result": "[1] Titre\n    https://e.org\n    extrait"}
    assert seen == [{"query": "é", "limit": 3}]
    # Échec de l'outil : 200 quand même, c'est un texte pour le modèle.
    r = client.post("/v1/tools/web_search", json={"query": "panne"})
    assert r.status_code == 200 and r.json()["is_error"] is True


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
