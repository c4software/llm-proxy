"""
Les outils du NAVIGATEUR — l'appareil : date, position, notifications,
presse-papiers, agenda, liens, partage, écran, caméra, codes, couleur,
écran allumé, voix.
Leur DÉCLARATION (Runs.BROWSER) : le prompt, le schéma, la famille du
menu Outils. Leur exécution est dans la page, chat/tools/device/
(docs/navigateur.md, « Écrire un outil de la page »).
"""

from ... import prompts
from ..contract import BrowserTool, Family, FamilyKey, ToolName
from .common import REASON

MAX_DELAY = 24 * 60          # minutes d'un rappel (notify), au plus
MAX_TEXT = 4000              # caractères lus à voix haute (speak), au plus


# ── Date et heure ─────────────────────────────────────────────────────

DEVICE_CONTEXT = BrowserTool(
    ToolName.DEVICE_CONTEXT,
)

CONTEXT_FAMILY = Family(
    FamilyKey.CONTEXT,
    label='Date et heure',
    tools=(ToolName.DEVICE_CONTEXT,),
    hint='date, heure, fuseau, langues, appareil',
    on=True,
)


# ── Position ──────────────────────────────────────────────────────────

GET_LOCATION = BrowserTool(
    ToolName.GET_LOCATION,
    params={
        'reason': {
            'type': 'string',
            'description': prompts.ref("get_location.param.reason"),
        },
    },
    required=('reason',),
)

LOCATION_FAMILY = Family(
    FamilyKey.LOCATION,
    label='Position',
    tools=(ToolName.GET_LOCATION,),
    hint='demandée sur une carte, approximative ou précise',
)


# ── Notifications ─────────────────────────────────────────────────────

NOTIFY = BrowserTool(
    ToolName.NOTIFY,
    params={
        'title': {'type': 'string', 'description': prompts.ref("notify.param.title")},
        'body': {'type': 'string', 'description': prompts.ref("notify.param.body")},
        'delay_minutes': {
            'type': 'number',
            'description': prompts.ref("notify.param.delay_minutes"),
        },
    },
    required=('title',),
    settings={'max_delay': MAX_DELAY},
)

NOTIFY_FAMILY = Family(
    FamilyKey.NOTIFY,
    label='Notifications',
    tools=(ToolName.NOTIFY,),
    hint='tout de suite, ou un rappel dans N minutes',
    on=True,
)


# ── Presse-papiers ────────────────────────────────────────────────────

CLIPBOARD_WRITE = BrowserTool(
    ToolName.CLIPBOARD_WRITE,
    params={'text': {'type': 'string', 'description': prompts.ref("clipboard_write.param.text")}},
    required=('text',),
)

CLIPBOARD_READ = BrowserTool(
    ToolName.CLIPBOARD_READ,
    params={
        'reason': REASON,
    },
)

CLIPBOARD_FAMILY = Family(
    FamilyKey.CLIPBOARD,
    label='Presse-papiers',
    tools=(ToolName.CLIPBOARD_WRITE, ToolName.CLIPBOARD_READ),
    hint='copier ; lire sur une carte',
    on=True,
)


# ── Agenda ────────────────────────────────────────────────────────────

ADD_TO_CALENDAR = BrowserTool(
    ToolName.ADD_TO_CALENDAR,
    params={
        'title': {'type': 'string', 'description': prompts.ref("add_to_calendar.param.title")},
        'start': {
            'type': 'string',
            'description': prompts.ref("add_to_calendar.param.start"),
        },
        'end': {
            'type': 'string',
            'description': prompts.ref("add_to_calendar.param.end"),
        },
        'location': {'type': 'string', 'description': prompts.ref("add_to_calendar.param.location")},
        'description': {'type': 'string', 'description': prompts.ref("add_to_calendar.param.description")},
        'alarm_minutes': {'type': 'number', 'description': prompts.ref("add_to_calendar.param.alarm_minutes")},
    },
    required=('title', 'start'),
)

CALENDAR_FAMILY = Family(
    FamilyKey.CALENDAR,
    label='Agenda',
    tools=(ToolName.ADD_TO_CALENDAR,),
    hint='un événement à ajouter (.ics)',
    on=True,
)


# ── Liens ─────────────────────────────────────────────────────────────

OPEN_LINK = BrowserTool(
    ToolName.OPEN_LINK,
    params={
        'url': {'type': 'string', 'description': prompts.ref("open_link.param.url")},
        'reason': {'type': 'string', 'description': prompts.ref("open_link.param.reason")},
    },
    required=('url',),
)

LINKS_FAMILY = Family(
    FamilyKey.LINKS,
    label='Liens',
    tools=(ToolName.OPEN_LINK,),
    hint='une adresse à ouvrir, en carte sous la réponse',
    on=True,
)


# ── Partage ───────────────────────────────────────────────────────────

SHARE = BrowserTool(
    ToolName.SHARE,
    params={
        'title': {'type': 'string', 'description': prompts.ref("share.param.title")},
        'text': {'type': 'string', 'description': prompts.ref("share.param.text")},
        'url': {'type': 'string', 'description': prompts.ref("share.param.url")},
    },
    required=('text',),
)

SHARE_FAMILY = Family(
    FamilyKey.SHARE,
    label='Partage',
    tools=(ToolName.SHARE,),
    hint='une carte sous la réponse, à partager quand on veut',
)


# ── Capture d'écran ───────────────────────────────────────────────────

CAPTURE_SCREEN = BrowserTool(
    ToolName.CAPTURE_SCREEN,
    params={
        'reason': REASON,
    },
    required=('reason',),
)

SCREEN_FAMILY = Family(
    FamilyKey.SCREEN,
    label="Capture d'écran",
    tools=(ToolName.CAPTURE_SCREEN,),
    hint="un onglet, une fenêtre ou l'écran, avec aperçu",
)


# ── Caméra ────────────────────────────────────────────────────────────

TAKE_PHOTO = BrowserTool(
    ToolName.TAKE_PHOTO,
    params={
        'reason': REASON,
    },
    required=('reason',),
)

CAMERA_FAMILY = Family(
    FamilyKey.CAMERA,
    label='Caméra',
    tools=(ToolName.TAKE_PHOTO,),
    hint='une photo, avec aperçu',
)


# ── QR et codes-barres ────────────────────────────────────────────────

SCAN_CODE = BrowserTool(
    ToolName.SCAN_CODE,
    params={
        'reason': REASON,
    },
)

CODES_FAMILY = Family(
    FamilyKey.CODES,
    label='QR et codes-barres',
    tools=(ToolName.SCAN_CODE,),
    hint="lus à la caméra ou à l'écran",
)


# ── Pipette ───────────────────────────────────────────────────────────

PICK_COLOR = BrowserTool(
    ToolName.PICK_COLOR,
    params={'reason': {'type': 'string', 'description': prompts.ref("pick_color.param.reason")}},
)

COLOR_FAMILY = Family(
    FamilyKey.COLOR,
    label='Pipette',
    tools=(ToolName.PICK_COLOR,),
    hint="une couleur prise à l'écran",
)


# ── Écran allumé ──────────────────────────────────────────────────────

KEEP_SCREEN_ON = BrowserTool(
    ToolName.KEEP_SCREEN_ON,
    params={'on': {'type': 'boolean', 'description': prompts.ref("keep_screen_on.param.on")}},
    required=('on',),
)

WAKE_FAMILY = Family(
    FamilyKey.WAKE,
    label='Écran allumé',
    tools=(ToolName.KEEP_SCREEN_ON,),
    hint='empêcher la mise en veille',
)


# ── Lecture à voix haute ──────────────────────────────────────────────

SPEAK = BrowserTool(
    ToolName.SPEAK,
    params={
        'text': {'type': 'string', 'description': prompts.ref("speak.param.text")},
        'lang': {
            'type': 'string',
            'description': prompts.ref("speak.param.lang"),
        },
    },
    required=('text',),
    settings={'max_text': MAX_TEXT},
)

SPEAK_FAMILY = Family(
    FamilyKey.SPEAK,
    label='Lecture à voix haute',
    tools=(ToolName.SPEAK,),
    hint='le navigateur lit un texte (synthèse vocale)',
)


TOOLS = (DEVICE_CONTEXT, GET_LOCATION, NOTIFY, CLIPBOARD_WRITE, CLIPBOARD_READ, ADD_TO_CALENDAR, OPEN_LINK, SHARE, CAPTURE_SCREEN, TAKE_PHOTO, SCAN_CODE, PICK_COLOR, KEEP_SCREEN_ON, SPEAK,)
FAMILIES = (CONTEXT_FAMILY, LOCATION_FAMILY, NOTIFY_FAMILY, CLIPBOARD_FAMILY, CALENDAR_FAMILY, LINKS_FAMILY, SHARE_FAMILY, SCREEN_FAMILY, CAMERA_FAMILY, CODES_FAMILY, COLOR_FAMILY, WAKE_FAMILY, SPEAK_FAMILY,)
