"""Les outils hébergés (llm_proxy/tools/) : garde-fou réseau, lecture de
page, recherche, HTML → texte, mémoire et exécution bornée. Sur des
octets, SANS réseau : HTTP passe par httpx.MockTransport, le DNS par une
table (toute adresse hors table est refusée avant de quitter la machine).
La configuration est posée par monkeypatch sur les constantes des
modules, jamais lue dans config.example.toml."""

import asyncio
import json
import socket
import types

import httpx
import pytest

from llm_proxy import tools
from llm_proxy.tools import html_text, net, web_fetch, web_search

PUBLIC = "93.184.216.34"
PUBLIC6 = "2606:4700:4700::1111"


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
    monkeypatch.setattr(web_search, "SEARXNG_URL", "http://searx.test:8080")
    monkeypatch.setattr(web_search, "LIMIT", 8)
    monkeypatch.setattr(web_search, "LANGUAGE", "")
    monkeypatch.setattr(web_search, "CATEGORIES", "")
    monkeypatch.setattr(tools, "MAX_CALLS", 8)
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 60)
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 24_000)


# ── net : adresses ──────────────────────────────────────────────────────

@pytest.mark.parametrize("ip", [
    "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.254",
    "192.168.0.1", "192.168.255.255",
    "127.0.0.1", "127.1.2.3", "127.255.255.254",
    "169.254.0.1", "169.254.169.254",           # lien local, métadonnées cloud
    "100.64.0.1", "100.100.100.100", "100.127.255.254",  # CGNAT, Tailscale
    "0.0.0.0", "0.1.2.3", "255.255.255.255",
    "224.0.0.1", "239.255.255.250",             # multicast
    "240.0.0.1", "192.0.0.8", "192.0.2.1", "198.18.0.1", "198.51.100.7",
    "203.0.113.9",
    "::1", "::", "fe80::1", "fe80::1%eth0", "febf::1",
    "fc00::1", "fd12:3456:789a::1", "fdff::1",
    "::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
    "::ffff:100.64.0.1", "::ffff:0.0.0.0",
    "ff02::1", "ff0e::1",                       # multicast, même « global »
    "2001:db8::1",                              # documentation
    "2002:a00:1::1", "2002:7f00:1::1",          # 6to4 autour d'une IPv4 privée
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",     # Teredo
    "64:ff9b::a00:1", "64:ff9b::7f00:1",        # NAT64 autour d'une IPv4 privée
    "64:ff9b::a9fe:a9fe",                       # NAT64 → 169.254.169.254
    "::10.0.0.1", "::127.0.0.1",                # « compatibles IPv4 »
    "", "pas-une-ip", "10.0.0.1/8", "1.2.3", "999.1.1.1", " 8.8.8.8",
])
def test_is_public_refuse(ip):
    assert net.is_public(ip) is False


@pytest.mark.parametrize("ip", [
    "8.8.8.8", "1.1.1.1", PUBLIC, "100.63.255.255", "100.128.0.1",
    "172.15.255.255", "172.32.0.1", "169.253.255.255", "126.255.255.255",
    "128.0.0.1", "223.255.255.254",
    PUBLIC6, "2a00:1450:4007:80e::200e",
    "::ffff:8.8.8.8", "64:ff9b::808:808",
])
def test_is_public_accepte(ip):
    assert net.is_public(ip) is True


# ── net : URL ───────────────────────────────────────────────────────────

def blocked(url, **kw):
    with pytest.raises(net.Blocked) as exc:
        go(net.public_target(url, **kw))
    return str(exc.value)


def test_public_target_nom_public():
    assert go(net.public_target("http://site.test/x")) == ("http", PUBLIC, 80)
    assert go(net.public_target("https://site.test/x")) == ("https", PUBLIC, 443)
    assert go(net.public_target("HTTPS://SITE.TEST/")) == ("https", PUBLIC, 443)


def test_public_target_port_explicite():
    assert go(net.public_target("http://site.test:8080/")) == ("http", PUBLIC, 8080)
    assert go(net.public_target("https://site.test:8443/")) == ("https", PUBLIC, 8443)
    # «:» sans port, port 0 : le port du schéma.
    assert go(net.public_target("http://site.test:/")) == ("http", PUBLIC, 80)
    assert go(net.public_target("https://site.test:0/")) == ("https", PUBLIC, 443)


@pytest.mark.parametrize("url", [
    "http://site.test:99999/", "http://site.test:-1/", "http://site.test:abc/",
    "http://site.test:80:80/", "http://[::1/", "http://[pas-ipv6]/",
])
def test_public_target_url_invalide_est_refusee_pas_levee(url):
    """urlsplit et `.port` lèvent ValueError : ce doit rester un Blocked."""
    blocked(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "file://site.test/etc/passwd", "ftp://site.test/",
    "gopher://site.test:70/_x", "site.test", "site.test/chemin", "//site.test/",
    "javascript:alert(1)", "data:text/plain,x", "ws://site.test/",
    "http://", "http:///chemin", "https://:443/", "", "   ", "http:site.test",
    "unix:///var/run/docker.sock", "dict://site.test:11211/stat",
])
def test_public_target_schema_refuse(url, dns):
    blocked(url)
    assert dns["__seen__"] == []        # refusé avant toute résolution


def test_public_target_nom_prive_et_melange():
    assert "privée ou locale" in blocked("http://intern.test/")
    # UNE adresse privée suffit, même si la première est publique.
    assert "privée ou locale" in blocked("http://mixte.test/")


def test_public_target_melange_prive_en_premier(dns):
    dns["mixte2.test"] = ["::1", PUBLIC]
    blocked("http://mixte2.test/")
    dns["mixte3.test"] = [PUBLIC, PUBLIC6, "fe80::1%eth0"]
    blocked("http://mixte3.test/")


def test_public_target_introuvable(dns):
    assert "introuvable" in blocked("http://absent.test/")
    assert "introuvable" in blocked("http://jamais-vu.test/")   # hors table
    dns["vide.test"] = []
    assert "introuvable" in blocked("http://vide.test/")


def test_public_target_identifiants():
    # Les identifiants ne changent pas l'hôte résolu…
    assert go(net.public_target("http://user:pass@site.test/")) == ("http", PUBLIC, 80)
    assert go(net.public_target("http://user:pass@site.test:81/")) == ("http", PUBLIC, 81)
    # …et un nom public placé en « identifiant » ne masque pas la cible.
    blocked("http://site.test@127.0.0.1/")
    blocked("http://site.test:80@10.0.0.1:8009/")
    blocked("http://site.test@intern.test/")
    blocked("http://127.0.0.1#@site.test/")
    blocked("http://127.0.0.1?@site.test/")
    blocked("http://127.0.0.1/@site.test/")
    blocked("http://site.test\\@127.0.0.1/")
    blocked("http://a@b@127.0.0.1/")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8009/v1/models", "http://10.0.0.1/",
    "http://192.168.1.1/", "http://172.16.0.1/", "http://169.254.169.254/latest/",
    "http://100.64.0.1/", "http://0.0.0.0/", "http://255.255.255.255/",
    "http://224.0.0.1/",
    "http://[::1]/", "http://[::1]:8080/", "http://[::]/", "http://[fe80::1]/",
    "http://[fe80::1%25eth0]/", "http://[fc00::1]/", "http://[fd00::1]:8009/",
    "http://[::ffff:10.0.0.1]/", "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/", "http://[0:0:0:0:0:ffff:a00:1]/",
    "http://[::127.0.0.1]/", "http://[64:ff9b::10.0.0.1]/", "http://[ff02::1]/",
    # 127.0.0.1 sous toutes ses écritures (inet_aton) : entier décimal,
    # hexadécimal, octal, formes courtes, mélange, point final.
    "http://2130706433/", "http://0x7f000001/", "http://0x7F.0.0.1/",
    "http://0177.0.0.1/", "http://017700000001/", "http://127.1/",
    "http://127.0.1/", "http://0x7f.1/", "http://0/", "http://0x0/",
    "http://127.0.0.1./",
    # 10.0.0.1, 169.254.169.254, 192.168.1.1
    "http://167772161/", "http://0xa9fea9fe/", "http://2852039166/",
    "http://0300.0250.1.1/", "http://3232235777/",
    # Chiffres et points Unicode, que la résolution replie sur l'ASCII.
    "http://①②⑦.0.0.1/", "http://127。0。0。1/", "http://１２７.０.０.１/",
])
def test_public_target_litteral_non_public(url):
    blocked(url)


def test_public_target_litteral_public():
    assert go(net.public_target("http://8.8.8.8/")) == ("http", "8.8.8.8", 80)
    assert go(net.public_target(f"http://[{PUBLIC6}]/")) == ("http", PUBLIC6, 80)
    assert go(net.public_target(f"https://[{PUBLIC6}]:8443/x")) == (
        "https", PUBLIC6, 8443)
    # 8.8.8.8 en entier décimal : accepté, et c'est bien 8.8.8.8 qui est joint.
    assert go(net.public_target("http://134744072/")) == ("http", "8.8.8.8", 80)


@pytest.mark.parametrize("url", [
    "http://exa mple.test/", "http://a\x00b.test/", "http://" + "a" * 70 + ".test/",
    "http://a..b.test/", "http://.test/", "http://%31%32%37.0.0.1/",
])
def test_public_target_nom_tordu(url):
    blocked(url)


def test_public_target_allow_private():
    assert go(net.public_target("http://intern.test/", allow_private=True)) == (
        "http", "10.0.0.5", 80)
    # Même ouvert aux adresses privées : http(s) seulement.
    blocked("file:///etc/passwd", allow_private=True)


# ── web_fetch ───────────────────────────────────────────────────────────

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

    def fetch(self, args) -> str:
        return go(web_fetch.run(args, transport=httpx.MockTransport(self.handler)))


def page(text="bonjour", ct="text/plain", status=200, **headers):
    h = dict(headers)
    if ct is not None:
        h["content-type"] = ct
    return httpx.Response(status, content=text.encode() if isinstance(text, str)
                          else text, headers=h)


def redirect(location, status=302):
    return httpx.Response(status, headers={"location": location})


def test_fetch_vise_l_ip_verifiee_avec_host():
    web = Web(default=page("contenu"))
    out = web.fetch({"url": "http://site.test/a/b?x=1&y=é#fragment"})
    assert out.startswith("URL: http://site.test/a/b?x=1&y=é#fragment\n")
    assert out.endswith("\n---\ncontenu")
    (req,) = web.requests
    assert req.method == "GET"
    assert req.url.scheme == "http" and req.url.host == PUBLIC
    assert req.url.port is None and req.url.path == "/a/b"
    assert req.url.query == b"x=1&y=%C3%A9"
    assert req.headers["host"] == "site.test"
    assert req.headers["user-agent"] == web_fetch.USER_AGENT
    assert req.extensions["sni_hostname"] == "site.test"


def test_fetch_port_non_standard_dans_host():
    web = Web(default=page())
    web.fetch({"url": "http://site.test:8080/p"})
    web.fetch({"url": "https://site.test:8443/p"})
    web.fetch({"url": "https://site.test:443/p"})
    web.fetch({"url": "http://site.test:443/p"})    # 443 n'est pas le défaut de http
    a, b, c, d = web.requests
    assert (str(a.url), a.headers["host"]) == (f"http://{PUBLIC}:8080/p", "site.test:8080")
    assert (str(b.url), b.headers["host"]) == (f"https://{PUBLIC}:8443/p", "site.test:8443")
    assert (str(c.url), c.headers["host"]) == (f"https://{PUBLIC}/p", "site.test")
    assert (str(d.url), d.headers["host"]) == (f"http://{PUBLIC}:443/p", "site.test:443")
    assert b.extensions["sni_hostname"] == "site.test"


def test_fetch_chemin_vide_et_requete_seule():
    web = Web(default=page())
    web.fetch({"url": "http://site.test"})
    web.fetch({"url": "http://site.test?q=1"})
    assert [str(r.url) for r in web.requests] == [
        f"http://{PUBLIC}/", f"http://{PUBLIC}/?q=1"]


def test_fetch_identifiants_non_transmis_et_hote_non_masque():
    web = Web(default=page())
    assert not web.fetch({"url": "http://user:secret@site.test/x"}).startswith("Error")
    (req,) = web.requests
    assert req.url.host == PUBLIC and req.headers["host"] == "site.test"
    assert "authorization" not in req.headers and not req.url.userinfo
    # Antislash : pour urlsplit l'hôte est ce qui suit le dernier «@», et
    # c'est CET hôte qui est résolu, vérifié et joint — pas d'écart
    # possible entre l'analyse du contrôle et celle de la connexion.
    web.fetch({"url": "http://127.0.0.1\\@site.test/y"})
    assert web.requests[-1].url.host == PUBLIC
    assert web.requests[-1].headers["host"] == "site.test"
    assert web.fetch({"url": "http://site.test\\@127.0.0.1/"}).startswith("Error:")
    assert len(web.requests) == 2


def test_fetch_ipv6():
    web = Web(default=page())
    web.fetch({"url": "http://site6.test:8080/x"})
    web.fetch({"url": f"http://[{PUBLIC6}]/x"})
    web.fetch({"url": f"https://[{PUBLIC6}]:8443/x"})
    a, b, c = web.requests
    assert str(a.url) == f"http://[{PUBLIC6}]:8080/x"
    assert a.headers["host"] == "site6.test:8080"
    # Une IPv6 littérale garde ses crochets dans Host.
    assert b.url.host == PUBLIC6 and b.headers["host"] == f"[{PUBLIC6}]"
    assert c.headers["host"] == f"[{PUBLIC6}]:8443"


def test_fetch_nom_accentue_en_punycode(dns):
    """Un Host non ASCII faisait lever UnicodeEncodeError par httpx."""
    dns["exämple.test"] = [PUBLIC]
    web = Web(default=page("ok"))
    assert web.fetch({"url": "http://exämple.test/"}).endswith("ok")
    (req,) = web.requests
    assert req.headers["host"] == "xn--exmple-cua.test"
    assert req.extensions["sni_hostname"] == "xn--exmple-cua.test"


def test_fetch_www_sans_schema():
    web = Web(default=page())
    dns_out = web.fetch({"url": "  www.site.test/doc  "})
    # www.site.test n'est pas dans la table : refusé, mais en https.
    assert dns_out == "Error: hôte introuvable : www.site.test."


def test_fetch_www_sans_schema_joint_en_https(dns):
    dns["www.site.test"] = [PUBLIC]
    web = Web(default=page())
    assert web.fetch({"url": "www.site.test/doc"}).startswith(
        "URL: https://www.site.test/doc\n")
    assert str(web.requests[0].url) == f"https://{PUBLIC}/doc"


@pytest.mark.parametrize("args", [
    {}, {"url": ""}, {"url": "   "}, {"url": None}, {"url": 5},
    {"url": ["http://site.test/"]}, {"url": {"href": "http://site.test/"}},
    {"URL": "http://site.test/"},
])
def test_fetch_url_absente(args):
    web = Web(default=page())
    assert web.fetch(args) == "Error: `url` is required."
    assert web.requests == []


@pytest.mark.parametrize("url", [
    "site.test", "ftp://site.test/", "file:///etc/passwd", "gopher://site.test/",
    "javascript:alert(1)", "http://", "http://site.test:99999/", "http://[::1",
    "http://127.0.0.1/", "http://localhost.invalide/", "http://intern.test/",
    "http://mixte.test/", "http://2130706433/", "http://[::ffff:10.0.0.1]/",
    "http://169.254.169.254/latest/meta-data/", "http://100.100.100.100/",
    "http://site.test@10.0.0.1/", "http://a..b/",
])
def test_fetch_cible_refusee_sans_requete(url):
    web = Web(default=page("SECRET"))
    out = web.fetch({"url": url})
    assert out.startswith("Error:") and "SECRET" not in out
    assert web.requests == []


def test_fetch_caractere_de_controle_dans_le_chemin():
    """httpx.InvalidURL n'est pas une HTTPError : elle sortait de run()."""
    web = Web(default=page())
    out = web.fetch({"url": "http://site.test/a\x01b"})
    assert out.startswith("Error: could not fetch") and "InvalidURL" in out
    out = web.fetch({"url": "http://site.test/" + "a" * 70_000})
    assert out.startswith("Error:")
    assert web.requests == []


# redirections

def test_fetch_redirection_vers_prive_refusee_au_2e_saut():
    web = Web({("site.test", "/"): redirect("http://intern.test/admin")},
              default=page("SECRET"))
    out = web.fetch({"url": "http://site.test/"})
    assert out == "Error: intern.test désigne une adresse privée ou locale : refusé."
    assert len(web.requests) == 1       # rien n'est parti vers l'adresse privée


@pytest.mark.parametrize("location", [
    "http://127.0.0.1:8009/v1/models", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/", "http://[::ffff:192.168.1.1]/", "http://2130706433/",
    "http://0x7f.1/", "//intern.test/x", "//127.0.0.1/x", "http://mixte.test/",
    "file:///etc/passwd", "ftp://site.test/x", "gopher://127.0.0.1:6379/_INFO",
    "http://site.test@10.0.0.1/", "http://100.64.0.2:11434/api/tags",
    "http://absent.test/", "http://site.test:99999/", "http://[::1",
    "\\\\127.0.0.1/x", "http:\\\\127.0.0.1\\x",
])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_fetch_redirection_hostile(location, status):
    web = Web({("site.test", "/"): redirect(location, status)},
              default=page("SECRET"))
    out = web.fetch({"url": "http://site.test/"})
    # Soit refusée, soit restée sur l'hôte public de départ (un Location
    # à antislashs est un chemin relatif pour urljoin) : jamais ailleurs.
    assert all(r.url.host == PUBLIC and r.headers["host"] == "site.test"
               for r in web.requests)
    if len(web.requests) == 1:
        assert out.startswith("Error:") and "SECRET" not in out


def test_fetch_rebinding_entre_deux_sauts(dns):
    """Le même nom, public au premier saut, privé au second : chaque saut
    refait la résolution ET le contrôle, et une seule résolution par saut
    (celle qui est contrôlée est celle qui est jointe)."""
    reponses = iter([[PUBLIC], ["127.0.0.1"], [PUBLIC]])
    dns["rebind.test"] = lambda: next(reponses)
    web = Web({("rebind.test", "/"): redirect("/2")}, default=page("SECRET"))
    out = web.fetch({"url": "http://rebind.test/"})
    assert out.startswith("Error:") and "SECRET" not in out
    assert len(web.requests) == 1 and web.requests[0].url.host == PUBLIC
    assert dns["__seen__"] == ["rebind.test", "rebind.test"]


def test_fetch_une_seule_resolution_par_saut(dns):
    web = Web(default=page())
    web.fetch({"url": "http://site.test/"})
    assert dns["__seen__"] == ["site.test"]


def test_fetch_boucle_de_redirections():
    web = Web(default=lambda req: redirect("/encore"))
    assert web.fetch({"url": "http://site.test/"}) == "Error: too many redirects."
    assert len(web.requests) == web_fetch.MAX_REDIRECTS + 1


def test_fetch_ping_pong_entre_deux_hotes(dns):
    dns["autre.test"] = ["1.1.1.1"]
    web = Web({("site.test", "/"): redirect("http://autre.test/"),
               ("autre.test", "/"): redirect("http://site.test/")})
    assert web.fetch({"url": "http://site.test/"}) == "Error: too many redirects."
    assert [r.url.host for r in web.requests] == [PUBLIC, "1.1.1.1"] * 3


def test_fetch_autant_de_redirections_que_permis():
    n = {"vu": 0}

    def saut(req):
        n["vu"] += 1
        return redirect(f"/{n['vu']}") if n["vu"] <= web_fetch.MAX_REDIRECTS \
            else page("arrivé")

    web = Web(default=saut)
    out = web.fetch({"url": "http://site.test/"})
    assert out.endswith("arrivé")
    assert out.startswith(f"URL: http://site.test/{web_fetch.MAX_REDIRECTS}\n")


def test_fetch_redirection_relative_garde_hote_et_port():
    web = Web({("site.test:8080", "/a/c"): redirect("../b?x=1"),
               ("site.test:8080", "/b"): page("fin")})
    out = web.fetch({"url": "http://site.test:8080/a/c"})
    assert out == "URL: http://site.test:8080/b?x=1\nContent-Type: text/plain\n\n---\nfin"
    assert str(web.requests[1].url) == f"http://{PUBLIC}:8080/b?x=1"
    assert web.requests[1].headers["host"] == "site.test:8080"


def test_fetch_redirection_vers_autre_hote_public(dns):
    dns["autre.test"] = ["1.1.1.1"]
    web = Web({("site.test", "/"): redirect("https://autre.test:8443/p"),
               ("autre.test:8443", "/p"): page("là")})
    out = web.fetch({"url": "http://site.test/"})
    assert out.startswith("URL: https://autre.test:8443/p\n") and out.endswith("là")
    assert str(web.requests[1].url) == "https://1.1.1.1:8443/p"
    assert web.requests[1].extensions["sni_hostname"] == "autre.test"


def test_fetch_3xx_sans_location_ou_non_suivi():
    web = Web({("site.test", "/a"): page("corps", status=302),
               ("site.test", "/b"): page("choix", status=300,
                                         location="http://intern.test/")})
    assert web.fetch({"url": "http://site.test/a"}).endswith("corps")
    assert web.fetch({"url": "http://site.test/b"}).endswith("choix")
    assert len(web.requests) == 2


# contenu

@pytest.mark.parametrize("ct", [
    "image/png", "application/pdf", "application/octet-stream", "video/mp4",
    "application/zip", "IMAGE/PNG; q=1", "font/woff2",
])
def test_fetch_type_non_textuel_refuse(ct):
    web = Web(default=page(b"\x89PNG\r\n", ct=ct))
    out = web.fetch({"url": "http://site.test/f"})
    assert out.startswith("Error: http://site.test/f is ")
    assert "cannot read" in out and "PNG" not in out


@pytest.mark.parametrize("ct", [
    "text/plain", "text/markdown; charset=utf-8", "application/json",
    "application/xml", "application/rss+xml", "TEXT/Plain",
])
def test_fetch_types_textuels_tels_quels(ct):
    web = Web(default=page('{"a": "<b>pas du html</b>"}', ct=ct))
    assert web.fetch({"url": "http://site.test/"}).endswith(
        '\n---\n{"a": "<b>pas du html</b>"}')


def test_fetch_html_en_texte_avec_titre():
    html = ("<html><head><title> Ma  page </title><style>p{}</style></head>"
            "<body><h1>Titre</h1><p>Un <a href='https://a.test/x'>lien</a>.</p>"
            "<script>alert(1)</script></body></html>")
    web = Web(default=page(html, ct="text/html; charset=utf-8"))
    assert web.fetch({"url": "http://site.test/"}) == (
        "URL: http://site.test/\nTitle: Ma page\nContent-Type: text/html\n\n"
        "---\n# Titre\n\nUn lien (https://a.test/x).")


def test_fetch_sans_content_type():
    web = Web({("site.test", "/h"): page("<HTML><body><p>vu</p></body></html>", ct=None),
               ("site.test", "/t"): page("a < b", ct=None)})
    assert web.fetch({"url": "http://site.test/h"}) == "URL: http://site.test/h\n\n---\nvu"
    assert web.fetch({"url": "http://site.test/t"}) == "URL: http://site.test/t\n\n---\na < b"


def test_fetch_charset():
    web = Web({("site.test", "/l"): page("café".encode("latin-1"),
                                         ct="text/plain; charset=iso-8859-1"),
               # Charset inconnu de Python : LookupError sortait de run().
               ("site.test", "/x"): page("café".encode(),
                                         ct="text/plain; charset=inexistant-42"),
               ("site.test", "/b"): page(b"ok \xff\xfe fin", ct="text/plain")})
    assert web.fetch({"url": "http://site.test/l"}).endswith("café")
    assert web.fetch({"url": "http://site.test/x"}).endswith("café")
    assert web.fetch({"url": "http://site.test/b"}).endswith("ok �� fin")


def test_fetch_page_vide():
    web = Web(default=page("", ct="text/html"))
    assert web.fetch({"url": "http://site.test/"}).endswith("\n---\n(empty page)")


def test_fetch_troncature_et_offset(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 10)
    texte = "abcdefghijklmnopqrstuvwxy"         # 25 caractères
    web = Web(default=page(texte))

    def lit(**args):
        out = web.fetch({"url": "http://site.test/", **args})
        tete, corps = out.split("\n\n---\n")
        return [l for l in tete.split("\n") if l.startswith("Characters")], corps

    assert lit() == (["Characters: 0-10 of 25 (truncated: pass offset=10 to continue)"],
                     "abcdefghij")
    assert lit(offset=10) == (
        ["Characters: 10-20 of 25 (truncated: pass offset=20 to continue)"], "klmnopqrst")
    assert lit(offset=20) == (["Characters: 20-25 of 25"], "uvwxy")
    assert lit(offset=15) == (["Characters: 15-25 of 25"], "pqrstuvwxy")
    assert lit(offset=25) == (["Characters: 25-25 of 25"], "(empty page)")
    assert lit(offset=10**9) == (["Characters: 25-25 of 25"], "(empty page)")
    # Valeurs farfelues : depuis le début.
    for bizarre in (-5, 0, "10", 10.0, True, None, [10], {"a": 1}):
        assert lit(offset=bizarre)[1] == "abcdefghij"
    # Recoller les morceaux rend la page entière.
    assert "".join(lit(offset=o)[1] for o in (0, 10, 20)) == texte


def test_fetch_pas_de_ligne_characters_si_tout_tient(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 10)
    web = Web(default=page("0123456789"))
    assert "Characters" not in web.fetch({"url": "http://site.test/"})


def test_fetch_corps_plus_gros_que_max_bytes(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 100)
    web = Web(default=page("x" * 5000))
    out = web.fetch({"url": "http://site.test/"})
    assert "Note: only the first 100 bytes were downloaded." in out
    assert out.endswith("\n---\n" + "x" * 100)


def test_fetch_arrete_de_lire_a_max_bytes(monkeypatch):
    """Le flux n'est pas lu jusqu'au bout : un corps sans fin ne bloque pas."""
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 100)
    servis = []

    async def sans_fin():
        while True:
            servis.append(1)
            yield b"y" * 40

    web = Web(default=lambda req: httpx.Response(
        200, content=sans_fin(), headers={"content-type": "text/plain"}))
    out = web.fetch({"url": "http://site.test/"})
    assert out.endswith("\n---\n" + "y" * 100)
    assert len(servis) == 3


def test_fetch_coupe_au_milieu_d_un_caractere(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 3)
    web = Web(default=page("éé"))          # 4 octets, coupé au milieu du 2e
    assert web.fetch({"url": "http://site.test/"}).endswith("\n---\né�")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_fetch_statut_erreur(status):
    web = Web(default=page("SECRET", status=status))
    assert web.fetch({"url": "http://site.test/x"}) == (
        f"Error: http://site.test/x returned HTTP {status}.")


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refusé"), httpx.ConnectTimeout("lent"),
    httpx.ReadTimeout("lent"), httpx.RemoteProtocolError("tordu"),
    httpx.TooManyRedirects("x"), httpx.DecodingError("gzip"),
])
def test_fetch_exception_httpx(exc):
    def casse(req):
        raise exc

    out = Web(default=casse).fetch({"url": "http://site.test/x"})
    assert out == f"Error: could not fetch http://site.test/x ({type(exc).__name__})."


def test_fetch_exception_au_2e_saut_nomme_la_bonne_url(dns):
    dns["autre.test"] = ["1.1.1.1"]

    def casse(req):
        raise httpx.ConnectError("refusé")

    web = Web({("site.test", "/"): redirect("http://autre.test/y"),
               ("autre.test", "/y"): casse})
    assert web.fetch({"url": "http://site.test/"}) == (
        "Error: could not fetch http://autre.test/y (ConnectError).")


def test_fetch_allow_private(monkeypatch):
    monkeypatch.setattr(web_fetch, "ALLOW_PRIVATE", True)
    web = Web(default=page("interne"))
    assert web.fetch({"url": "http://intern.test:8009/"}).endswith("interne")
    assert str(web.requests[0].url) == "http://10.0.0.5:8009/"
    assert web.fetch({"url": "file:///etc/passwd"}).startswith("Error:")


def test_fetch_action():
    assert web_fetch.action({"url": "http://a.test/"}) == {
        "type": "open_page", "url": "http://a.test/"}
    assert web_fetch.action({}) == {"type": "open_page", "url": ""}
    assert web_fetch.action({"url": None}) == {"type": "open_page", "url": ""}
    assert web_fetch.action({"url": 12}) == {"type": "open_page", "url": "12"}


def test_render_direct(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_BYTES", 4)
    out = web_fetch.render("http://a.test/", "", b"abcd", None)
    assert out == ("URL: http://a.test/\nNote: only the first 4 bytes were "
                   "downloaded.\n\n---\nabcd")
    assert web_fetch.render("u", "text/plain", b"", None) == (
        "URL: u\nContent-Type: text/plain\n\n---\n(empty page)")


# ── web_search ──────────────────────────────────────────────────────────

class Searx:
    def __init__(self, reponse=None):
        self.reponse, self.requests = reponse, []

    def handler(self, request):
        self.requests.append(request)
        r = self.reponse
        if callable(r):
            return r(request)
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, json=r if r is not None else {"results": []})

    def search(self, args) -> str:
        return go(web_search.run(args, transport=httpx.MockTransport(self.handler)))


def res(n, **kw):
    return {"url": f"https://r.test/{n}", "title": f"Titre {n}",
            "content": f"extrait {n}", **kw}


def test_search_parametres_minimaux():
    s = Searx()
    s.search({"query": "  llama.cpp  rocm "})
    (req,) = s.requests
    assert req.method == "GET"
    assert req.url.scheme == "http" and req.url.host == "searx.test"
    assert req.url.port == 8080 and req.url.path == "/search"
    assert dict(req.url.params) == {"q": "llama.cpp  rocm", "format": "json",
                                    "pageno": "1"}


def test_search_parametres_complets(monkeypatch):
    monkeypatch.setattr(web_search, "LANGUAGE", "fr-FR")
    monkeypatch.setattr(web_search, "CATEGORIES", "general,it")
    s = Searx()
    s.search({"query": 'site:a.test "phrase exacte" -x & y=1 #é', "recency": "week"})
    assert dict(s.requests[0].url.params) == {
        "q": 'site:a.test "phrase exacte" -x & y=1 #é', "format": "json",
        "pageno": "1", "time_range": "week", "language": "fr-FR",
        "categories": "general,it"}


@pytest.mark.parametrize("recency", ["day", "week", "month", "year"])
def test_search_recency_valide(recency):
    s = Searx()
    s.search({"query": "x", "recency": recency})
    assert s.requests[0].url.params["time_range"] == recency


@pytest.mark.parametrize("recency", [
    "decade", "", None, 7, "Week", ["day"], {"a": 1}, True, "day&format=html"])
def test_search_recency_farfelue_ignoree(recency):
    s = Searx()
    s.search({"query": "x", "recency": recency})
    assert "time_range" not in s.requests[0].url.params
    assert s.requests[0].url.params["format"] == "json"


def test_search_la_requete_ne_peut_pas_changer_les_parametres():
    s = Searx()
    s.search({"query": "x&format=html&pageno=9"})
    p = s.requests[0].url.params
    assert p["q"] == "x&format=html&pageno=9"
    assert p.get_list("format") == ["json"] and p.get_list("pageno") == ["1"]
    assert s.requests[0].url.path == "/search"


def test_search_mise_en_forme():
    s = Searx({"results": [
        res(1, publishedDate="2026-01-02T10:20:30"),
        res(2, title="  Titre\n sur  deux\tlignes ", content=" a\n\n b  c "),
        res(3, content="", publishedDate=None),
        {"url": "https://r.test/4"},
    ]})
    assert s.search({"query": "q"}) == (
        "[1] Titre 1 (2026-01-02)\n"
        "    https://r.test/1\n"
        "    extrait 1\n"
        "[2] Titre sur deux lignes\n"
        "    https://r.test/2\n"
        "    a b c\n"
        "[3] Titre 3\n"
        "    https://r.test/3\n"
        "[4] https://r.test/4\n"
        "    https://r.test/4")


def test_search_extrait_tronque_a_240():
    s = Searx({"results": [res(1, content="m" * 239 + " " + "n" * 50),
                           res(2, content="p" * 240), res(3, content="p" * 241)]})
    lignes = s.search({"query": "q"}).split("\n")
    assert lignes[2] == "    " + "m" * 239 + "…"     # espace final retiré
    assert lignes[5] == "    " + "p" * 240           # pile 240 : entier
    assert lignes[8] == "    " + "p" * 240 + "…"


def test_search_resultats_sans_url_ignores():
    s = Searx({"results": [
        {"title": "sans url", "content": "x"}, {"url": "", "title": "vide"},
        {"url": None}, {"url": 42}, {"url": ["https://r.test/l"]},
        "une chaîne", None, 7, ["liste"],
        res(1), res(2),
    ]})
    out = s.search({"query": "q"})
    assert out.startswith("[1] Titre 1\n") and "\n[2] Titre 2\n" in out
    assert "[3]" not in out and "sans url" not in out and "vide" not in out


def test_search_champs_non_textuels():
    s = Searx({"results": [{"url": "https://r.test/1", "title": 404,
                            "content": ["a", "b"], "publishedDate": 20260102}]})
    assert s.search({"query": "q"}) == (
        "[1] 404 (20260102)\n    https://r.test/1\n    ['a', 'b']")


@pytest.mark.parametrize("limit,attendu", [
    (1, 1), (5, 5), (20, 20), (21, 20), (50, 20), (10**9, 20),
    (0, 1), (-3, 1),
    # Pas un entier : la valeur par défaut (LIMIT = 3 ici).
    (None, 3), ("5", 3), (2.5, 3), (5.0, 3), (True, 3), (False, 3),
    ([5], 3), ({"n": 5}, 3),
])
def test_search_limit(monkeypatch, limit, attendu):
    monkeypatch.setattr(web_search, "LIMIT", 3)
    s = Searx({"results": [res(n) for n in range(1, 31)]})
    out = s.search({"query": "q", "limit": limit})
    assert out.count("\n    https://") == attendu
    assert f"[{attendu}] Titre {attendu}\n" in out and f"[{attendu + 1}]" not in out


def test_search_limit_absent(monkeypatch):
    monkeypatch.setattr(web_search, "LIMIT", 2)
    s = Searx({"results": [res(n) for n in range(1, 31)]})
    assert s.search({"query": "q"}).count("https://") == 2


def test_search_aucun_resultat():
    for corps in ({"results": []}, {"results": None}, {}, {"results": [{"x": 1}]},
                  {"query": "q", "number_of_results": 0}):
        assert Searx(httpx.Response(200, json=corps)).search({"query": " q "}) == (
            "No results for «q».")


@pytest.mark.parametrize("corps", [
    b"<html>pas du json</html>", b"", b"{", b"\xff\xfe", b"[]", b"[1, 2]",
    b"null", b"3", b'"texte"', b'{"results": 5}', b'{"results": "abc"}',
    b'{"results": {"0": {"url": "https://r.test/"}}}', b'{"results": true}',
])
def test_search_reponse_illisible(corps):
    s = Searx(httpx.Response(200, content=corps,
                             headers={"content-type": "application/json"}))
    assert s.search({"query": "q"}) == "Error: unreadable search engine response."


def test_search_403_dit_quoi_regler():
    out = Searx(httpx.Response(403, text="Forbidden")).search({"query": "q"})
    assert out.startswith("Error:") and "search.formats" in out and "json" in out


@pytest.mark.parametrize("status", [201, 204, 301, 302, 400, 401, 404, 429, 500, 502])
def test_search_statut_autre_que_200(status):
    out = Searx(httpx.Response(status, json={"results": [res(1)]},
                               headers={"location": "http://site.test/"})
                ).search({"query": "q"})
    assert out == f"Error: search engine returned HTTP {status}."


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refusé"), httpx.ConnectTimeout("lent"),
    httpx.ReadTimeout("lent"), httpx.RemoteProtocolError("tordu")])
def test_search_moteur_injoignable(exc):
    def casse(req):
        raise exc

    assert Searx(casse).search({"query": "q"}) == (
        f"Error: search engine unreachable ({type(exc).__name__}).")


def test_search_non_configuree(monkeypatch):
    monkeypatch.setattr(web_search, "SEARXNG_URL", "")
    s = Searx()
    assert s.search({"query": "q"}) == (
        "Error: web search is not configured on this proxy.")
    assert s.requests == []


@pytest.mark.parametrize("args", [
    {}, {"query": ""}, {"query": "   \n"}, {"query": None}, {"query": 3},
    {"query": ["a"]}, {"q": "a"},
])
def test_search_requete_absente(args):
    s = Searx()
    assert s.search(args) == "Error: `query` is required."
    assert s.requests == []


def test_search_instance_privee_non_soumise_au_garde_fou(monkeypatch):
    """L'instance est une adresse de configuration : privée, et jointe."""
    monkeypatch.setattr(web_search, "SEARXNG_URL", "http://10.0.0.7:8888")
    s = Searx({"results": [res(1)]})
    assert s.search({"query": "q"}).startswith("[1] Titre 1")
    assert s.requests[0].url.host == "10.0.0.7"


def test_search_action():
    assert web_search.action({"query": "x"}) == {"type": "search", "query": "x"}
    assert web_search.action({}) == {"type": "search", "query": ""}
    assert web_search.action({"query": None}) == {"type": "search", "query": ""}


def test_format_results_direct():
    assert web_search.format_results("q", [], 5) == "No results for «q»."
    assert web_search.format_results("q", [res(1), res(2), res(3)], 2).count("https") == 2


# ── html_text ───────────────────────────────────────────────────────────

T = html_text.html_to_text


def test_html_document_vide():
    assert T("") == ("", "")
    assert T("   \n\t ") == ("", "")
    assert T("<html></html>") == ("", "")
    assert T("<!doctype html><!-- rien -->") == ("", "")


def test_html_ignore_script_style_nav():
    html = ("<html><head><title>T</title><style>body{color:red}</style>"
            "<script>var a = '</p><p>piège';</script></head><body>"
            "<nav><a href='https://a.test/'>Menu</a></nav><p>Vu</p>"
            "<script type='x'>if (a < b) { document.write('<p>non</p>') }</script>"
            "<noscript>activez js</noscript><svg><path d='M0'/><text>svg</text></svg>"
            "<template><p>gabarit</p></template><iframe src='x'>cadre</iframe>"
            "<button>Cliquer</button><select><option>choix</option></select>"
            "<footer>pied</footer><p>Aussi</p></body></html>")
    assert T(html) == ("T", "Vu\n\nAussi")


def test_html_titres():
    titre, texte = T("<h1>Un</h1><h2>Deux</h2><h3>Trois</h3><h4>Quatre</h4>"
                     "<h5>Cinq</h5><h6>  Six \n fois </h6><p>fin</p>")
    assert texte == ("# Un\n\n## Deux\n\n### Trois\n\n#### Quatre\n\n"
                     "##### Cinq\n\n###### Six fois\n\nfin")
    assert titre == ""


def test_html_titre_de_page():
    assert T("<title>  Ma \n page </title><p>x</p>") == ("Ma page", "x")
    assert T("<title>A &amp; B &eacute;</title>")[0] == "A & B é"
    # Le titre d'un <svg> du corps ne s'ajoute pas à celui de la page.
    assert T("<head><title>Page</title></head><body><svg><title>Icône</title>"
             "</svg><p>x</p></body>") == ("Page", "x")
    assert T("<title>Un</title><title>Deux</title>")[0] == "Un"


def test_html_listes():
    assert T("<ul><li>a</li><li>b <b>gras</b></li></ul><ol><li>c</ol>") == (
        "", "- a\n- b gras\n\n- c")


def test_html_liens():
    assert T("<p>Voir <a href='https://a.test/x?y=1&amp;z=2'>la doc</a> ici</p>")[1] == (
        "Voir la doc (https://a.test/x?y=1&z=2) ici")
    # Libellé identique à l'URL, ou vide : l'URL seule.
    assert T("<a href='https://a.test/'>https://a.test/</a>")[1] == "https://a.test/"
    assert T("<a href='https://a.test/'><img src='x.png'></a>")[1] == "https://a.test/"
    # Lien relatif, ancre, javascript:, sans href : le libellé seul.
    assert T("<a href='/x'>rel</a> <a href='#h'>ancre</a> "
             "<a href='javascript:void(0)'>js</a> <a>nu</a> <a href>vide</a>")[1] == (
        "rel ancre js nu vide")
    assert T("<a href='http://a.test/'><b>gras</b> et <i>ital</i></a>")[1] == (
        "gras et ital (http://a.test/)")


def test_html_pre_preserve():
    html = ("<p>Avant</p><pre>def f():\n    return  1\n\n\nx = [\n\t1,\n]\n</pre>"
            "<p>Après   coup</p>")
    assert T(html)[1] == ("Avant\n\n```\ndef f():\n    return  1\n\n\nx = [\n\t1,\n]\n"
                          "```\n\nAprès coup")
    assert T("<pre><code>a &lt; b &amp;&amp; c</code></pre>")[1] == (
        "```\na < b && c\n```")
    assert T("<pre>\n  indenté\n</pre>")[1] == "```\n  indenté\n```"


def test_html_entites():
    assert T("<p>caf&eacute; &lt;b&gt; &amp; &#233; &#x41; &nbsp;fin &euro; "
             "&inconnue; &copy</p>")[1] == "café <b> & é A fin € &inconnue; ©"


def test_html_blocs_et_espaces():
    assert T("<div>a</div><div>b</div><p>c<br>d</p>\n\n\n<section>e</section>")[1] == (
        "a\n\nb\n\nc\nd\n\ne")
    assert T("a   b\n\n\t c")[1] == "a b c"
    assert T("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr>"
             "</table>")[1] == "| A | B\n\n| 1 | 2"


def test_html_balises_non_fermees():
    # </head> omis (HTML valide) : le corps n'est pas avalé.
    assert T("<html><head><title>T</title><meta charset=utf-8><body><p>corps") == (
        "T", "corps")
    # <a> jamais fermé : le texte qui suit n'est pas perdu.
    assert T("<p>début <a href='https://a.test/'>lien <p>suite</p><p>fin</p>")[1] \
        .replace("\n", " ").split() == ["début", "lien", "suite", "fin",
                                        "(https://a.test/)"]
    assert T("<a href='https://a.test/'>un<a href='https://b.test/'>deux</a>")[1] == (
        "un (https://a.test/)deux (https://b.test/)")
    assert T("<p>a<p>b<li>c<li>d<h2>e")[1] == "a\nb\n- c\n- d\n\n## e"
    assert T("<pre>code\n  sans fin")[1] == "```\ncode\n  sans fin"
    assert T("<div><b>gras <i>ital</div> après")[1] == "gras ital\nafter".replace(
        "after", "après")


def test_html_page_entiere_dans_un_form():
    """ASP.NET : toute la page dans <form>. Elle sortait vide."""
    assert T("<body><form action='/'><input type=hidden name=v value=x>"
             "<h1>Titre</h1><p>Texte</p><button>Envoyer</button></form></body>") == (
        "", "# Titre\n\nTexte")


def test_html_mal_forme_ne_leve_pas():
    for html in ["<", "<<<>>>", "</p></div></nav></script>", "<p <b>x", "<a href=",
                 "<![CDATA[x]]>", "<!-- jamais fermé", "<?php echo 1 ?>", "&#xZZ; &#;",
                 "&#99999999999;", "<p>a</p" , "<script>jamais fermé", "<nav>",
                 "</nav><p>x</p>", "<svg/><p>y</p>", "\x00<p>\x00</p>",
                 "<a href='https://a.test/'>" * 500, "<div>" * 5000 + "x",
                 "<pre>" * 50 + "x" + "</pre>" * 3, "```\n<p>```</p>\n```"]:
        titre, texte = T(html)
        assert isinstance(titre, str) and isinstance(texte, str)
    assert T("</nav><p>x</p>")[1] == "x"
    assert T("<svg/><p>y</p>")[1] == "y"
    assert T("<div>" * 5000 + "x")[1] == "x"


# ── Memory ──────────────────────────────────────────────────────────────

@pytest.fixture
def horloge(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: t[0])
    return t


def test_memory_store_recall():
    m = tools.Memory(4, 60)
    assert m.recall("a") is None and len(m) == 0
    m.store("a", "web_search", '{"query": "x"}', "résultat")
    assert m.recall("a") == {"name": "web_search", "arguments": '{"query": "x"}',
                             "result": "résultat"}
    assert len(m) == 1


def test_memory_evince_le_plus_ancien():
    m = tools.Memory(3, 60)
    for k in "abcd":
        m.store(k, "n", "{}", k)
    assert len(m) == 3
    assert m.recall("a") is None
    assert [m.recall(k)["result"] for k in "bcd"] == ["b", "c", "d"]


def test_memory_recall_rafraichit():
    m = tools.Memory(3, 60)
    for k in "abc":
        m.store(k, "n", "{}", k)
    assert m.recall("a")["result"] == "a"      # «a» redevient le plus récent
    m.store("d", "n", "{}", "d")
    assert m.recall("b") is None
    assert m.recall("a") and m.recall("c") and m.recall("d")


def test_memory_ecrasement_d_une_cle():
    m = tools.Memory(2, 60)
    m.store("a", "n", "{}", "v1")
    m.store("b", "n", "{}", "b")
    m.store("a", "autre", '{"x": 1}', "v2")    # écrase ET rafraîchit
    assert len(m) == 2
    assert m.recall("a") == {"name": "autre", "arguments": '{"x": 1}', "result": "v2"}
    m.store("c", "n", "{}", "c")
    assert m.recall("b") is None and m.recall("a")["result"] == "v2"


def test_memory_expiration(horloge):
    m = tools.Memory(4, 60)
    m.store("a", "n", "{}", "a")
    horloge[0] += 60
    assert m.recall("a")["result"] == "a"      # pile la durée : encore là
    horloge[0] += 0.5
    assert m.recall("a") is None
    assert len(m) == 0                          # et retirée


def test_memory_recall_ne_prolonge_pas_la_duree(horloge):
    m = tools.Memory(4, 60)
    m.store("a", "n", "{}", "a")
    horloge[0] += 40
    assert m.recall("a")
    horloge[0] += 40
    assert m.recall("a") is None


def test_memory_reecriture_repart_de_zero(horloge):
    m = tools.Memory(4, 60)
    m.store("a", "n", "{}", "v1")
    horloge[0] += 50
    m.store("a", "n", "{}", "v2")
    horloge[0] += 50
    assert m.recall("a")["result"] == "v2"


def test_memory_bornes_degenerees():
    m = tools.Memory(0, 60)
    m.store("a", "n", "{}", "a")
    assert len(m) == 0 and m.recall("a") is None
    m = tools.Memory(1, 60)
    m.store("a", "n", "{}", "a")
    m.store("b", "n", "{}", "b")
    assert m.recall("a") is None and m.recall("b")["result"] == "b"


# ── Hosted ──────────────────────────────────────────────────────────────

def outil(name="echo", run=None, kinds=("web_search",), item="web_search_call",
          action="search"):
    async def defaut(args):
        return "reçu " + json.dumps(args, sort_keys=True)

    return types.SimpleNamespace(
        NAME=name, KINDS=kinds, ITEM_TYPE=item, ENABLED=True,
        action=lambda args: {"type": action}, run=run or defaut)


def test_hosted_run_passe_les_arguments():
    h = tools.Hosted([outil()], tools.Memory(4, 60))
    assert go(h.run("echo", '{"b": 2, "a": "é"}', 0)) == 'reçu {"a": "\\u00e9", "b": 2}'
    assert go(h.run("echo", "", 0)) == "reçu {}"
    assert go(h.run("echo", "{}", 0)) == "reçu {}"


def test_hosted_outil_inconnu():
    h = tools.Hosted([outil()], tools.Memory(4, 60))
    assert go(h.run("rm_rf", "{}", 0)) == "Error: unknown tool rm_rf."
    assert go(h.run("", "{}", 0)) == "Error: unknown tool ."
    # Inconnu ET hors limite : c'est « inconnu » qui est dit.
    assert go(h.run("rm_rf", "{}", 99)) == "Error: unknown tool rm_rf."
    assert go(tools.Hosted([], tools.Memory(4, 60)).run("echo", "{}", 0)) == (
        "Error: unknown tool echo.")


@pytest.mark.parametrize("arguments", [
    "pas du json", "{", "[1, 2]", '"chaîne"', "3", "null", "true", "{'a': 1}",
    '{"a": 1} trop', "\x00", {"query": "déjà un dict"}, 12, ["x"],
])
def test_hosted_arguments_non_json_ou_non_objet(arguments):
    appels = []

    async def run(args):
        appels.append(args)
        return "x"

    h = tools.Hosted([outil(run=run)], tools.Memory(4, 60))
    assert go(h.run("echo", arguments, 0)) == (
        "Error: the tool arguments are not a JSON object.")
    assert appels == []


def test_hosted_arguments_none():
    """Un appel sans arguments (None) : objet vide, et surtout pas une
    exception dans la ligne de journal."""
    h = tools.Hosted([outil()], tools.Memory(4, 60))
    assert go(h.run("echo", None, 0)) == "reçu {}"


def test_hosted_limite_d_appels(monkeypatch):
    monkeypatch.setattr(tools, "MAX_CALLS", 3)
    appels = []

    async def run(args):
        appels.append(1)
        return "ok"

    h = tools.Hosted([outil(run=run)], tools.Memory(4, 60))
    assert [go(h.run("echo", "{}", n)) for n in (0, 1, 2)] == ["ok"] * 3
    for n in (3, 4, 100):
        out = go(h.run("echo", "{}", n))
        assert out.startswith("Error: the limit of 3 web tool calls")
    assert len(appels) == 3


def test_hosted_delai_depasse(monkeypatch):
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.05)
    fini = []

    async def lent(args):
        try:
            await asyncio.sleep(30)
        finally:
            fini.append("annulé")
        return "jamais"

    h = tools.Hosted([outil("lent", run=lent)], tools.Memory(4, 60))
    assert go(h.run("lent", "{}", 0)) == "Error: lent timed out after 0 s."
    assert fini == ["annulé"]           # la coroutine est annulée, pas abandonnée


@pytest.mark.parametrize("exc", [
    RuntimeError("boum"), ValueError("x"), KeyError("k"), OSError("réseau"),
    ZeroDivisionError(), httpx.ConnectError("x"), UnicodeEncodeError("ascii", "é", 0, 1, "x"),
    TypeError("x"), RecursionError(), MemoryError(),
])
def test_hosted_exception_rendue_en_texte(exc):
    async def casse(args):
        raise exc

    h = tools.Hosted([outil("casse", run=casse)], tools.Memory(4, 60))
    assert go(h.run("casse", "{}", 0)) == (
        f"Error: casse failed ({type(exc).__name__}).")


def test_hosted_timeout_leve_par_l_outil():
    async def casse(args):
        raise TimeoutError("interne")

    h = tools.Hosted([outil("casse", run=casse)], tools.Memory(4, 60))
    assert go(h.run("casse", "{}", 0)).startswith("Error: casse ")


def test_hosted_resultat_tronque(monkeypatch):
    monkeypatch.setattr(tools, "MAX_RESULT_CHARS", 10)

    async def bavard(args):
        return "x" * args["n"]

    h = tools.Hosted([outil("bavard", run=bavard)], tools.Memory(4, 60))
    assert go(h.run("bavard", '{"n": 10}', 0)) == "x" * 10
    assert go(h.run("bavard", '{"n": 11}', 0)) == "x" * 10 + "\n[truncated]"
    assert go(h.run("bavard", '{"n": 5000}', 0)) == "x" * 10 + "\n[truncated]"
    assert go(h.run("bavard", '{"n": 0}', 0)) == ""


def test_hosted_bout_en_bout_avec_les_vrais_modules(monkeypatch):
    """Les vrais outils derrière Hosted.run : rien ne lève, même sur une
    cible refusée ou des arguments de mauvais type."""
    monkeypatch.setattr(web_search, "SEARXNG_URL", "")
    h = tools.Hosted([web_search, web_fetch], tools.Memory(4, 60))
    assert go(h.run("web_fetch", '{"url": "http://127.0.0.1:8009/"}', 0)).startswith(
        "Error: 127.0.0.1 désigne une adresse privée")
    assert go(h.run("web_fetch", '{"url": "http://site.test:99999/"}', 0)) == (
        "Error: URL invalide.")
    assert go(h.run("web_fetch", '{"url": ["x"], "offset": "a"}', 0)) == (
        "Error: `url` is required.")
    assert go(h.run("web_search", '{"query": "x", "limit": "beaucoup"}', 0)) == (
        "Error: web search is not configured on this proxy.")


def test_hosted_for_kind():
    a = outil("a", kinds=("web_search", "web_search_preview"))
    b = outil("b", kinds=("web_search",))
    c = outil("c", kinds=("code_interpreter",))
    h = tools.Hosted([a, b, c], tools.Memory(4, 60))
    assert h.for_kind("web_search") == [a, b]
    assert h.for_kind("web_search_preview") == [a]
    assert h.for_kind("code_interpreter") == [c]
    assert h.for_kind("function") == [] and h.for_kind("") == []
    assert h.for_kind("web") == []      # pas de correspondance partielle
    assert h.by_name == {"a": a, "b": b, "c": c}


def test_hosted_for_kind_vrais_modules():
    h = tools.Hosted([web_search, web_fetch], tools.Memory(4, 60))
    for kind in ("web_search", "web_search_preview", "web_search_2025_08_26"):
        assert h.for_kind(kind) == [web_search, web_fetch]
    assert [m.DEFINITION["function"]["name"] for m in h.modules] == [
        "web_search", "web_fetch"]
    for m in h.modules:
        f = m.DEFINITION["function"]
        assert m.DEFINITION["type"] == "function" and f["name"] == m.NAME
        assert set(f["parameters"]["required"]) <= set(f["parameters"]["properties"])


def test_hosted_for_item():
    h = tools.Hosted([web_search, web_fetch], tools.Memory(4, 60))
    assert h.for_item({"type": "web_search_call",
                       "action": {"type": "search", "query": "x"}}) is web_search
    assert h.for_item({"type": "web_search_call",
                       "action": {"type": "open_page", "url": "u"}}) is web_fetch
    for item in [
        {"type": "web_search_call", "action": {"type": "find_in_page"}},
        {"type": "web_search_call", "action": {}},
        {"type": "web_search_call", "action": "search"},
        {"type": "web_search_call", "action": None},
        {"type": "web_search_call"},
        {"type": "function_call", "action": {"type": "search"}},
        {"action": {"type": "search"}},
        {},
    ]:
        assert h.for_item(item) is None
    # Sans l'outil de lecture, son élément n'est attribué à personne.
    assert tools.Hosted([web_search], tools.Memory(4, 60)).for_item(
        {"type": "web_search_call", "action": {"type": "open_page"}}) is None


def test_hosted_enabled_et_defauts(monkeypatch):
    monkeypatch.setattr(web_search, "ENABLED", True)
    monkeypatch.setattr(web_fetch, "ENABLED", False)
    assert tools.enabled() == [web_search]
    h = tools.Hosted()
    assert h.modules == [web_search] and bool(h) is True
    assert h.memory is tools.MEMORY and h.expired == tools.EXPIRED
    monkeypatch.setattr(web_search, "ENABLED", False)
    assert tools.enabled() == [] and bool(tools.Hosted()) is False
    # Une liste vide explicite n'est pas « les outils activés ».
    monkeypatch.setattr(web_fetch, "ENABLED", True)
    assert tools.Hosted([]).modules == []
    m = tools.Memory(1, 1)
    assert tools.Hosted([web_fetch], m).memory is m
