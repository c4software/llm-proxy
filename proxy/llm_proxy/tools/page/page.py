"""
Les outils du NAVIGATEUR — questions, fichiers, artifacts, dossier ouvert.
Leur DÉCLARATION (Runs.BROWSER) : le prompt, le schéma, la famille du
menu Outils. Leur exécution est dans la page, chat/tools/page/
(docs/navigateur.md, « Écrire un outil de la page »).
"""

from ... import prompts
from ..contract import BrowserTool, Family, FamilyKey, ToolName
from .common import GREP, SLICE

# Les types d'un artifact (show_artifact) : le schéma les énumère, la page
# les vérifie.
TYPES = ('html', 'markdown', 'svg', 'mermaid', 'code')


# ── Questions ─────────────────────────────────────────────────────────

ASK_USER = BrowserTool(
    ToolName.ASK_USER,
    params={
        'questions': {
            'type': 'array',
            'minItems': 1,
            'maxItems': 4,
            'items': {
                'type': 'object',
                'required': ['question', 'header', 'options'],
                'properties': {
                    'question': {'type': 'string', 'description': prompts.ref("ask_user.param.questions.question")},
                    'header': {
                        'type': 'string',
                        'description': prompts.ref("ask_user.param.questions.header"),
                    },
                    'options': {
                        'type': 'array',
                        'minItems': 2,
                        'maxItems': 6,
                        'items': {
                            'type': 'object',
                            'required': ['label'],
                            'properties': {
                                'label': {
                                    'type': 'string',
                                    'description': prompts.ref("ask_user.param.questions.options.label"),
                                },
                                'description': {
                                    'type': 'string',
                                    'description': prompts.ref("ask_user.param.questions.options.description"),
                                },
                            },
                        },
                    },
                    'multiple': {
                        'type': 'boolean',
                        'description': prompts.ref("ask_user.param.questions.multiple"),
                    },
                },
            },
        },
    },
    required=('questions',),
)

ASK_FAMILY = Family(
    FamilyKey.ASK,
    label='Questions',
    tools=(ToolName.ASK_USER,),
    hint='des questions à choix, dans la feuille du bas',
    fixed=True,
)


# ── Fichiers ──────────────────────────────────────────────────────────

SAVE_FILE = BrowserTool(
    ToolName.SAVE_FILE,
    params={
        'name': {'type': 'string', 'description': prompts.ref("save_file.param.name")},
        'content': {'type': 'string', 'description': prompts.ref("save_file.param.content")},
        'mime': {'type': 'string', 'description': prompts.ref("save_file.param.mime")},
    },
    required=('name', 'content'),
)

SAVE_FAMILY = Family(
    FamilyKey.SAVE,
    label='Fichiers',
    tools=(ToolName.SAVE_FILE,),
    hint='un fichier à télécharger sous la réponse',
    fixed=True,
)


# ── Artifacts ─────────────────────────────────────────────────────────

SHOW_ARTIFACT = BrowserTool(
    ToolName.SHOW_ARTIFACT,
    params={
        'id': {
            'type': 'string',
            'description': prompts.ref("show_artifact.param.id"),
        },
        'title': {'type': 'string', 'description': prompts.ref("show_artifact.param.title")},
        'type': {'type': 'string', 'enum': list(TYPES)},
        'content': {
            'type': 'string',
            'description': prompts.ref("show_artifact.param.content"),
        },
        'language': {'type': 'string', 'description': prompts.ref("show_artifact.param.language")},
    },
    required=('title', 'type', 'content'),
    settings={'types': list(TYPES)},
)

EDIT_ARTIFACT = BrowserTool(
    ToolName.EDIT_ARTIFACT,
    params={
        'id': {'type': 'string', 'description': prompts.ref("edit_artifact.param.id")},
        'edits': {
            'type': 'array',
            'minItems': 1,
            'items': {
                'type': 'object',
                'required': ['old_string', 'new_string'],
                'properties': {
                    'old_string': {
                        'type': 'string',
                        'description': prompts.ref("edit_artifact.param.edits.old_string"),
                    },
                    'new_string': {
                        'type': 'string',
                        'description': prompts.ref("edit_artifact.param.edits.new_string"),
                    },
                    'replace_all': {
                        'type': 'boolean',
                        'description': prompts.ref("edit_artifact.param.edits.replace_all"),
                    },
                    'regex': {
                        'type': 'boolean',
                        'description': prompts.ref("edit_artifact.param.edits.regex"),
                    },
                    'ignore_case': {
                        'type': 'boolean',
                        'description': prompts.ref("edit_artifact.param.edits.ignore_case"),
                    },
                },
            },
        },
        'title': {'type': 'string', 'description': prompts.ref("edit_artifact.param.title")},
    },
    required=('id', 'edits'),
)

READ_ARTIFACT = BrowserTool(
    ToolName.READ_ARTIFACT,
    params={
        'id': {'type': 'string', 'description': prompts.ref("read_artifact.param.id")},
        'start_line': {
            'type': 'integer',
            'description': prompts.ref("read_artifact.param.start_line"),
        },
        'end_line': {
            'type': 'integer',
            'description': prompts.ref("read_artifact.param.end_line"),
        },
    },
    required=('id',),
    settings=SLICE,
)

GREP_ARTIFACT = BrowserTool(
    ToolName.GREP_ARTIFACT,
    params={
        'id': {'type': 'string', 'description': prompts.ref("grep_artifact.param.id")},
        'pattern': {
            'type': 'string',
            'description': prompts.ref("grep_artifact.param.pattern"),
        },
        'regex': {
            'type': 'boolean',
            'description': prompts.ref("grep_artifact.param.regex"),
        },
        'ignore_case': {'type': 'boolean', 'description': prompts.ref("grep_artifact.param.ignore_case")},
        'context': {
            'type': 'integer',
            'description': prompts.ref("grep_artifact.param.context"),
        },
    },
    required=('id', 'pattern'),
    settings=GREP,
)

ARTIFACTS_FAMILY = Family(
    FamilyKey.ARTIFACTS,
    label='Artifacts',
    tools=(ToolName.SHOW_ARTIFACT, ToolName.EDIT_ARTIFACT, ToolName.READ_ARTIFACT, ToolName.GREP_ARTIFACT),
    hint='le panneau latéral',
    fixed=True,
    authored=True,
)


# ── Proposer le débogage ──────────────────────────────────────────────

REQUEST_ARTIFACT_DEBUG = BrowserTool(
    ToolName.REQUEST_ARTIFACT_DEBUG,
    params={
        'id': {'type': 'string', 'description': prompts.ref("request_artifact_debug.param.id")},
        'reason': {
            'type': 'string',
            'description': prompts.ref("request_artifact_debug.param.reason"),
        },
    },
    required=('id',),
)

ARTIFACT_DEBUG_OFFER_FAMILY = Family(
    FamilyKey.ARTIFACT_DEBUG_OFFER,
    label='Proposer le débogage',
    tools=(ToolName.REQUEST_ARTIFACT_DEBUG,),
    hint='une carte pour le rétablir',
    fixed=True,
)


# ── Déboguer un artifact ──────────────────────────────────────────────

INSPECT_ARTIFACT = BrowserTool(
    ToolName.INSPECT_ARTIFACT,
    params={
        'id': {'type': 'string', 'description': prompts.ref("inspect_artifact.param.id")},
        'screenshot': {'type': 'boolean', 'description': prompts.ref("inspect_artifact.param.screenshot")},
    },
    required=('id',),
)

RUN_IN_ARTIFACT = BrowserTool(
    ToolName.RUN_IN_ARTIFACT,
    params={
        'id': {'type': 'string', 'description': prompts.ref("run_in_artifact.param.id")},
        'code': {'type': 'string', 'description': prompts.ref("run_in_artifact.param.code")},
    },
    required=('id', 'code'),
)

ARTIFACT_DEBUG_FAMILY = Family(
    FamilyKey.ARTIFACT_DEBUG,
    label='Déboguer un artifact',
    tools=(ToolName.INSPECT_ARTIFACT, ToolName.RUN_IN_ARTIFACT),
    hint='tester la page affichée',
    fixed=True,
    authored=True,
)


# ── Dossier ouvert ────────────────────────────────────────────────────

FOLDER_LIST = BrowserTool(
    ToolName.FOLDER_LIST,
    params={
        'path': {
            'type': 'string',
            'description': prompts.ref("folder_list.param.path"),
        },
        'recursive': {'type': 'boolean', 'description': prompts.ref("folder_list.param.recursive")},
    },
)

FOLDER_READ = BrowserTool(
    ToolName.FOLDER_READ,
    params={
        'path': {'type': 'string', 'description': prompts.ref("folder_read.param.path")},
        'start_line': {'type': 'integer', 'description': prompts.ref("folder_read.param.start_line")},
        'end_line': {
            'type': 'integer',
            'description': prompts.ref("folder_read.param.end_line"),
        },
    },
    required=('path',),
)

FOLDER_SEARCH = BrowserTool(
    ToolName.FOLDER_SEARCH,
    params={
        'pattern': {
            'type': 'string',
            'description': prompts.ref("folder_search.param.pattern"),
        },
        'path': {'type': 'string', 'description': prompts.ref("folder_search.param.path")},
        'regex': {
            'type': 'boolean',
            'description': prompts.ref("folder_search.param.regex"),
        },
        'ignore_case': {'type': 'boolean', 'description': prompts.ref("folder_search.param.ignore_case")},
    },
    required=('pattern',),
)

FOLDER_WRITE = BrowserTool(
    ToolName.FOLDER_WRITE,
    params={
        'path': {'type': 'string', 'description': prompts.ref("folder_write.param.path")},
        'content': {'type': 'string', 'description': prompts.ref("folder_write.param.content")},
    },
    required=('path', 'content'),
)

FOLDER_EDIT = BrowserTool(
    ToolName.FOLDER_EDIT,
    params={
        'path': {'type': 'string'},
        'old_text': {
            'type': 'string',
            'description': prompts.ref("folder_edit.param.old_text"),
        },
        'new_text': {'type': 'string', 'description': prompts.ref("folder_edit.param.new_text")},
        'replace_all': {'type': 'boolean', 'description': prompts.ref("folder_edit.param.replace_all")},
    },
    required=('path', 'old_text', 'new_text'),
)

FOLDER_FAMILY = Family(
    FamilyKey.FOLDER,
    label='Dossier ouvert',
    tools=(ToolName.FOLDER_LIST, ToolName.FOLDER_READ, ToolName.FOLDER_SEARCH, ToolName.FOLDER_WRITE, ToolName.FOLDER_EDIT),
    hint='lire, chercher, écrire avec accord',
    fixed=True,
)


TOOLS = (ASK_USER, SAVE_FILE, SHOW_ARTIFACT, EDIT_ARTIFACT, READ_ARTIFACT, GREP_ARTIFACT, REQUEST_ARTIFACT_DEBUG, INSPECT_ARTIFACT, RUN_IN_ARTIFACT, FOLDER_LIST, FOLDER_READ, FOLDER_SEARCH, FOLDER_WRITE, FOLDER_EDIT,)
FAMILIES = (ASK_FAMILY, SAVE_FAMILY, ARTIFACTS_FAMILY, ARTIFACT_DEBUG_OFFER_FAMILY, ARTIFACT_DEBUG_FAMILY, FOLDER_FAMILY,)
