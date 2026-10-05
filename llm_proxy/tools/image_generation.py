"""
`image_generation` : générer une image, par la route
`/v1/images/generations` d'un backend du proxy. Un client de l'API
Responses déclare `{"type": "image_generation"}` en comptant qu'OpenAI
dessinera ; ici c'est le modèle d'image de la CONFIGURATION
([tools.image_generation].model, nom préfixé comme partout :
`bigchuck/Qwen-Image-2.1-heretic`) qui le fait — jamais celui que le
client nomme sur son outil (`gpt-image-1`…).

L'appel part par le client HTTP du backend que désigne le préfixe
(backends.py : même routage, même clé, préfixe retiré), avec le corps
que gufo accepte — celui de l'extension gufo-media.ts de llmsetup :
`model`, `prompt`, `size`, et `steps` s'il est réglé. Réponse attendue :
`{"data": [{"b64_json": "…"}]}`. Une image par appel.

Ce que cet outil a de particulier : son résultat n'est pas qu'un texte.
Le MODÈLE reçoit une phrase courte (il ne voit pas l'image), le CLIENT
l'image en base64 dans l'élément `image_generation_call`. `run` rend
donc un `Image` : une chaîne — le texte du modèle, tout ce que la
boucle, la mémoire des résultats et les journaux manipulent — qui porte
en plus l'image pour `item`. Le base64 ne passe ni par la mémoire ni par
les journaux.

À savoir du backend visé : sur gufo le modèle d'image partage sa mémoire
avec les LLM. Générer DÉCHARGE le modèle de la conversation (bascule
~30 s) et le tour suivant de la boucle le recharge (~40 s) : d'où un
délai propre, long (`timeout`), à la place de [tools].run_timeout.

Ni le limiteur d'un backend à quotas ni les statistiques ne voient cet
appel : il laisse une ligne de journal, c'est tout.
"""

import base64
import json
import time

import httpx

from .. import config
from ..backends import route_backend
from ..settings import log

ENABLED = config.flag("tools.image_generation.enabled", False)
# Le modèle d'image, PRÉFIXÉ par son backend. Vide = l'outil répond au
# modèle qu'il n'est pas configuré.
MODEL = config.text("tools.image_generation.model", "").strip()
# Tailles permises, et celle qui sert quand ni le client ni le modèle
# n'en demandent une permise. Sur gufo (Qwen-Image, 20 étapes) : ~15 s en
# 512x512, ~90 s en 1024x1024.
SIZES = config.strings("tools.image_generation.sizes",
                       ("512x512", "768x768", "1024x1024"))
SIZE = config.text("tools.image_generation.size", "512x512").strip()
# Étapes de diffusion (`steps`, extension de gufo) ; 0 = non envoyé, le
# défaut du backend.
STEPS = config.integer("tools.image_generation.steps", 0)
# Délai (s) de la requête au backend, bascule de modèle comprise.
TIMEOUT = config.num("tools.image_generation.timeout", 300)
# Images générées au plus pour UNE réponse (en plus de [tools].max_calls,
# qui compte tous les outils).
MAX_PER_RESPONSE = config.integer("tools.image_generation.max_per_response", 2)

NAME = "image_generation"
ITEM_TYPE = "image_generation_call"
KINDS = ("image_generation",)
# Lus par tools.Hosted.run : le délai d'une exécution, à la place de
# [tools].run_timeout (une marge au-dessus de la requête, pour que ce
# soit son erreur à elle qui parle) ; le nombre d'appels de CETTE
# fonction par réponse.
RUN_TIMEOUT = TIMEOUT + 5
MAX_CALLS = MAX_PER_RESPONSE
# Pas sur /v1/tools : ces routes rendent un texte, et pi comme omp ont
# leur outil d'image (gufo-media.ts), qui écrit le fichier chez eux.
DIRECT = False

DEFINITION = {"type": "function", "function": {
    "name": NAME,
    "description": (
        "Generate an image from a text prompt. The image is delivered to "
        "the user, not to you: you only get a short confirmation. Write a "
        "complete, self-contained prompt (subject, style, composition). "
        "Generation is slow: call it once per image the user asked for."),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "What the image must show."},
            "size": {"type": "string", "enum": list(SIZES),
                     "description": f"Width x height (default {SIZE}). "
                                    f"Larger is much slower."},
        },
        "required": ["prompt"],
    },
}}


class Image(str):
    """Le résultat d'une génération : le TEXTE rendu au modèle, qui porte
    en plus ce que seul le client reçoit. Une chaîne, pour traverser
    telle quelle tout ce qui manipule des résultats d'outil ; `str(x)`
    en rend le texte nu — c'est ce que la mémoire range."""

    def __new__(cls, text: str, b64: str, size: str, fmt: str):
        self = super().__new__(cls, text)
        self.b64, self.size, self.format = b64, size, fmt
        return self


def _text(size: str, fmt: str) -> str:
    return (f"Image generated ({size}, {fmt}) and shown to the user. You "
            f"cannot see it: do not describe details you did not ask for.")


# Texte d'un appel rejoué dont ni la mémoire ni l'élément ne disent la
# taille (proxy redémarré, client qui ne renvoie que `revised_prompt`).
# Il ne demande PAS de relancer l'outil : l'image, elle, est chez le
# client.
REPLAYED = "Image generated and shown to the user. You cannot see it."
FAILED = "Error: the image generation failed."


def options(tool: dict) -> dict:
    """Ce que le CLIENT a réglé sur son outil et que `run` respecte, dans
    les bornes du proxy : `size`, si elle est permise (`auto`, absente ou
    hors liste = au choix du modèle, puis le défaut). Le reste n'a pas
    d'équivalent chez le backend : `model`, `quality`, `output_format`,
    `output_compression`, `background`, `moderation`, `partial_images`,
    `input_fidelity`, `input_image_mask`, `action` — ignorés."""
    size = tool.get("size")
    return {"size": size} if isinstance(size, str) and size in SIZES else {}


def item(args: dict, result) -> dict:
    """Les champs de l'élément `image_generation_call` terminé. Le prompt
    est rendu dans `revised_prompt` (rien ne le réécrit ici) : c'est par
    lui qu'un élément rejoué redevient un appel si la mémoire l'a perdu."""
    prompt = str(args.get("prompt") or "")
    if isinstance(result, Image):
        return {"status": "completed", "result": result.b64,
                "revised_prompt": prompt, "size": result.size,
                "output_format": result.format}
    return {"status": "failed", "result": None, "revised_prompt": prompt}


def replay(it: dict) -> tuple[str, str]:
    """Élément rejoué par le client et absent de la mémoire → (arguments,
    texte du résultat), reconstruits de ce qu'il porte. JAMAIS son
    `result` : le modèle ne reçoit pas l'image. Avec `size` et
    `output_format` (un client qui renvoie l'élément entier), le texte
    est celui de la boucle ; Codex n'en garde que `revised_prompt`."""
    arguments = json.dumps({"prompt": str(it.get("revised_prompt") or "")},
                           ensure_ascii=False)
    if it.get("status") == "failed":
        return arguments, FAILED
    size, fmt = it.get("size"), it.get("output_format")
    if isinstance(size, str) and size and isinstance(fmt, str) and fmt:
        return arguments, _text(size, fmt)
    return arguments, REPLAYED


def _format(b64: str) -> str:
    """Le format réel de l'image, lu dans ses premiers octets (le backend
    ne le dit pas) ; `png` à défaut — c'est ce que gufo rend."""
    try:
        head = base64.b64decode(b64[:16])
    except ValueError:
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return "png"


async def run(args: dict, size: str | None = None) -> str:
    """`size` ne vient jamais du modèle par ce paramètre : c'est celle que
    le CLIENT a fixée sur son outil (voir `options`), elle l'emporte sur
    l'argument du modèle."""
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: `prompt` is required."
    backend, prefixed = route_backend({"model": MODEL}) if MODEL else (None, False)
    if backend is None or not prefixed:
        return "Error: image generation is not configured on this proxy."
    if size is None:
        size = args.get("size") if args.get("size") in SIZES else SIZE
    body = {"model": MODEL[len(backend.name) + 1:], "prompt": prompt.strip(),
            "size": size}
    if STEPS > 0:
        body["steps"] = STEPS
    started = time.monotonic()
    try:
        r = await backend.client.post(
            "/v1/images/generations", json=body,
            headers=backend.auth_headers(),
            timeout=httpx.Timeout(TIMEOUT, connect=backend.connect_timeout))
    except httpx.TimeoutException:
        return f"Error: the image backend timed out after {int(TIMEOUT)} s."
    except httpx.HTTPError as exc:
        return f"Error: image backend unreachable ({type(exc).__name__})."
    if r.status_code != 200:
        excerpt = " ".join(r.text[:200].split())
        return (f"Error: the image backend returned HTTP {r.status_code}"
                + (f" ({excerpt})." if excerpt else "."))
    try:
        b64 = r.json()["data"][0]["b64_json"]
    except (ValueError, LookupError, TypeError):
        b64 = None
    if not isinstance(b64, str) or not b64:
        return "Error: unreadable image backend response (no `b64_json`)."
    fmt = _format(b64)
    # La seule trace de la génération : la ligne de stats de la réponse
    # compte des tokens, une image n'en est pas.
    log.info("image_generation : %s %s (%s) par %s en %.1fs, %d Ko",
             size, fmt, f"{STEPS} étapes" if STEPS > 0 else "étapes du backend",
             MODEL, time.monotonic() - started, len(b64) * 3 // 4 // 1024)
    return Image(_text(size, fmt), b64, size, fmt)


if ENABLED and (not MODEL or route_backend({"model": MODEL}) == (None, False)
                or SIZE not in SIZES):
    log.warning("[tools.image_generation] actif mais mal réglé : `model` "
                "doit porter le préfixe d'un backend (reçu %r) et `size` "
                "(%r) figurer dans `sizes`", MODEL, SIZE)
