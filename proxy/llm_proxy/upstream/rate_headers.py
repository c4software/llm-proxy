"""
Les LIMITES ANNONCÉES dans les en-têtes d'une réponse de l'amont, pour le
limiteur (quotas.QuotaState.observe) : le plafond, le restant et, quand
l'amont le dit, le délai avant que la fenêtre se libère, par type (rpm,
tpm, rpd, tpd).

  Mistral    x-ratelimit-{limit,remaining}-{req,tokens}-{minute,day}
  OpenAI     x-ratelimit-{limit,remaining,reset}-{requests,tokens} : la
             minute ;
  Groq       les mêmes noms, mais `requests` compte la journée : un
             x-ratelimit-reset-requests de plus d'une minute (« 2m59.56s »)
             le dit, et le backend reste dès lors compté à la journée ;
  Anthropic  anthropic-ratelimit-{requests,tokens}-{limit,remaining} : la
             minute.

Un plafond sans son restant (ou l'inverse), nul, illisible ou démesuré
(au-delà de MAX), est ignoré.
"""

import re

MAX = 10 ** 12
_COUNT = re.compile(r"[0-9]{1,13}\Z")
_MISTRAL = re.compile(r"x-ratelimit-(limit|remaining)-(req|tokens)-(minute|day)\Z")
_MISTRAL_KINDS = {("req", "minute"): "rpm", ("tokens", "minute"): "tpm",
                  ("req", "day"): "rpd", ("tokens", "day"): "tpd"}
# Une durée d'OpenAI ou de Groq : « 1s », « 6m0s », « 2m59.56s », « 20ms ».
_DURATION = re.compile(r"(?:([0-9]{1,6})h)?(?:([0-9]{1,6})m(?!s))?"
                       r"(?:([0-9]{1,6}(?:\.[0-9]{1,6})?)s)?(?:([0-9]{1,9})ms)?\Z")


def _count(value) -> int | None:
    text = str(value).strip()
    if not _COUNT.match(text):
        return None
    n = int(text)
    return n if n <= MAX else None


def _seconds(value) -> float | None:
    m = _DURATION.match(str(value or "").strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def parse(headers, daily_requests: bool = False) -> tuple[dict, bool]:
    """({type: (plafond, restant, délai en s ou None)}, daily_requests)
    annoncés par `headers` (toute table nom → valeur). `daily_requests` :
    ce backend a déjà montré que ses `requests` au format OpenAI comptent
    la journée (Groq) ; rendu mis à jour."""
    h = {str(k).lower(): v for k, v in headers.items()}
    found: dict[str, list] = {}         # type → [plafond, restant, délai]
    for name, value in h.items():
        m = _MISTRAL.match(name)
        if m:
            found.setdefault(_MISTRAL_KINDS[m[2], m[3]], [None, None, None])[
                m[1] == "remaining"] = _count(value)
    if not found and ("x-ratelimit-limit-requests" in h or "x-ratelimit-limit-tokens" in h):
        reset = _seconds(h.get("x-ratelimit-reset-requests"))
        daily_requests = daily_requests or (reset is not None and reset > 60)
        for kind, word in (("rpd" if daily_requests else "rpm", "requests"), ("tpm", "tokens")):
            found[kind] = [_count(h.get(f"x-ratelimit-limit-{word}")),
                           _count(h.get(f"x-ratelimit-remaining-{word}")),
                           _seconds(h.get(f"x-ratelimit-reset-{word}"))]
    elif not found:
        for kind, word in (("rpm", "requests"), ("tpm", "tokens")):
            found[kind] = [_count(h.get(f"anthropic-ratelimit-{word}-limit")),
                           _count(h.get(f"anthropic-ratelimit-{word}-remaining")), None]
    return ({k: (cap, left, reset) for k, (cap, left, reset) in found.items()
             if cap and left is not None}, daily_requests)
