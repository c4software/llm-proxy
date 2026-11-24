"""
Le catalogue des modèles : ce que chaque backend annonce (/v1/models,
préfixé par son nom), le TYPE de chaque modèle (conversation, image,
voix, OCR, embeddings…), ses MODALITÉS (`architecture`, à la forme
d'OpenRouter et de llama.cpp), sa fenêtre de contexte, et s'il lit les
images.
Sert GET /v1/models, le sélecteur de la page et les outils qui visent un
modèle spécialisé.
"""

import asyncio
import fnmatch
import time

from fastapi import Request

from ..core.settings import CATALOG_TTL, log
from . import anthropic
from .backends import Backend, permitted, visible
from .relaying import clean_headers




def _model_max_context(m: dict) -> int | None:
    """max_context_length d'une entrée /v1/models. Albert le fournit ;
    llama.cpp non — on le dérive du «--ctx-size» de status.args (présent
    même modèle déchargé), sinon de meta.n_ctx_train."""
    if isinstance(m.get("max_context_length"), int):
        return m["max_context_length"]
    status = m.get("status")
    if isinstance(status, dict) and isinstance(status.get("args"), list):
        args = status["args"]
        try:
            return int(args[args.index("--ctx-size") + 1])
        except (ValueError, IndexError, TypeError):
            pass
    meta = m.get("meta")
    if isinstance(meta, dict):
        for key in ("n_ctx_train", "n_ctx"):
            if isinstance(meta.get(key), int):
                return meta[key]
    return None


# Types d'un modèle de CONVERSATION : ceux qu'on peut proposer en exemple
# à un client de chat (tableau de bord, /healthz `chat_models`).
CHAT_TYPES = ("text-generation", "image-text-to-text")
IMAGE_TYPE = "text-to-image"

# Les `type` d'autres catalogues (OpenCode Zen : chat, embedding, image),
# ramenés à ceux d'Albert ; « chat » dépend des modalités d'entrée.
_FOREIGN_TYPES = {"embedding": "text-embeddings-inference",
                  "embeddings": "text-embeddings-inference",
                  "image": IMAGE_TYPE}

# Un catalogue MULTI-PROTOCOLES dit par modèle, dans `api` (un objet), les
# API qui le servent (OpenCode Zen : chat-completions, responses, messages,
# generate-content…). Seuls restent ceux que le protocole du backend sait
# parler : un autre échouerait.
_SERVED_APIS = {"openai": {"chat-completions", "embeddings", "images"},
                "anthropic": {"messages"}}

# Un catalogue sans `type` ni `architecture` (llama-swap) : le nom est le
# seul indice, pour ne pas proposer un modèle d'image ou de voix à un
# client de chat. Un mot entier du nom, jamais une sous-chaîne ; pour un
# nom qui trompe, [backends.<nom>.model_types] tranche.
_NAME_TYPES = (
    (("image", "flux", "sdxl", "diffusion", "imagen"), "text-to-image"),
    (("tts", "speech", "voice"), "text-to-speech"),
    (("asr", "whisper", "stt", "transcribe"), "automatic-speech-recognition"),
    (("embed", "embedding", "embeddings", "bge", "e5"), "text-embeddings-inference"),
    (("rerank", "reranker"), "text-classification"),
)


def converses(b: Backend, model: str) -> bool:
    """Peut-on présenter D'OFFICE des outils à ce modèle (nom préfixé ou
    non) ? Non si le catalogue de son backend le dit d'un autre type que
    de conversation (image, voix, embeddings) : un `tools` qu'il n'attend
    pas y serait refusé. Catalogue pas encore lu, modèle absent : oui — le
    backend n'est pas sondé pour ça. Le catalogue ne dit PAS si un modèle
    de conversation sait appeler des outils."""
    plain = model.lower().removeprefix(b.name + "/")
    return b.model_types.get(plain, "text-generation") in CHAT_TYPES


# Les modalités d'entrée et de sortie de chaque type, pour un backend qui
# ne les donne pas (Albert n'a que `type`, llama-swap rien) : la forme
# `architecture` d'OpenRouter et de llama.cpp, que les clients lisent
# (pi, omp, OpenCode : ce qui va dans `input`).
_TYPE_MODALITIES = {
    "text-generation": (["text"], ["text"]),
    "image-text-to-text": (["text", "image"], ["text"]),
    "text-to-image": (["text"], ["image"]),
    "text-to-speech": (["text"], ["audio"]),
    "image-to-text": (["image"], ["text"]),
    "automatic-speech-recognition": (["audio"], ["text"]),
    "text-embeddings-inference": (["text"], ["embeddings"]),
    "text-classification": (["text"], ["text"]),
}


def _overridden(m: dict, b: Backend | None) -> bool:
    """Un motif de [backends.<nom>.model_types] vise-t-il ce modèle ?"""
    name = str(m.get("id") or "").lower()
    return any(fnmatch.fnmatchcase(name, p) for p in (b.type_overrides if b is not None else ()))


def _architecture(m: dict, kind: str, b: Backend | None = None) -> dict:
    """`architecture` d'une entrée /v1/models : celle du backend telle
    quelle s'il en donne une (llama.cpp, OpenRouter), sinon déduite du
    type (Albert, llama-swap) ; déduite aussi quand
    [backends.<nom>.model_types] a tranché le type."""
    arch = m.get("architecture")
    if isinstance(arch, dict) and not _overridden(m, b):
        inp, out = arch.get("input_modalities"), arch.get("output_modalities")
        # llama.cpp ne donne que l'entrée : la sortie, celle du type.
        if not (isinstance(out, list) and out):
            out = _TYPE_MODALITIES.get(kind, (["text"], ["text"]))[1]
        if isinstance(inp, list) and inp:
            inp, out = [str(x) for x in inp], [str(x) for x in out]
            return {"modality": "+".join(inp) + "->" + "+".join(out),
                    "input_modalities": inp, "output_modalities": out}
    inp, out = _TYPE_MODALITIES.get(kind, (["text"], ["text"]))
    return {"modality": "+".join(inp) + "->" + "+".join(out),
            "input_modalities": list(inp), "output_modalities": list(out)}


def _capability_type(caps: dict) -> str:
    """Type d'une carte de modèle Mistral, d'après ses `capabilities`
    (ModelCapabilities : des booléens, faux par défaut). Son `type` (`base`
    ou `fine-tuned`) dit l'origine du modèle, pas sa tâche. Aucune capacité :
    un modèle d'embeddings, le seul que Mistral liste sans drapeau. Une
    capacité que le proxy ne sert pas (transcription temps réel, par
    WebSocket seulement) : « other », proposé nulle part."""
    if caps.get("completion_chat") is True:
        return "image-text-to-text" if caps.get("vision") is True else "text-generation"
    if caps.get("audio_transcription") is True:
        return "automatic-speech-recognition"
    if caps.get("audio_speech") is True:
        return "text-to-speech"
    if caps.get("ocr") is True:
        return "image-to-text"
    if caps.get("classification") is True or caps.get("moderation") is True:
        return "text-classification"
    if not any(v is True for v in caps.values()):
        return "text-embeddings-inference"
    return "other"


def _model_type(m: dict, b: Backend | None = None) -> str:
    """`type` d'une entrée /v1/models, par ordre de confiance : le motif
    de [backends.<nom>.model_types] ; les `capabilities` (Mistral) ; le
    `type` du backend (Albert le fournit) ;
    `architecture.{input,output}_modalities` (llama.cpp) ; à défaut le nom
    (voir _NAME_TYPES) ; sinon «text-generation»."""
    name = str(m.get("id") or "").lower()
    for pattern, kind in (b.type_overrides.items() if b is not None else ()):
        if fnmatch.fnmatchcase(name, pattern):
            return kind
    # Une carte de Mistral (BaseModelCard, FTModelCard) : `type` est son
    # discriminant ; un autre catalogue peut avoir ses `capabilities` à lui.
    if m.get("type") in ("base", "fine-tuned") and isinstance(m.get("capabilities"), dict):
        return _capability_type(m["capabilities"])
    kind = m.get("type")
    if kind == "chat":
        mods, arch = m.get("modalities"), m.get("architecture")
        sees = [*((mods.get("input") if isinstance(mods, dict) else None) or []),
                *((arch.get("input_modalities") if isinstance(arch, dict) else None) or [])]
        return "image-text-to-text" if any(
            str(x).startswith("image") for x in sees) else "text-generation"
    if isinstance(kind, str):
        return _FOREIGN_TYPES.get(kind, kind)
    arch = m.get("architecture")
    if isinstance(arch, dict):
        inp = arch.get("input_modalities") or []
        out = arch.get("output_modalities") or []
        if "audio" in inp:
            return "automatic-speech-recognition"
        if "image" in inp and "text" in out:
            return "image-text-to-text"
        return "text-generation"
    words = set(name.replace("/", "-").replace("_", "-").replace(".", "-").split("-"))
    for keys, kind in _NAME_TYPES:
        if words.intersection(keys):
            return kind
    return "text-generation"


# Les catalogues lus : (backend, en-têtes) → (instant, entrées ou None).
# Les en-têtes en font partie : un backend sans clé reçoit celle du
# client, et peut ne pas montrer les mêmes modèles à tous. Une lecture
# en cours est partagée : les appels simultanés (la page en fait
# plusieurs à son ouverture) attendent la même.
_cache: dict[tuple, tuple[float, list | None]] = {}
_reading: dict[tuple, asyncio.Task] = {}


def forget() -> None:
    """Oublie les catalogues lus (tests, rechargement)."""
    _cache.clear()
    _reading.clear()


async def fetch_models(b: Backend, request: Request | None = None) -> list | None:
    """Le catalogue d'UN backend, normalisé et préfixé ; None s'il ne
    répond pas. Gardé CATALOG_TTL secondes, sa réponse comme son silence,
    puis relu en arrière-plan au premier appel qui le trouve périmé (cet
    appel-là a encore l'ancien). Met à jour b.models et b.model_types à
    chaque lecture."""
    # Sans clé, un backend du TOML lit son catalogue avec l'Authorization du
    # client (clean_headers, qui ajoute aussi ses en-têtes propres).
    headers = b.auth_headers() if b.api_key or b.api == "anthropic" or request is None \
        else clean_headers(request, b)
    # Deux comptes peuvent avoir chacun un backend du même nom.
    key = (b.name, b.owner, b.url,
           tuple(sorted((k.lower(), v) for k, v in headers.items())))
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < CATALOG_TTL:
        return hit[1]
    task = _reading.get(key)
    if task is None:
        task = _reading[key] = asyncio.ensure_future(_read_models(b, headers))

        def keep(t: asyncio.Task) -> None:
            _reading.pop(key, None)
            if t.cancelled() or t.exception() is not None:
                return
            now = time.monotonic()
            if len(_cache) > 256:        # une clé de client par entrée : borné
                for k in [k for k, (at, _) in _cache.items() if now - at >= 10 * CATALOG_TTL]:
                    del _cache[k]
            _cache[key] = (now, t.result())
        task.add_done_callback(keep)
    # Périmé : l'ancien catalogue est rendu tout de suite, le nouveau se
    # lit en arrière-plan — seule la toute première lecture attend.
    if hit is not None:
        return hit[1]
    return await asyncio.shield(task)


async def _read_models(b: Backend, headers: dict) -> list | None:
    try:
        if b.api == "anthropic":
            data = await anthropic.read_models(b, headers)
        else:
            r = await b.client.get(
                "/v1/models", headers=headers, timeout=b.meta_timeout,
            )
            if r.status_code != 200:
                log.warning("/v1/models %s → %d", b.name, r.status_code)
                return None
            data = r.json().get("data") or []
    except Exception as exc:
        log.warning("/v1/models %s injoignable : %s", b.name, exc)
        return None
    # Entrées NORMALISÉES sur le schéma d'Albert, id/aliases préfixés.
    # Le reste (status, args, chemins de .gguf, meta… — détail interne
    # llama.cpp) est écarté : à ne pas publier.
    entries = []
    types: dict[str, str] = {}
    served = [m for m in data if isinstance(m, dict) and m.get("id") and (
        not isinstance(m.get("api"), dict) or _SERVED_APIS[b.api].intersection(m["api"]))]
    for m in served:
        costs = m.get("costs")
        kind = _model_type(m, b)
        types[str(m["id"]).lower()] = kind
        for a in m.get("aliases") or []:
            if isinstance(a, str):
                types[a.lower()] = kind
        entries.append({
            "object": "model",
            "id": f"{b.name}/{m['id']}",
            "created": m.get("created") or 0,
            "owned_by": m.get("owned_by") or b.name,
            "type": kind,
            "architecture": _architecture(m, kind, b),
            "costs": costs if isinstance(costs, dict)
            else {"prompt_tokens": 0.0, "completion_tokens": 0.0},
            "max_context_length": _model_max_context(m),
            "aliases": [
                f"{b.name}/{a}" for a in (m.get("aliases") or [])
                if isinstance(a, str)
            ],
        })
    b.models = {str(m["id"]).lower() for m in served}
    b.aliases = {a.lower(): str(m["id"]).lower() for m in served
                 for a in m.get("aliases") or [] if isinstance(a, str)}
    b.model_types = types
    return entries


async def accepts_images(b: Backend, plain_model: str) -> bool:
    """Le modèle verra-t-il une image ? Jamais sans `images = true` sur
    le backend ; avec, c'est le TYPE du modèle au catalogue qui décide
    (chargé à la demande s'il ne l'a pas encore été). Modèle absent du
    catalogue : on fait confiance au flag."""
    if not b.images:
        return False
    if not b.model_types:
        await fetch_models(b)
    kind = b.model_types.get(plain_model.lower())
    return kind is None or kind == "image-text-to-text"


async def merged_models(request: Request) -> list[dict] | None:
    """Catalogue unifié : chaque backend que voit le client est interrogé,
    ses modèles exposés préfixés par son nom («albert/…», «bigchuck/…») —
    les noms renvoyés sont directement routables ; ceux que son compte ne
    peut pas employer n'y sont pas. None = aucun backend joignable ; aucun
    backend du tout : une liste vide."""
    seen = list(visible().values())
    results = await asyncio.gather(*(fetch_models(b, request) for b in seen))
    if results and all(lst is None for lst in results):
        return None
    return [e for b, lst in zip(seen, results) if lst for e in lst
            if permitted(b, e["id"])]
