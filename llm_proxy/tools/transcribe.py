"""
`transcribe` : la transcription d'un fichier audio, pour que le modèle
puisse LIRE un enregistrement (réunion, podcast, message vocal) dont il
n'a que l'adresse. Le proxy télécharge le fichier, le confie au modèle de
transcription d'un de ses backends (API OpenAI `/v1/audio/transcriptions`,
multipart) et rend le texte.

La cible est choisie par le modèle : elle passe par le téléchargement
gardé commun (net.download, [tools.net]) — listes de domaines, adresses
publiques, à CHAQUE saut de redirection. Le fichier n'arrive QUE par
URL : ni pièce jointe, ni envoi.

Le modèle de transcription, lui, est une adresse de CONFIGURATION
([tools.transcribe].model = "<backend>/<modèle>") : il part par le client
HTTP du backend (backends.py), comme une requête relayée, sans passer par
app.py — pas de cycle d'import. Ce que app.py fait autour d'un relais est
refait ici, en petit : la porte de quota (backend à quotas dont
`/audio/transcriptions` n'est pas exempté) et la ligne de statistiques.

Une transcription coûte des secondes de GPU par minute d'audio : le TEXTE
est gardé en cache (le sien, plus long que le cache web — un
enregistrement ne change pas comme une page d'actualité), et chaque
morceau (`offset`) en sort sans rien refaire. L'audio, lui, n'est jamais
gardé.
"""

import asyncio
import time
from urllib.parse import urlsplit

import httpx

from .. import albert, backends, config, stats
from ..settings import is_exempt, log
from . import net, webcache
from .contract import Call, Result, Source, Tool, ToolError

ENABLED = config.flag("tools.transcribe.enabled", False)
# «<backend>/<modèle>», préfixé comme tout modèle du proxy. Vide → l'outil
# répond au modèle que la transcription n'est pas configurée.
MODEL = config.text("tools.transcribe.model", "").strip()
# Délai (s) de la requête de transcription. Avec celui du téléchargement,
# il fait le délai de l'outil (Transcribe.timeout), à la place de
# [tools].run_timeout : une heure d'audio ne se transcrit pas en 60 s.
TIMEOUT = config.num("tools.transcribe.timeout", 300)
# Délai (s) du téléchargement, redirections comprises.
DOWNLOAD_TIMEOUT = config.num("tools.transcribe.download_timeout", 60)
# Octets téléchargés au plus. Au-delà le fichier est REFUSÉ, pas coupé :
# une transcription partielle passerait pour entière, et un conteneur
# (MP4, WebM) coupé ne se décode pas. 25 Mo : la limite de l'API d'OpenAI,
# soit ~25 min de MP3 à 128 kbit/s ou ~2 h 30 de voix en Opus.
MAX_BYTES = config.integer("tools.transcribe.max_bytes", 25_000_000)
MAX_CHARS = config.integer("tools.transcribe.max_chars", 20_000)
# Langue passée au modèle de transcription quand l'appel n'en donne pas
# («fr», «en»…). Vide = il la détecte.
LANGUAGE = config.text("tools.transcribe.language", "").strip().lower()
USER_AGENT = "llm-proxy transcribe (+https://github.com/c4software/llm-proxy)"
ACCEPT = "audio/*,video/*;q=0.8,*/*;q=0.5"
PATH = "/v1/audio/transcriptions"
# L'endpoint des lignes de statistiques des requêtes au modèle de
# transcription : la route du proxy d'où elles naissent, pas celle du
# backend — ce que l'outil consomme d'un modèle se distingue ainsi de ce
# que les clients en consomment.
ENDPOINT = "/v1/tools/transcribe"

NAME = "transcribe"

# Le cache des transcriptions : (URL lue, texte, langue, durée). Borné en
# entrées et en caractères, en mémoire vive, commun à tous les clients
# comme le cache web — les garde-fous passent AVANT sa lecture.
CACHE = webcache.Cache(config.num("tools.transcribe.cache_ttl", 3600),
                       config.integer("tools.transcribe.cache_entries", 64),
                       4_000_000)

# Extension → type MIME : ce qui est annoncé au backend avec le fichier.
# L'extension compte : plus d'un serveur de transcription choisit son
# décodeur d'après le nom du fichier.
FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
           "ogg": "audio/ogg", "m4a": "audio/mp4", "aac": "audio/aac",
           "webm": "audio/webm", "amr": "audio/amr", "mp4": "video/mp4"}
# Les écritures d'un Content-Type qui ne se déduisent pas de FORMATS.
_TYPES = {v: k for k, v in FORMATS.items()} | {
    "audio/mp3": "mp3", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/x-flac": "flac", "audio/opus": "ogg", "application/ogg": "ogg",
    "audio/x-m4a": "m4a", "audio/m4a": "m4a", "video/webm": "webm"}
# Octets qu'il faut avoir lus pour reconnaître un format.
_HEAD = 12


def audio_kind(url: str, content_type: str, head: bytes) -> str | None:
    """L'extension du format («mp3»…, «» : de l'audio dont on ne sait pas
    le format), ou None si ce n'est pas de l'audio.

    Les premiers OCTETS décident d'abord : ils ne mentent pas, alors qu'un
    stockage objet sert volontiers un MP3 en `application/octet-stream`.
    À défaut, le Content-Type (`audio/…`, `video/…` : la piste son d'une
    vidéo se transcrit) fait foi. L'extension de l'URL ne PROUVE rien —
    c'est celui qui écrit l'URL qui la choisit — : elle ne sert qu'à
    nommer un fichier déjà reconnu comme audio."""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"\x1aE\xdf\xa3":  # EBML : WebM, Matroska
        return "webm"
    if head[:5] == b"#!AMR":
        return "amr"
    if head[:3] == b"ID3":
        return "mp3"
    # Mot de synchronisation MPEG (11 bits à 1) ; couche «00» = AAC (ADTS).
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "aac" if head[1] & 0x06 == 0 else "mp3"
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct.startswith(("audio/", "video/")) and ct != "application/ogg":
        return None
    ext = urlsplit(url).path.rpartition(".")[2].lower()
    return _TYPES.get(ct) or (ext if ext in FORMATS else "")


def _refused(url: str, why: str) -> ToolError:
    return ToolError("unsupported", f"{url} {why}.")


def _limit(r, body: bytes) -> int:
    """La borne de lecture d'une réponse (net.download) : rien d'une
    réponse qui n'est pas 2xx ni d'un fichier ANNONCÉ trop gros, et pas
    un octet de plus dès que les premiers disent que ce n'est pas de
    l'audio — une page HTML ou une archive de 25 Mo n'est pas téléchargée
    pour être refusée. Un octet au-delà de MAX_BYTES : de quoi savoir que
    le fichier le dépasse."""
    if not 200 <= r.status_code < 300 or _announced(r) > MAX_BYTES:
        return 0
    if len(body) >= _HEAD and audio_kind(
            str(r.request.url), r.headers.get("content-type", ""),
            body[:_HEAD]) is None:
        return len(body)
    return MAX_BYTES + 1


def _announced(r) -> int:
    length = r.headers.get("content-length", "")
    return int(length) if length.isdigit() else 0


async def download(url: str, settings, transport) -> tuple[str, bytes, str, str]:
    """(URL réellement lue, corps, format, type annoncé) d'un fichier
    audio, sous le garde-fou. Lève ToolError : ce qui n'est pas de
    l'audio, ou trop gros, est REFUSÉ — jamais coupé."""
    try:
        url, r, body = await asyncio.wait_for(net.download(
            url, settings, timeout=DOWNLOAD_TIMEOUT, limit=_limit,
            user_agent=USER_AGENT, accept=ACCEPT, transport=transport),
            DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        raise ToolError("not_accessible", (
            f"{url} took more than {int(DOWNLOAD_TIMEOUT)} s to download."))
    if not 200 <= r.status_code < 300:  # un 3xx sans Location
        raise ToolError("not_accessible",
                        f"{url} returned HTTP {r.status_code}.")
    if _announced(r) > MAX_BYTES or len(body) > MAX_BYTES:
        raise _refused(url, f"is larger than {MAX_BYTES} bytes, which this "
                            f"tool does not download")
    content_type = r.headers.get("content-type", "").split(";")[0].strip()
    kind = audio_kind(url, content_type, body[:_HEAD])
    if kind is None or not body:
        raise _refused(url, (
            f"is {content_type or 'not audio'}" if kind is None else "is empty")
            + ", which this tool cannot transcribe (audio files only: "
            + ", ".join(FORMATS) + ")")
    return url, body, kind, content_type


def _backend() -> tuple[backends.Backend, str]:
    """(backend, modèle sans préfixe) de [tools.transcribe].model, lu à
    l'appel : un modèle absent ou mal préfixé est une réponse au modèle,
    pas un proxy qui ne démarre pas."""
    backend, prefixed = backends.route_backend({"model": MODEL}) \
        if MODEL else (None, False)
    if backend is None or not prefixed or backend.client is None:
        if MODEL:
            log.warning("transcribe : modèle %r sans backend joignable "
                        "([tools.transcribe].model = \"<backend>/<modèle>\")",
                        MODEL)
        raise ToolError("unavailable",
                        "audio transcription is not configured on this proxy.")
    return backend, MODEL[len(backend.name) + 1:]


def _count(backend, model: str, status: int, started: float, usage) -> None:
    """La ligne de statistiques de la requête au backend, celle qu'écrit
    app.Call.done pour une transcription relayée : le modèle de
    transcription apparaît dans l'usage, qu'il serve un client ou l'outil.
    (L'exécution de l'outil, elle, a sa ligne à part : Hosted.run.)"""
    usage = usage if isinstance(usage, dict) else {}

    def tokens(*names) -> int:
        value = next((usage[n] for n in names if n in usage), 0)
        return value if isinstance(value, int) \
            and not isinstance(value, bool) else 0

    try:
        stats.record(MODEL, backend.name, model, ENDPOINT, status,
                     time.monotonic() - started,
                     tokens("input_tokens", "prompt_tokens"),
                     tokens("output_tokens", "completion_tokens"),
                     True, False)
    except Exception:  # les statistiques ne cassent jamais un outil
        log.exception("stats : transcription non enregistrée")


async def _transcribe(name: str, content_type: str, body: bytes,
                      language: str) -> tuple[str, str, float | None]:
    """Le fichier → (texte, langue, durée en secondes ou None), par le
    modèle de transcription configuré."""
    backend, model = _backend()
    # La porte de quota de app.gate, pour un backend à quotas dont le
    # chemin n'est pas exempté (il l'est par défaut : exempt_paths). Une
    # requête, coût 1 : des octets d'audio ne sont pas des tokens.
    if backend.quotas and not is_exempt(PATH):
        try:
            await backend.quota_state.get_limiter({"model": model}).acquire(1)
        except albert.QuotaWaitTooLong:
            raise ToolError("too_many_requests", (
                "the transcription model is over its rate limit. "
                "Try again later."))
    data = {"model": model, "response_format": "json"}
    if language:
        data["language"] = language
    started = time.monotonic()
    try:
        r = await backend.client.post(
            PATH, data=data, files={"file": (name, body, content_type)},
            headers=backend.auth_headers(),
            timeout=httpx.Timeout(TIMEOUT, connect=backend.connect_timeout))
    except httpx.HTTPError as exc:
        log.warning("transcribe : backend %s injoignable (%s)",
                    backend.name, type(exc).__name__)
        _count(backend, model, 503 if not backend.quotas else 502, started, None)
        raise ToolError("unavailable", (
            "the transcription backend is offline or did not answer. "
            "Try again later."))
    try:
        doc = r.json()
    except ValueError:  # `text` d'un backend qui ignore response_format
        doc = {"text": r.text} if r.status_code < 400 else None
    _count(backend, model, r.status_code, started,
           doc.get("usage") if isinstance(doc, dict) else None)
    if r.status_code >= 400:
        # Le corps de l'erreur reste dans le journal : il peut nommer le
        # backend, et le modèle n'en ferait rien.
        log.warning("transcribe : %s a répondu %d : %s", backend.name,
                    r.status_code, r.text[:300])
        if r.status_code == 429:
            raise ToolError("too_many_requests", (
                "the transcription model is busy. Try again later."))
        if r.status_code in (400, 413, 415, 422):
            raise ToolError("unsupported", (
                f"the transcription model could not read this audio file "
                f"(HTTP {r.status_code})."))
        raise ToolError("unavailable", (
            f"the transcription backend failed (HTTP {r.status_code})."))
    text = doc.get("text") if isinstance(doc, dict) else None
    if not isinstance(text, str):
        raise ToolError("unavailable",
                        "the transcription backend returned no text.")
    # La durée : `duration` (verbose_json) ou `usage.seconds` (ce que rend
    # un backend qui compte l'audio à la durée), si l'un des deux est là.
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    duration = next((float(d) for d in (doc.get("duration"),
                                        usage.get("seconds"))
                     if isinstance(d, (int, float))
                     and not isinstance(d, bool) and d > 0), None)
    heard = doc.get("language")
    return text.strip(), heard if isinstance(heard, str) and heard \
        else language, duration


def render(entry, offset: int = 0, max_chars: int | None = None) -> Result:
    """Le morceau demandé d'une transcription (`entry` : ce que garde le
    cache). Même en-tête et même reprise par `offset` que web_fetch ;
    `meta` : `url` (celle réellement lue), `total`, `language` et
    `duration` (secondes) s'ils sont connus, `range` si ce n'est pas la
    transcription entière."""
    url, text, language, duration = entry
    total = len(text)
    offset = min(max(offset, 0), total)
    shown = text[offset:offset + min(max_chars or MAX_CHARS, MAX_CHARS)]
    meta = {"url": url, "total": total}
    head = [f"URL: {url}"]
    if language:
        meta["language"] = language
        head.append(f"Language: {language}")
    if duration:
        meta["duration"] = duration
        head.append(f"Duration: {int(duration) // 60}:{int(duration) % 60:02d}")
    end = offset + len(shown)
    if offset or end < total:
        meta["range"] = (offset, end)
        head.append(f"Characters: {offset}-{end} of {total}"
                    + (f" (truncated: pass offset={end} to continue)"
                       if end < total else ""))
    return Result("\n".join(head) + "\n\n---\n" + (shown or "(no speech found)"),
                  meta=meta)


class Transcribe(Tool):
    name = NAME
    # Pas de liaison : ni l'API Responses ni l'API Messages n'ont d'outil
    # hébergé de transcription à remplacer. L'outil se déclare par son nom
    # sur /v1/chat/completions et s'appelle par /v1/tools/transcribe.

    @property
    def enabled(self) -> bool:
        return ENABLED

    @property
    def timeout(self) -> float:
        """Le délai de l'outil (contrat : Tool.timeout), à la place de
        [tools].run_timeout : le téléchargement, puis la transcription."""
        return DOWNLOAD_TIMEOUT + TIMEOUT

    def spec(self, present) -> dict:
        return {"type": "function", "function": {
            "name": NAME,
            "description": (
                "Transcribe the speech of an audio file, given its URL, and "
                "return the text (" + ", ".join(FORMATS) + f"; up to "
                f"{MAX_BYTES // 1_000_000} MB). The text has no timestamps "
                "and no speaker names. Long transcripts are truncated: pass "
                "`offset` to continue from a given character position (the "
                "audio is not transcribed again)."),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string",
                            "description": "The http(s) URL of the audio "
                                           "file."},
                    "language": {"type": "string",
                                 "description": "Language spoken, as an "
                                                "ISO 639-1 code (`en`, `fr`). "
                                                "Omit it to let the model "
                                                "detect it."},
                    "offset": {"type": "integer",
                               "description": "Character position to start "
                                              "from, to continue a truncated "
                                              "transcript."},
                },
                "required": ["url"],
            },
        }}

    def summary(self, args: dict, result: Result | None = None) -> dict:
        """L'URL, suivie de la plage rendue quand la transcription est lue
        en plusieurs morceaux — comme web_fetch, pour la même raison."""
        url = str(args.get("url") or "")
        span = result.meta.get("range") if url and result is not None else None
        if span:
            url = f"{url} [{span[0]}, {span[1]}]"
        return {"type": NAME, "url": url}

    async def run(self, args: dict, call: Call, transport=None) -> Result:
        """`transport` : celui du TÉLÉCHARGEMENT (tests). La requête de
        transcription part par le client du backend."""
        asked = args.get("url")
        if not isinstance(asked, str) or not asked.strip():
            raise ToolError("invalid_input", "`url` is required.")
        url = asked.strip()
        if url.startswith("www."):
            url = "https://" + url
        language = args.get("language")
        if language is None or language == "":
            language = LANGUAGE
        elif not isinstance(language, str) or not (
                language.isascii() and language.isalpha()
                and len(language) in (2, 3)):
            # «French», «fr-FR» : un backend les refuse (400) ou, pire,
            # les ignore. Le modèle corrige mieux qu'on ne devine.
            raise ToolError("invalid_input", (
                "`language` must be an ISO 639-1 code such as `en` or `fr`."))
        language = language.lower()
        offset = args.get("offset")
        offset = offset if isinstance(offset, int) \
            and not isinstance(offset, bool) else 0
        max_chars = call.settings.get("max_chars")

        def read(entry) -> Result:
            out = render(entry, offset, max_chars)
            return Result(out.text, sources=(Source(asked, asked),),
                          meta=out.meta)

        # La langue fait partie de la clé : la même URL transcrite «en
        # français» n'est pas la transcription en langue détectée.
        key = (url.split("#", 1)[0], language)
        net.check(url, call.settings)
        hit = CACHE.get(key)
        if hit is not None:
            return read(hit)
        # Non configuré : dit avant de télécharger 25 Mo pour rien.
        _backend()
        url, body, kind, content_type = await download(
            url, call.settings, transport)
        text, heard, duration = await _transcribe(
            f"audio.{kind}" if kind else "audio",
            FORMATS.get(kind) or content_type or "application/octet-stream",
            body, language)
        entry = (url, text, heard, duration)
        CACHE.put(key, entry, len(text))
        return read(entry)


TOOL = Transcribe()
