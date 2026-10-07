"""L'outil hébergé `transcribe` (llm_proxy/tools/transcribe.py) :
téléchargement gardé de l'audio, reconnaissance du format, requête
multipart au modèle de transcription, découpage et cache du texte. SANS
réseau, comme test_tools.py : le web est un httpx.MockTransport, le DNS
une table, et le backend de transcription un faux «asr» dont le client
HTTP est lui aussi un MockTransport. La configuration est posée par
monkeypatch sur les constantes du module.

Un test par comportement ; les familles d'entrées sont des tables, et
l'assertion nomme l'entrée fautive."""

import asyncio
import re
import socket

import httpx
import pytest

import conftest  # noqa: F401 — pose CONFIG_PATH avant tout import du paquet

from llm_proxy import albert, backends, multipart, tools
from llm_proxy.tools import net, transcribe

PUBLIC = "93.184.216.34"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 32


def go(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Résolution sans réseau : nom → adresses ; hors table, seul un
    littéral numérique est résolu (aucune requête DNS)."""
    table = {"site.test": [PUBLIC], "autre.test": ["1.1.1.1"],
             "intern.test": ["10.0.0.5"]}
    real = socket.getaddrinfo

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        if host in table:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                    for ip in table[host]]
        return real(host, port, family, type, proto,
                    flags | socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class Asr:
    """Le faux backend de transcription : garde chaque requête reçue,
    répond `reply` (un JSON servi en 200, une httpx.Response, une
    fonction, ou une exception à lever)."""

    def __init__(self):
        self.requests, self.reply = [], {"text": "bonjour tout le monde"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.reply(request) if callable(self.reply) else self.reply
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) \
            else httpx.Response(200, json=reply)

    def champ(self, name: str, n: int = -1) -> str | None:
        """Un champ texte du formulaire de la requête n."""
        m = re.search(rb'name="%s"\r\n\r\n(.*?)\r\n--' % name.encode(),
                      self.requests[n].content, re.S)
        return m.group(1).decode() if m else None


@pytest.fixture(autouse=True)
def asr(monkeypatch):
    """Les réglages dont dépendent les tests, et le backend «asr» (sans
    quota) vers lequel pointe [tools.transcribe].model. `asr.lines` : les
    lignes de statistiques écrites pour les requêtes au backend."""
    env = Asr()
    b = backends.Backend("asr", {"url": "http://asr.invalid", "api_key": "k"})
    b.client = httpx.AsyncClient(base_url=b.url,
                                 transport=httpx.MockTransport(env.handler))
    env.backend, env.lines = b, []
    monkeypatch.setitem(backends.BACKENDS, "asr", b)
    monkeypatch.setattr(transcribe, "MODEL", "asr/whisper-test")
    monkeypatch.setattr(transcribe, "MAX_BYTES", 25_000_000)
    monkeypatch.setattr(transcribe, "MAX_CHARS", 20_000)
    monkeypatch.setattr(transcribe, "LANGUAGE", "")
    monkeypatch.setattr(transcribe, "TIMEOUT", 300)
    monkeypatch.setattr(transcribe, "ACCEPTED", list(transcribe.FORMATS))
    monkeypatch.setattr(transcribe, "RESPONSE_FORMAT", "verbose_json")
    monkeypatch.setattr(transcribe, "DOWNLOAD_TIMEOUT", 60)
    # Le garde-fou commun ([tools.net]).
    monkeypatch.setattr(net, "ALLOW_PRIVATE", False)
    monkeypatch.setattr(net, "ALLOWED_DOMAINS", [])
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", [])
    monkeypatch.setattr(transcribe.stats, "record",
                        lambda *a: env.lines.append(a))
    transcribe.CACHE.clear()
    monkeypatch.setattr(transcribe.CACHE, "ttl", 3600)
    return env


class Web:
    """Un faux web : `pages[(hôte, chemin)]` → réponse (ou fonction)."""

    def __init__(self, pages=None):
        self.pages, self.requests = pages or {}, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = self.pages.get((request.headers["host"], request.url.path))
        if page is None:
            return httpx.Response(404, text="rien")
        return page(request) if callable(page) else page

    def run(self, url, settings=None, **args) -> tools.Result:
        """Ce que l'exécuteur (tools.Hosted) fait d'un `run` : son Result,
        ou celui de l'échec prévu (ToolError)."""
        try:
            return go(transcribe.TOOL.run(
                {"url": url, **args}, tools.Call(settings or {}),
                transport=httpx.MockTransport(self.handler)))
        except tools.ToolError as exc:
            return tools.failure(exc.code, exc.message)


def audio(body=WAV, ct="audio/wav", status=200, **headers):
    h = dict(headers)
    if ct is not None:
        h["content-type"] = ct
    return httpx.Response(status, content=body, headers=h)


def un_son(**kw):
    return Web({("site.test", "/a.wav"): audio(**kw)})


# ── le contrat ──────────────────────────────────────────────────────────

def test_outil_enregistrable_et_presente_sans_liaison():
    t = transcribe.TOOL
    f = t.spec(frozenset({"transcribe"}))["function"]
    assert (t.name, f["name"], t.kinds) == (
        "transcribe", "transcribe", ("transcribe",))
    assert f["parameters"]["required"] == ["url"]
    assert set(f["parameters"]["properties"]) == {"url", "language", "offset"}
    assert t.responses is None and t.anthropic is None
    assert t.summary({"url": "https://site.test/a.wav"}) == {
        "type": "transcribe", "url": "https://site.test/a.wav"}


# ── de l'URL au texte ───────────────────────────────────────────────────

def test_transcription_fichier_envoye_au_backend_et_texte_rendu(asr):
    asr.reply = {"text": " bonjour tout le monde ", "language": "fr",
                 "usage": {"type": "duration", "seconds": 75}}
    web = un_son()
    out = web.run("https://site.test/a.wav")
    assert out.error is None
    assert out.text == ("URL: https://site.test/a.wav\nLanguage: fr\n"
                        "Duration: 1:15\n\n---\nbonjour tout le monde")
    assert out.meta == {"url": "https://site.test/a.wav", "total": 21,
                        "language": "fr", "duration": 75.0}
    assert out.sources == (tools.Source("https://site.test/a.wav",
                                        "https://site.test/a.wav"),)
    # Le téléchargement : vers l'adresse vérifiée, sous le nom demandé.
    assert web.requests[0].url.host == PUBLIC
    assert web.requests[0].headers["host"] == "site.test"
    # La requête au backend : celle qu'un client ferait au proxy, préfixe
    # retiré, clé du BACKEND, le fichier octet pour octet.
    [sent] = asr.requests
    assert (sent.method, sent.url.path) == ("POST", "/v1/audio/transcriptions")
    assert sent.headers["authorization"] == "Bearer k"
    assert multipart.model_field(
        sent.content, sent.headers["content-type"]) == "whisper-test"
    assert asr.champ("response_format") == "verbose_json"
    assert asr.champ("language") is None
    assert b'filename="audio.wav"' in sent.content and WAV in sent.content
    # La ligne de statistiques de cette requête, comme pour un relais.
    [line] = asr.lines
    assert line[:5] == ("asr/whisper-test", "asr", "whisper-test",
                        "/v1/tools/transcribe", 200)


def test_format_reconnu_aux_octets_puis_au_type_jamais_a_l_extension():
    cas = [  # (chemin, Content-Type, premiers octets) → extension, ou None
        (("/a", "application/octet-stream", WAV), "wav"),
        (("/a", "text/plain", MP3), "mp3"),
        (("/a", "", b"\xff\xfb\x90\x00" + b"\x00" * 8), "mp3"),
        (("/a", "", b"\xff\xf1\x50\x80" + b"\x00" * 8), "aac"),
        (("/a", "", b"fLaC" + b"\x00" * 8), "flac"),
        (("/a", "", b"OggS" + b"\x00" * 8), "ogg"),
        (("/a", "", b"\x00\x00\x00\x20ftypM4A "), "m4a"),
        (("/a", "", b"\x1aE\xdf\xa3" + b"\x00" * 8), "webm"),
        (("/a", "audio/x-m4a; x=1", b"\x00" * 12), "m4a"),
        (("/a", "video/mp4", b"\x00" * 12), "mp4"),
        (("/a.flac", "audio/x-inconnu", b"\x00" * 12), "flac"),
        (("/a", "audio/x-inconnu", b"\x00" * 12), ""),
        (("/a.mp3", "application/octet-stream", b"\x00" * 12), None),
        (("/a.mp3", "text/html", b"<!doctype html>"), None),
        (("/a.mp3", "", b""), None),
    ]
    for (path, ct, head), attendu in cas:
        assert transcribe.audio_kind(
            "https://site.test" + path, ct, head[:12]) == attendu, (path, ct, head)


def test_ce_qui_n_est_pas_de_l_audio_est_refuse_sans_appeler_le_backend(asr):
    cas = {
        "/page.mp3": audio(b"<!doctype html><html>" + b"x" * 500, "text/html"),
        "/vide.wav": audio(b"", "audio/wav"),
        "/doc.pdf": audio(b"%PDF-1.4\n" + b"x" * 500, "application/pdf"),
    }
    web = Web({("site.test", p): r for p, r in cas.items()})
    for path in cas:
        out = web.run("https://site.test" + path)
        assert out.error == "unsupported", path
        assert "cannot transcribe (audio files only: mp3, wav" in out.text, path
    assert asr.requests == [] and len(transcribe.CACHE) == 0


def test_fichier_trop_gros_refuse_et_non_coupe(monkeypatch, asr):
    monkeypatch.setattr(transcribe, "MAX_BYTES", 100)
    lus = []

    def flux(request):
        """Sans Content-Length : seule la lecture révèle la taille."""
        async def blocs():
            for _ in range(50):
                lus.append(1)
                yield WAV[:40] if len(lus) == 1 else b"\x00" * 40
        return httpx.Response(200, content=blocs(),
                              headers={"content-type": "audio/wav"})

    web = Web({("site.test", "/annonce.wav"): audio(WAV + b"\x00" * 200),
               ("site.test", "/flux.wav"): flux,
               ("site.test", "/juste.wav"): audio(WAV[:12] + b"\x00" * 88)})
    for path in ("/annonce.wav", "/flux.wav"):
        out = web.run("https://site.test" + path)
        assert out.error == "unsupported", path
        assert "is larger than 100 bytes" in out.text, path
    assert len(lus) == 3            # arrêté au bloc qui dépasse, pas au 50e
    assert asr.requests == []
    assert web.run("https://site.test/juste.wav").error is None


# ── le garde-fou réseau ─────────────────────────────────────────────────

def test_cible_absente_ou_refusee_erreur_sans_aucune_requete(asr):
    web = Web()
    cas = [({"url": ""}, "invalid_input", "`url` is required."),
           ({"url": 12}, "invalid_input", "`url` is required."),
           ({"url": "file:///etc/passwd"}, "invalid_input", "http(s)"),
           ({"url": "http://127.0.0.1/a.wav"}, "not_allowed", "privée"),
           ({"url": "http://intern.test/a.wav"}, "not_allowed", "privée"),
           ({"url": "https://site.test/a.wav", "language": "French"},
            "invalid_input", "ISO 639-1"),
           ({"url": "https://site.test/a.wav", "language": "fr-FR"},
            "invalid_input", "ISO 639-1")]
    for args, code, mot in cas:
        try:
            out = go(transcribe.TOOL.run(
                args, tools.Call(), transport=httpx.MockTransport(web.handler)))
        except tools.ToolError as exc:
            out = tools.failure(exc.code, exc.message)
        assert (out.error, mot in out.text) == (code, True), (args, out)
    assert web.requests == [] and asr.requests == []


def test_redirection_suivie_et_recontrolee_a_chaque_saut(monkeypatch, asr):
    def vers(location):
        return httpx.Response(302, headers={"location": location})

    web = Web({("site.test", "/r"): vers("https://autre.test/b.mp3"),
               ("autre.test", "/b.mp3"): audio(MP3, "audio/mpeg"),
               ("site.test", "/prive"): vers("http://intern.test/a.wav"),
               ("site.test", "/boucle"): vers("/boucle")})
    out = web.run("https://site.test/r")
    assert out.error is None and out.meta["url"] == "https://autre.test/b.mp3"
    assert out.sources[0].url == "https://site.test/r"
    assert b'filename="audio.mp3"' in asr.requests[0].content

    assert web.run("https://site.test/prive").error == "not_allowed"
    assert web.run("https://site.test/boucle").text == \
        "Error: too many redirects."
    assert len(asr.requests) == 1

    # Les listes de domaines : celles de la configuration et celles du
    # client (call.settings), à chaque saut — avant le cache, qui a gardé
    # la première transcription.
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", ["autre.test"])
    transcribe.CACHE.clear()
    assert web.run("https://site.test/r").error == "not_allowed"
    monkeypatch.setattr(net, "BLOCKED_DOMAINS", [])
    assert web.run("https://site.test/r",
                   {"allowed_domains": ["site.test"]}).error == "not_allowed"
    assert web.run("https://site.test/r").error is None
    assert web.run("https://site.test/r",
                   {"blocked_domains": ["site.test"]}).error == "not_allowed"
    assert len(asr.requests) == 2


def test_echec_du_telechargement_rendu_avec_son_code(asr):
    def panne(request):
        raise httpx.ConnectError("refusé")

    web = Web({("site.test", "/429"): audio(b"", status=429),
               ("site.test", "/500"): audio(b"", status=500),
               ("site.test", "/panne"): panne})
    for path, code, mot in (("/absent", "not_accessible", "HTTP 404"),
                            ("/429", "too_many_requests", "HTTP 429"),
                            ("/500", "not_accessible", "HTTP 500"),
                            ("/panne", "not_accessible", "ConnectError")):
        out = web.run("https://site.test" + path)
        assert (out.error, mot in out.text) == (code, True), (path, out)
    assert asr.requests == [] and len(transcribe.CACHE) == 0


# ── la langue, le découpage, le cache ───────────────────────────────────

def test_langue_de_l_appel_ou_de_la_configuration(monkeypatch, asr):
    web = un_son()
    out = web.run("https://site.test/a.wav", language="FR")
    assert asr.champ("language") == "fr"
    # Le backend ne dit pas la langue entendue : celle demandée est rendue.
    assert out.meta["language"] == "fr" and "Language: fr" in out.text
    monkeypatch.setattr(transcribe, "LANGUAGE", "de")
    web.run("https://site.test/a.wav")
    assert asr.champ("language") == "de"
    # Une langue = une transcription : trois requêtes, trois entrées.
    web.run("https://site.test/a.wav", language="en")
    assert len(asr.requests) == 3 and len(transcribe.CACHE) == 3


def test_transcription_longue_decoupee_par_offset_sans_rien_refaire(
        monkeypatch, asr):
    monkeypatch.setattr(transcribe, "MAX_CHARS", 10)
    asr.reply = {"text": "0123456789abcdefghijKLMNO"}
    web = un_son()
    un = web.run("https://site.test/a.wav")
    assert un.text.endswith(
        "Characters: 0-10 of 25 (truncated: pass offset=10 to continue)"
        "\n\n---\n0123456789")
    assert un.meta["range"] == (0, 10)
    assert transcribe.TOOL.summary({"url": "u"}, un) == {
        "type": "transcribe", "url": "u [0, 10]"}
    deux = web.run("https://site.test/a.wav#t=3", offset=10)
    assert deux.text.endswith("offset=20 to continue)\n\n---\nabcdefghij")
    trois = web.run("https://site.test/a.wav", offset=20)
    assert trois.text.endswith("Characters: 20-25 of 25\n\n---\nKLMNO")
    # Le client peut demander des morceaux plus petits, jamais plus grands.
    assert web.run("https://site.test/a.wav", {"max_chars": 4}).meta[
        "range"] == (0, 4)
    assert web.run("https://site.test/a.wav", {"max_chars": 99}).meta[
        "range"] == (0, 10)
    assert web.run("https://site.test/a.wav", offset="x").meta[
        "range"] == (0, 10)
    # Un téléchargement, une transcription, pour six lectures.
    assert len(web.requests) == 1 and len(asr.requests) == 1


def test_un_echec_n_est_jamais_garde_en_cache(asr):
    web = un_son()
    asr.reply = httpx.Response(500, text="boom")
    assert web.run("https://site.test/a.wav").error == "unavailable"
    asr.reply = {"text": ""}
    out = web.run("https://site.test/a.wav")
    assert out.error is None and out.text.endswith("---\n(no speech found)")
    assert len(asr.requests) == 2


# ── le backend de transcription ─────────────────────────────────────────

def test_echec_du_backend_rendu_avec_son_code_sans_son_message(asr):
    web = un_son()
    cas = [(httpx.Response(400, json={"error": {"message": "secret http://asr"}}),
            "unsupported", "could not read this audio file (HTTP 400)", 400),
           (httpx.Response(429, text="doucement"),
            "too_many_requests", "busy", 429),
           (httpx.Response(503, text="chargement"),
            "unavailable", "failed (HTTP 503)", 503),
           (httpx.Response(200, json={"résultat": "?"}),
            "unavailable", "returned no text", 200),
           (httpx.ConnectError("éteint"), "unavailable", "offline", 503)]
    for reply, code, mot, statut in cas:
        asr.reply = reply
        out = web.run("https://site.test/a.wav")
        assert (out.error, mot in out.text) == (code, True), (reply, out)
        assert "secret" not in out.text
        assert asr.lines[-1][4] == statut, reply
    # Un backend qui ignore response_format et rend du texte nu.
    asr.reply = httpx.Response(200, text="texte nu\n")
    assert web.run("https://site.test/a.wav").text.endswith("---\ntexte nu")


def test_modele_non_configure_dit_avant_tout_telechargement(monkeypatch, asr):
    web = un_son()
    for model in ("", "whisper-sans-prefixe", "inconnu/whisper"):
        monkeypatch.setattr(transcribe, "MODEL", model)
        out = web.run("https://site.test/a.wav")
        assert (out.error, out.text) == ("unavailable", (
            "Error: audio transcription is not configured on this proxy.")), model
    # Clients HTTP pas ouverts (hors de l'application) : même réponse.
    monkeypatch.setattr(transcribe, "MODEL", "asr/whisper-test")
    monkeypatch.setattr(asr.backend, "client", None)
    assert web.run("https://site.test/a.wav").error == "unavailable"
    assert web.requests == [] and asr.requests == []


def test_backend_a_quotas_passe_par_son_limiteur_sauf_chemin_exempte(
        monkeypatch, asr):
    """Ce que fait app.gate pour une transcription relayée."""
    pris = []

    class Limiteur:
        async def acquire(self, cost, wait_gone=None):
            pris.append(cost)
            if len(pris) > 1:
                raise albert.QuotaWaitTooLong(1200, "minute")
            return 0.0

    monkeypatch.setattr(asr.backend, "quotas", True)
    monkeypatch.setattr(asr.backend, "quota_state", type("Q", (), {
        "get_limiter": lambda self, payload: Limiteur()})())
    web = un_son()
    # Exempté (le défaut de proxy.exempt_paths) : pas de limiteur.
    monkeypatch.setattr(transcribe, "is_exempt", lambda path: True)
    assert web.run("https://site.test/a.wav").error is None and pris == []
    monkeypatch.setattr(transcribe, "is_exempt", lambda path: False)
    transcribe.CACHE.clear()
    assert web.run("https://site.test/a.wav").error is None and pris == [1]
    transcribe.CACHE.clear()
    assert web.run("https://site.test/a.wav").error == "too_many_requests"
    assert len(asr.requests) == 2


# ── par l'exécuteur ─────────────────────────────────────────────────────

def test_par_hosted_arguments_json_et_delai_propre(monkeypatch, asr):
    """L'outil tel que l'exécuteur l'appelle : `run(args, call)`, sans
    transport. Son délai est le SIEN (Tool.timeout : téléchargement +
    transcription), à la place de [tools].run_timeout."""
    seen = []

    async def download(url, settings, transport):
        seen.append(url)
        await asyncio.sleep(5 if "lent" in url else 0.05)
        return url, WAV, "wav", "audio/wav"

    monkeypatch.setattr(transcribe, "download", download)
    # Le délai commun, trop court pour le premier appel : il ne compte pas.
    monkeypatch.setattr(tools, "RUN_TIMEOUT", 0.01)
    monkeypatch.setattr(transcribe, "DOWNLOAD_TIMEOUT", 0.2)
    monkeypatch.setattr(transcribe, "TIMEOUT", 0.2)
    assert transcribe.TOOL.timeout == 0.4 and transcribe.TOOL.max_calls is None
    h = tools.Hosted([transcribe.TOOL], tools.Memory(4, 60))
    out = go(h.run("transcribe", '{"url": "https://site.test/a.wav"}', 0))
    assert out.error is None and out.text.endswith("bonjour tout le monde")
    out = go(h.run("transcribe", '{"url": "https://site.test/lent.wav"}', 0))
    assert (out.error, out.text) == (
        "timeout", "Error: transcribe timed out after 0 s.")
    assert seen == ["https://site.test/a.wav", "https://site.test/lent.wav"]
    assert len(asr.requests) == 1
    # Un outil qui n'est pas web : le refus par limite le dit.
    assert go(h.run("transcribe", "{}", 99)).text.startswith(
        "Error: the limit of 8 tool calls for one answer")


def test_au_registre_presente_sur_chat_completions_et_tools_seulement(
        monkeypatch):
    """Enregistré après web_fetch ; sans liaison : déclarable sur
    /v1/chat/completions et exécutable par /v1/tools, ignoré des surfaces
    Responses et Anthropic."""
    from llm_proxy import anthropic_api, responses_api
    names = [t.name for t in tools.REGISTRY]
    assert names.index("transcribe") > names.index("web_fetch")
    assert "transcribe" in tools.kinds()
    monkeypatch.setattr(transcribe, "ENABLED", True)
    h = tools.Hosted()
    assert h.for_kind("transcribe") == [transcribe.TOOL]
    assert transcribe.TOOL not in h.for_responses("web_search")
    assert h.for_server("transcribe_20260101") is None
    ctx = responses_api.to_chat({"model": "m", "input": "x", "tools": [
        {"type": "transcribe"}]}, hosted=h)[1]
    assert "transcribe" not in ctx.hosted and ctx.ignored == ["transcribe"]
    assert not anthropic_api.Context({"tools": [
        {"type": "transcribe_20260101", "name": "transcribe"}]}, h).hosted
    monkeypatch.setattr(transcribe, "ENABLED", False)
    assert transcribe.TOOL not in tools.enabled()


def test_formats_que_le_modele_lit_refus_avant_l_envoi(monkeypatch, asr):
    """[tools.transcribe].formats : un modèle de transcription qui ne lit
    que le WAV (Qwen3-ASR sous gufo, vu en vrai) ne reçoit que du WAV —
    le reste est refusé avant de lui être envoyé, sans être téléchargé en
    entier, et la description de l'outil le dit au modèle."""
    monkeypatch.setattr(transcribe, "ACCEPTED", ["wav"])
    lus = []

    def gros_mp3(request):
        async def blocs():
            for _ in range(50):
                lus.append(1)
                yield MP3
        return httpx.Response(200, content=blocs(),
                              headers={"content-type": "audio/mpeg"})

    web = Web({("site.test", "/a.wav"): audio(),
               ("site.test", "/a.mp3"): gros_mp3,
               ("site.test", "/inconnu"): audio(b"\x00" * 64, "audio/x-rare")})
    out = web.run("https://site.test/a.mp3")
    assert (out.error, out.text) == ("unsupported", (
        "Error: https://site.test/a.mp3 is mp3, which the transcription "
        "model of this proxy cannot read (it reads: wav). This tool does "
        "not convert audio."))
    assert len(lus) == 1 and asr.requests == []
    out = web.run("https://site.test/inconnu")
    assert out.error == "unsupported" and "audio of an unknown format" in out.text
    assert web.run("https://site.test/a.wav").error is None
    description = transcribe.TOOL.spec(())["function"]["description"]
    assert "return the text (wav; up to 25 MB)" in description
    # La configuration : des formats connus, au moins un.
    from llm_proxy import config
    for value, ok in ((["WAV", ".flac"], ["wav", "flac"]), (["wma"], None),
                      ([], None)):
        monkeypatch.setattr(config, "CONFIG", {"tools": {"transcribe": {
            "formats": value}}})
        if ok is None:
            with pytest.raises(SystemExit):
                transcribe._accepted()
        else:
            assert transcribe._accepted() == ok


def test_ce_que_rend_un_vrai_backend_langue_duree_et_refus_en_500(asr):
    """Les réponses de gufo (Qwen3-ASR), relevées le 07/10/2026 :
    `verbose_json` rend la langue par son NOM et la durée en secondes ;
    un fichier qu'il ne lit pas vaut un 500 dont le corps dit
    `invalid_request_error` — le fichier est en cause, pas le backend."""
    asr.reply = {"text": "And so, my fellow Americans.", "task": "transcribe",
                 "language": "english", "duration": 11, "segments": []}
    out = un_son().run("https://site.test/a.wav", language="en")
    assert out.text == ("URL: https://site.test/a.wav\nLanguage: english\n"
                        "Duration: 0:11\n\n---\nAnd so, my fellow Americans.")
    assert out.meta == {"url": "https://site.test/a.wav", "total": 28,
                        "language": "english", "duration": 11.0}
    assert asr.champ("language") == "en"
    transcribe.CACHE.clear()
    asr.reply = httpx.Response(500, json={"error": {
        "message": "Qwen3-ASR input must be a RIFF WAV",
        "type": "invalid_request_error", "code": "transcription_failed"}})
    out = un_son().run("https://site.test/a.wav")
    assert (out.error, out.text) == ("unsupported", (
        "Error: the transcription model could not read this audio file "
        "(HTTP 500)."))
    assert "RIFF" not in out.text and len(transcribe.CACHE) == 0
