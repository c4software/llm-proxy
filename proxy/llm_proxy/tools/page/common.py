"""Ce que partagent les déclarations des outils du navigateur."""

# Lignes d'une tranche lue (read_artifact, shell_read), par défaut et au
# plus ; lignes de contexte d'une recherche (grep_artifact, shell_grep),
# par défaut et au plus. Le prompt les dit, la page les applique (settings).
SLICE_LINES = 80
MAX_SLICE = 400
CONTEXT = 2
MAX_CONTEXT = 10
SLICE = {'slice_lines': SLICE_LINES, 'max_slice': MAX_SLICE}
GREP = {'context': CONTEXT, 'max_context': MAX_CONTEXT}

# Le paramètre `reason` d'une carte : pourquoi, montré à l'utilisateur.
REASON = {"type": "string", "description": "Pourquoi, en une phrase, montrée à l'utilisateur."}
