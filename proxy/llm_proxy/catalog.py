"""
Le catalogue des modèles : ce que chaque backend annonce (/v1/models,
préfixé par son nom), le TYPE de chaque modèle (conversation, image,
voix, OCR, embeddings…), sa fenêtre de contexte, et s'il lit les images.
Sert GET /v1/models, le sélecteur de la page et les outils qui visent un
modèle spécialisé.
"""

import asyncio
import fnmatch

from fastapi import APIRouter, Request

from . import anthropic_api
from . import chat_api
from . import tools
from .backends import BACKENDS, Backend
from .settings import log
from .httputil import anthropic_disabled, dialect_of, error_response
from .relaying import clean_headers


router = APIRouter()


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

# Un catalogue sans `type` ni `architecture` (llama-swap devant gufo ou
# llama.cpp) : le nom est le seul indice. Sans cela un modèle d'image, de
# voix ou de transcription était publié «text-generation» et proposé à un
# client de chat. Un mot entier du nom, jamais une sous-chaîne ; pour un nom
# qui trompe, [backends.<nom>.model_types] tranche.
_NAME_TYPES = (
    (("image", "flux", "sdxl", "diffusion", "imagen"), "text-to-image"),
    (("tts", "speech", "voice"), "text-to-speech"),
    (("asr", "whisper", "stt", "transcribe"), "automatic-speech-recognition"),
    (("embed", "embedding", "embeddings", "bge", "e5"), "text-embeddings-inference"),
    (("rerank", "reranker"), "text-classification"),
)


def always_shown() -> list[str]:
    """Les outils de [chat].always présentables à l'instant : ceux du
    registre, actifs. Relu à chaque appel — le registre peut grandir
    après le démarrage."""
    return chat_api.offered(tools.enabled())


def _spares(b: Backend, model: str) -> bool:
    """Ce modèle est-il de ceux à qui rien n'est présenté d'office
    ([chat].always_except) ? Jugé sur son nom PRÉFIXÉ, que le client l'ait
    écrit ainsi ou non (backend de repli)."""
    plain = model.lower().removeprefix(b.name + "/")
    return chat_api.excepted(f"{b.name}/{plain}")


def _converses(b: Backend, model: str) -> bool:
    """Peut-on présenter D'OFFICE des outils à ce modèle (nom préfixé ou
    non) ? Non si le catalogue de son backend le dit d'un autre type que
    de conversation (image, voix, embeddings) : un `tools` qu'il n'attend
    pas y serait refusé. Catalogue pas encore lu, modèle absent : oui — le
    backend n'est pas sondé pour ça. Le catalogue ne dit PAS si un modèle
    de conversation sait appeler des outils."""
    plain = model.lower().removeprefix(b.name + "/")
    return b.model_types.get(plain, "text-generation") in CHAT_TYPES


def _model_type(m: dict, b: Backend | None = None) -> str:
    """`type` d'une entrée /v1/models, par ordre de confiance : le motif
    de [backends.<nom>.model_types] ; le `type` du backend (Albert le
    fournit) ; `architecture.{input,output}_modalities` (llama.cpp) ; à
    défaut le nom (voir _NAME_TYPES) ; sinon «text-generation»."""
    name = str(m.get("id") or "").lower()
    for pattern, kind in (b.type_overrides.items() if b is not None else ()):
        if fnmatch.fnmatchcase(name, pattern):
            return kind
    if isinstance(m.get("type"), str):
        return m["type"]
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


async def fetch_models(b: Backend, request: Request | None = None) -> list | None:
    """Le catalogue d'UN backend, normalisé et préfixé ; None s'il ne
    répond pas. Met à jour b.models et b.model_types au passage."""
    headers = b.auth_headers() or (
        clean_headers(request, "") if request is not None else {})
    try:
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
    # Entrées NORMALISÉES sur un schéma UNIFORME (celui d'Albert),
    # id/aliases préfixés, champs manquants dérivés ou par défaut.
    # Le reste (status, args, preset, chemins de .gguf, meta… —
    # détail interne llama.cpp) est écarté : illisible pour les
    # clients, et à ne pas publier.
    entries = []
    types: dict[str, str] = {}
    for m in data:
        if isinstance(m, dict) and m.get("id"):
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
                "costs": costs if isinstance(costs, dict)
                else {"prompt_tokens": 0.0, "completion_tokens": 0.0},
                "max_context_length": _model_max_context(m),
                "aliases": [
                    f"{b.name}/{a}" for a in (m.get("aliases") or [])
                    if isinstance(a, str)
                ],
            })
    b.models = {
        str(m.get("id")).lower() for m in data
        if isinstance(m, dict) and m.get("id")
    }
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
    """Catalogue unifié : chaque backend en ligne est interrogé, ses
    modèles exposés préfixés par son nom («albert/…», «bigchuck/…») —
    les noms renvoyés sont directement routables. None = aucun backend
    joignable."""
    results = await asyncio.gather(
        *(fetch_models(b, request) for b in BACKENDS.values()))
    if all(lst is None for lst in results):
        return None
    return [e for lst in results if lst for e in lst]


@router.get("/v1/models")
async def list_models(request: Request):
    """Même catalogue, deux formes : OpenAI par défaut, Anthropic quand
    la requête porte `anthropic-version` (et que la surface est active)."""
    dialect = dialect_of(request)
    if dialect == "anthropic" and not anthropic_api.ENABLED:
        return anthropic_disabled(dialect)
    entries = await merged_models(request)
    if entries is None:
        return error_response(dialect, 502, "proxy_error",
                              "aucun backend joignable pour /v1/models")
    if dialect == "anthropic":
        return anthropic_api.models_list(entries)
    return {"object": "list", "data": entries}
