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

CONVERSION ([tools.transcribe].convert, `convert`). Un modèle de
transcription ne lit pas tout — Qwen3-ASR sous gufo, que du WAV. Ce que
`formats` ne liste pas est converti ici en WAV 16 kHz mono par ffmpeg,
puis envoyé. ffmpeg décode alors des octets HOSTILES, venus du web :
  * un sous-processus sans shell, aux arguments FIXES — rien n'y vient de
    l'URL ni du fichier —, sans entrée standard, sans environnement que
    PATH, dans un dossier temporaire à lui (0700), supprimé dans tous les
    cas avec l'entrée et la sortie ;
  * le format d'entrée est IMPOSÉ (`-f`, d'après les premiers octets déjà
    reconnus) : pas de sondage, donc aucun des « formats » qui ne sont
    que des listes d'autres fichiers (HLS, concat, SDP…) ; et le seul
    protocole permis est `file` (`-protocol_whitelist`) : ni réseau, ni
    tube, ni `data:` ;
  * un délai (`convert_timeout`), au-delà duquel le processus est tué, et
    une sortie bornée (`convert_max_bytes`, par `-fs`) : un fichier qui
    la dépasse est REFUSÉ, pas coupé ;
  * hors de la boucle asyncio (sous-processus asyncio, écritures dans un
    fil), et tué si l'appel est annulé.
Ce que cela ne fait PAS : isoler ffmpeg du proxy. Une faille de décodeur
s'exécuterait avec les droits du proxy — d'où `convert = false` pour qui
préfère refuser.
ffmpeg absent (lancement hors conteneur) : le proxy démarre, app.py le
dit, et un format à convertir est refusé comme sans conversion.
"""

import asyncio
import os
import shutil
import tempfile
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


def _accepted() -> list[str]:
    """[tools.transcribe].formats : les formats que le modèle de
    transcription lit TELS QUELS, parmi FORMATS. Un modèle qui ne lit que
    le WAV — Qwen3-ASR sous gufo, vu le 07/10/2026 — ne recevra que du
    WAV : le reste est converti (convert), ou refusé."""
    names = [f.lower().lstrip(".") for f in config.strings(
        "tools.transcribe.formats", FORMATS)]
    unknown = [f for f in names if f not in FORMATS]
    if unknown or not names:
        raise SystemExit(
            f"{config.CONFIG_PATH} : tools.transcribe.formats doit lister "
            f"des formats parmi {', '.join(FORMATS)} (reçu {unknown or names})")
    return names


ACCEPTED = _accepted()
# Le `response_format` demandé au backend. «verbose_json» rend, en plus du
# texte, la langue entendue et la durée (vu sous gufo : `language`,
# `duration`) ; «json» ne rend que le texte — pour un backend qui refuse
# l'autre.
RESPONSE_FORMAT = config.text("tools.transcribe.response_format",
                              "verbose_json").strip() or "json"


# Convertir en WAV ce que `formats` ne liste pas (tête de module).
CONVERT = config.flag("tools.transcribe.convert", True)
# Le binaire : un nom cherché dans PATH, ou un chemin. Cherché UNE fois, à
# l'import : «» = pas de conversion, dit au démarrage (app.py). Sans objet
# pour un backend qui ne lit pas le WAV : c'est en WAV qu'on convertit.
FFMPEG = (shutil.which(config.text("tools.transcribe.ffmpeg", "ffmpeg")
                       .strip() or "ffmpeg") or "") \
    if CONVERT and "wav" in ACCEPTED else ""
# Délai (s) d'une conversion ; s'ajoute au délai de l'outil.
CONVERT_TIMEOUT = config.num("tools.transcribe.convert_timeout", 60)
# Octets du WAV produit, au plus. 16 kHz mono 16 bits = 1,92 Mo par
# minute : 100 Mo ≈ 52 min. Au-delà le fichier est refusé — coupé, sa
# transcription passerait pour entière. C'est aussi ce que le proxy tient
# en mémoire le temps de l'envoi.
CONVERT_MAX_BYTES = config.integer("tools.transcribe.convert_max_bytes",
                                   100_000_000)
# Format reconnu → démultiplexeur IMPOSÉ à ffmpeg (`-f`).
DEMUXERS = {"mp3": "mp3", "wav": "wav", "flac": "flac", "ogg": "ogg",
            "m4a": "mov", "mp4": "mov", "aac": "aac", "webm": "matroska",
            "amr": "amr"}


def _offered() -> list[str]:
    """Les formats que l'outil annonce au modèle : ceux que le backend
    lit, plus ceux que ffmpeg lui convertit."""
    return list(FORMATS) if FFMPEG else ACCEPTED


def _readable(kind: str | None) -> bool:
    """Ce format part-il au modèle de transcription, tel quel ou
    converti ? Un audio de format inconnu («») ne part que si rien n'est
    restreint — et n'est jamais converti : sans format à imposer, ffmpeg
    sonderait."""
    if kind is None:
        return False
    return kind in ACCEPTED or (not kind and len(ACCEPTED) == len(FORMATS)) \
        or bool(FFMPEG and kind in DEMUXERS)


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
    l'audio, ou pas un format que le modèle de transcription lit — une
    page HTML ou une archive de 25 Mo n'est pas téléchargée pour être
    refusée. Un octet au-delà de MAX_BYTES : de quoi savoir que le
    fichier le dépasse."""
    if not 200 <= r.status_code < 300 or _announced(r) > MAX_BYTES:
        return 0
    if len(body) >= _HEAD and not _readable(audio_kind(
            str(r.request.url), r.headers.get("content-type", ""),
            body[:_HEAD])):
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
            + ", ".join(_offered()) + ")")
    if not _readable(kind):
        # De l'audio, mais pas pour ce modèle, et pas de conversion ici
        # (désactivée, ffmpeg absent, format inconnu) : dit avant l'envoi.
        raise _refused(url, (
            f"is {kind or 'audio of an unknown format'}, which the "
            f"transcription model of this proxy cannot read (it reads: "
            + ", ".join(ACCEPTED) + "). This tool does not convert audio"))
    return url, body, kind, content_type


def _ffmpeg_args(kind: str, limit: int) -> list[str]:
    """Les arguments de la conversion — FIXES : seuls le démultiplexeur
    (une valeur de DEMUXERS) et la borne changent. `in` et `out.wav` sont
    des noms du dossier de travail."""
    return [FFMPEG, "-nostdin", "-hide_banner", "-nostats",
            "-loglevel", "error", "-y",
            "-protocol_whitelist", "file", "-f", DEMUXERS[kind], "-i", "in",
            # La première piste son, et rien d'autre : ni image (la
            # pochette d'un MP3, la vidéo d'un MP4), ni sous-titres, ni
            # données, ni métadonnées.
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            "-fs", str(limit), "-f", "wav", "out.wav"]


def _work() -> str:
    return tempfile.mkdtemp(prefix="llm-proxy-audio-")


def _put(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _take(path: str, limit: int) -> bytes:
    """Le fichier, lu jusqu'à `limit` + 1 octets ; b"" s'il n'existe pas."""
    try:
        with open(path, "rb") as fh:
            return fh.read(limit + 1)
    except OSError:
        return b""


async def convert(url: str, body: bytes, kind: str) -> bytes:
    """`body` (un fichier de format `kind`) en WAV 16 kHz mono, par ffmpeg
    (tête de module). Lève ToolError : `unsupported` si ffmpeg ne le
    décode pas ou si le WAV dépasse CONVERT_MAX_BYTES, `timeout` au-delà
    de CONVERT_TIMEOUT."""
    started = time.monotonic()
    work = await asyncio.to_thread(_work)
    proc = None
    try:
        await asyncio.to_thread(_put, os.path.join(work, "in"), body)
        with open(os.path.join(work, "err"), "wb") as err:
            proc = await asyncio.create_subprocess_exec(
                *_ffmpeg_args(kind, CONVERT_MAX_BYTES), cwd=work,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=err,
                env={"PATH": os.environ.get("PATH", os.defpath)})
        try:
            code = await asyncio.wait_for(proc.wait(), CONVERT_TIMEOUT)
        except asyncio.TimeoutError:
            raise ToolError("timeout", (
                f"converting {url} to WAV took more than "
                f"{int(CONVERT_TIMEOUT)} s. Do not retry with this file."))
        wav = await asyncio.to_thread(
            _take, os.path.join(work, "out.wav"), CONVERT_MAX_BYTES)
        if code != 0 or len(wav) <= 44:     # 44 octets : l'en-tête seul
            said = await asyncio.to_thread(_take, os.path.join(work, "err"), 300)
            log.warning("transcribe : ffmpeg n'a pas converti %s (%s, code "
                        "%s) : %s", url, kind, code,
                        said.decode("utf-8", "replace").strip()[:300])
            raise _refused(url, (
                f"is {kind} audio that could not be decoded, so it cannot "
                f"be transcribed"))
        if len(wav) >= CONVERT_MAX_BYTES:
            raise _refused(url, (
                f"is too long: converted for the transcription model it "
                f"exceeds {CONVERT_MAX_BYTES // 1_000_000} MB (about "
                f"{CONVERT_MAX_BYTES // 1_920_000} minutes), which this "
                f"tool does not transcribe"))
        log.info("transcribe : %s converti en WAV par ffmpeg (%d → %d "
                 "octets, %.1fs)", kind, len(body), len(wav),
                 time.monotonic() - started)
        return wav
    except OSError as exc:      # ffmpeg disparu, disque plein
        log.warning("transcribe : conversion impossible (%s)", exc)
        raise ToolError("unavailable", (
            "audio conversion failed on this proxy. Try a WAV file, or "
            "try again later."))
    finally:
        # Délai, annulation de l'appel, erreur : ffmpeg ne survit pas.
        if proc is not None and proc.returncode is None:
            proc.kill()
            await asyncio.shield(proc.wait())
        await asyncio.to_thread(shutil.rmtree, work, True)


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
    data = {"model": model, "response_format": RESPONSE_FORMAT}
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
        # Le fichier est en cause, pas le backend : un statut qui le dit,
        # ou un corps qui le dit sous un autre statut (gufo rend un 500
        # `invalid_request_error` pour un format qu'il ne lit pas).
        error = doc.get("error") if isinstance(doc, dict) else None
        refused = isinstance(error, dict) \
            and error.get("type") == "invalid_request_error"
        if r.status_code in (400, 413, 415, 422) or refused:
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
    # La langue : telle que le backend la dit — un code ou un nom
    # («english» sous gufo) ; à défaut, celle qui a été demandée.
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
        [tools].run_timeout : le téléchargement, la conversion s'il y en
        a une, puis la transcription."""
        return DOWNLOAD_TIMEOUT + TIMEOUT + (CONVERT_TIMEOUT if FFMPEG else 0)

    def prompt(self, present) -> str:
        return (
            "Transcribe the speech of an audio file, given its URL, and "
            "return the text (" + ", ".join(_offered()) + f"; up to "
            f"{MAX_BYTES // 1_000_000} MB). The text has no timestamps "
            "and no speaker names. Long transcripts are truncated: pass "
            "`offset` to continue from a given character position (the "
            "audio is not transcribed again).")

    def parameters(self, present) -> dict:
        return {
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
        }

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
        if kind and kind not in ACCEPTED:
            # Lisible seulement converti (_readable l'a admis pour cela).
            body, kind = await convert(url, body, kind), "wav"
        text, heard, duration = await _transcribe(
            f"audio.{kind}" if kind else "audio",
            FORMATS.get(kind) or content_type or "application/octet-stream",
            body, language)
        entry = (url, text, heard, duration)
        CACHE.put(key, entry, len(text))
        return read(entry)


TOOL = Transcribe()
