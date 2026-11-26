"""
Les outils du NAVIGATEUR — les bacs de code : JavaScript et TypeScript, PHP,
SQL, PostgreSQL, Linux, programmes d'outils.
Leur DÉCLARATION (Runs.BROWSER) : le prompt, le schéma, la famille du
menu Outils. Leur exécution est dans la page, chat/tools/sandbox/
(docs/navigateur.md, « Écrire un outil de la page »).
"""

from ... import prompts
from ..contract import BrowserTool, Family, FamilyKey, ToolName
from .common import GREP, SLICE

# Délais (s) d'un appel, que le prompt dit et que la page applique.
JAVASCRIPT_TIMEOUT = 15
TYPESCRIPT_TIMEOUT = 60      # le premier appel charge le compilateur
PHP_TIMEOUT = 30             # le premier appel compile le runtime
SHELL_TIMEOUT = 60           # run_shell, par défaut
SHELL_MAX_TIMEOUT = 600
PROGRAM_TIMEOUT = 120
MAX_ROWS = 200               # lignes montrées par résultat (SQL, PostgreSQL)
MAX_CALLS = 20               # appels d'outils par programme
CONCURRENCY = 4              # … à la fois


# ── JavaScript, TypeScript ────────────────────────────────────────────

RUN_JAVASCRIPT = BrowserTool(
    ToolName.RUN_JAVASCRIPT,
    params={
        'code': {'type': 'string', 'description': prompts.ref("run_javascript.param.code")},
        'language': {
            'type': 'string',
            'enum': ['javascript', 'typescript'],
            'description': prompts.ref("run_javascript.param.language"),
        },
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': prompts.ref("run_javascript.param.files"),
        },
    },
    required=('code',),
    settings={'timeout': JAVASCRIPT_TIMEOUT},
)

CHECK_TYPESCRIPT = BrowserTool(
    ToolName.CHECK_TYPESCRIPT,
    params={
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': prompts.ref("check_typescript.param.files"),
        },
        'code': {
            'type': 'string',
            'description': prompts.ref("check_typescript.param.code"),
        },
        'path': {
            'type': 'string',
            'description': prompts.ref("check_typescript.param.path"),
        },
    },
    settings={'timeout': TYPESCRIPT_TIMEOUT},
)

JAVASCRIPT_FAMILY = Family(
    FamilyKey.JAVASCRIPT,
    label='JavaScript, TypeScript',
    tools=(ToolName.RUN_JAVASCRIPT, ToolName.CHECK_TYPESCRIPT),
    hint='du code dans un bac isolé, sans réseau : fichiers, graphiques ; vérifier les types',
    authored=True,
)


# ── PHP ───────────────────────────────────────────────────────────────

RUN_PHP = BrowserTool(
    ToolName.RUN_PHP,
    params={
        'code': {'type': 'string', 'description': prompts.ref("run_php.param.code")},
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': prompts.ref("run_php.param.files"),
        },
    },
    required=('code',),
    settings={'timeout': PHP_TIMEOUT},
)

PHP_FAMILY = Family(
    FamilyKey.PHP,
    label='PHP',
    tools=(ToolName.RUN_PHP,),
    hint='PHP 8.3 en WASM, dans un bac isolé (≈ 18 Mo au premier appel)',
    authored=True,
)


# ── SQL ───────────────────────────────────────────────────────────────

RUN_SQL = BrowserTool(
    ToolName.RUN_SQL,
    params={
        'sql': {
            'type': 'string',
            'description': prompts.ref("run_sql.param.sql"),
        },
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': prompts.ref("run_sql.param.files"),
        },
        'reset': {
            'type': 'boolean',
            'description': prompts.ref("run_sql.param.reset"),
        },
        'download': {
            'type': 'boolean',
            'description': prompts.ref("run_sql.param.download"),
        },
    },
    settings={'max_rows': MAX_ROWS},
)

SQL_FAMILY = Family(
    FamilyKey.SQL,
    label='SQL',
    tools=(ToolName.RUN_SQL,),
    hint='SQLite en WASM : une base par conversation, import de CSV, JSON, .sqlite',
    authored=True,
)


# ── PostgreSQL ────────────────────────────────────────────────────────

RUN_POSTGRES = BrowserTool(
    ToolName.RUN_POSTGRES,
    params={
        'sql': {
            'type': 'string',
            'description': prompts.ref("run_postgres.param.sql"),
        },
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': prompts.ref("run_postgres.param.files"),
        },
        'reset': {
            'type': 'boolean',
            'description': prompts.ref("run_postgres.param.reset"),
        },
    },
    settings={'max_rows': MAX_ROWS},
)

POSTGRES_FAMILY = Family(
    FamilyKey.POSTGRES,
    label='PostgreSQL',
    tools=(ToolName.RUN_POSTGRES,),
    hint=('PostgreSQL en WASM (PGlite) : une base par conversation, import de CSV, JSON, .sql '
          '(≈ 17 Mo au premier appel)'),
    authored=True,
)


# ── Linux ─────────────────────────────────────────────────────────────

RUN_SHELL = BrowserTool(
    ToolName.RUN_SHELL,
    params={
        'script': {'type': 'string', 'description': prompts.ref("run_shell.param.script")},
        'files': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': prompts.ref("run_shell.param.files"),
        },
        'timeout': {
            'type': 'number',
            'description': prompts.ref("run_shell.param.timeout"),
        },
        'reset': {
            'type': 'boolean',
            'description': prompts.ref("run_shell.param.reset"),
        },
        'terminal': {
            'type': 'boolean',
            'description': prompts.ref("run_shell.param.terminal"),
        },
    },
    required=('script',),
    settings={'timeout': SHELL_TIMEOUT, 'max_timeout': SHELL_MAX_TIMEOUT},
)

SHELL_READ = BrowserTool(
    ToolName.SHELL_READ,
    params={
        'path': {'type': 'string', 'description': prompts.ref("shell_read.param.path")},
        'start_line': {
            'type': 'integer',
            'description': prompts.ref("shell_read.param.start_line"),
        },
        'end_line': {
            'type': 'integer',
            'description': prompts.ref("shell_read.param.end_line"),
        },
    },
    required=('path',),
    settings=SLICE,
)

SHELL_GREP = BrowserTool(
    ToolName.SHELL_GREP,
    params={
        'pattern': {
            'type': 'string',
            'description': prompts.ref("shell_grep.param.pattern"),
        },
        'path': {
            'type': 'string',
            'description': prompts.ref("shell_grep.param.path"),
        },
        'regex': {
            'type': 'boolean',
            'description': prompts.ref("shell_grep.param.regex"),
        },
        'ignore_case': {'type': 'boolean', 'description': prompts.ref("shell_grep.param.ignore_case")},
        'context': {
            'type': 'integer',
            'description': prompts.ref("shell_grep.param.context"),
        },
    },
    required=('pattern',),
    settings=GREP,
)

SHELL_EDIT = BrowserTool(
    ToolName.SHELL_EDIT,
    params={
        'path': {'type': 'string', 'description': prompts.ref("shell_edit.param.path")},
        'edits': {
            'type': 'array',
            'minItems': 1,
            'items': {
                'type': 'object',
                'required': ['old_string', 'new_string'],
                'properties': {
                    'old_string': {'type': 'string', 'description': prompts.ref("shell_edit.param.edits.old_string")},
                    'new_string': {
                        'type': 'string',
                        'description': prompts.ref("shell_edit.param.edits.new_string"),
                    },
                    'replace_all': {
                        'type': 'boolean',
                        'description': prompts.ref("shell_edit.param.edits.replace_all"),
                    },
                    'regex': {
                        'type': 'boolean',
                        'description': prompts.ref("shell_edit.param.edits.regex"),
                    },
                    'ignore_case': {
                        'type': 'boolean',
                        'description': prompts.ref("shell_edit.param.edits.ignore_case"),
                    },
                },
            },
        },
    },
    required=('path', 'edits'),
)

SHELL_WRITE = BrowserTool(
    ToolName.SHELL_WRITE,
    params={
        'path': {'type': 'string', 'description': prompts.ref("shell_write.param.path")},
        'content': {'type': 'string', 'description': prompts.ref("shell_write.param.content")},
    },
    required=('path', 'content'),
)

LINUX_FAMILY = Family(
    FamilyKey.LINUX,
    label='Linux',
    tools=(ToolName.RUN_SHELL, ToolName.SHELL_READ, ToolName.SHELL_GREP, ToolName.SHELL_EDIT, ToolName.SHELL_WRITE),
    hint=('Une VM Linux en WASM (Alpine : bash, python3, git, outils GNU…), sans réseau, '
          'partagée avec le panneau Terminal'),
    authored=True,
)


# ── Programmes d'outils ───────────────────────────────────────────────

RUN_TOOLS_PROGRAM = BrowserTool(
    ToolName.RUN_TOOLS_PROGRAM,
    params={'code': {'type': 'string', 'description': prompts.ref("run_tools_program.param.code")}},
    required=('code',),
    settings={'max_calls': MAX_CALLS, 'concurrency': CONCURRENCY, 'timeout': PROGRAM_TIMEOUT},
)

PROGRAMS_FAMILY = Family(
    FamilyKey.PROGRAMS,
    label="Programmes d'outils",
    tools=(ToolName.RUN_TOOLS_PROGRAM,),
    hint='enchaîner les outils hébergés cochés dans un programme JavaScript, en une étape',
    authored=True,
)


TOOLS = (RUN_JAVASCRIPT, CHECK_TYPESCRIPT, RUN_PHP, RUN_SQL, RUN_POSTGRES, RUN_SHELL, SHELL_READ, SHELL_GREP, SHELL_EDIT, SHELL_WRITE, RUN_TOOLS_PROGRAM,)
FAMILIES = (JAVASCRIPT_FAMILY, PHP_FAMILY, SQL_FAMILY, POSTGRES_FAMILY, LINUX_FAMILY, PROGRAMS_FAMILY,)
