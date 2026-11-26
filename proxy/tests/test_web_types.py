"""Le code de la page (web/static), vérifié par l'outillage de
proxy/wasm : `npm run check` — ses types (vue-tsc, les fichiers en
`// @ts-check`), l'exécution des outils de la page contre leur interface
(check-tools.mjs), les tests de sa logique (Vitest, proxy/wasm/tests) et
son build (Vite, qui compile chaque gabarit) — ; et l'exécution des outils
contre leurs déclarations, celles du proxy (check-tools.mjs
--declarations). Ignoré sans Node ou sans `npm ci` dans proxy/wasm."""

import json
import os
import shutil
import subprocess

import pytest

from llm_proxy.ui import page_tools

WASM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wasm")


def node():
    path = shutil.which("node")
    if not path or not os.path.isdir(os.path.join(WASM, "node_modules", "vite")):
        pytest.skip("Node, ou l'outillage de la page (npm ci dans proxy/wasm), absent")
    return path


def test_page_checks_pass():
    """Types, outils, tests et build de la page : `npm run check`."""
    node()
    r = subprocess.run([shutil.which("npm") or "npm", "run", "check"], cwd=WASM,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


def test_chaque_outil_de_la_page_a_sa_declaration_et_son_execution(tmp_path):
    """L'exécution (tools/index.js) : chaque outil hérite de PageTool et
    n'a que ses membres, chaque nom de TOOL et clé de FAMILY est
    implémenté une fois ; et chaque déclaration du proxy (tools/page/) a
    son exécution, chaque exécution native sa déclaration."""
    declared = tmp_path / "declarations.json"
    declared.write_text(json.dumps(page_tools.declarations()), encoding="utf-8")
    r = subprocess.run([node(), "check-tools.mjs", "--declarations", str(declared)], cwd=WASM,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "déclarations et exécution conformes" in r.stdout
