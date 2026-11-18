"""Le code de la page (web/static), vérifié par l'outillage de
proxy/wasm (npm run check) : ses types (les fichiers en `// @ts-check`,
selon proxy/wasm/tsconfig.json), le registre des outils de la page
contre leur interface (proxy/wasm/check-tools.mjs), et les tests de sa
logique pure (proxy/wasm/tests, `node --test`). Ignoré sans Node ou
sans `npm ci` dans proxy/wasm (les tests Node, sans Node seulement)."""

import os
import shutil
import subprocess

import pytest

WASM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wasm")
TSC = os.path.join(WASM, "node_modules", "typescript", "bin", "tsc")


def node():
    path = shutil.which("node")
    if not path or not os.path.isfile(TSC):
        pytest.skip("Node, ou TypeScript (npm ci dans proxy/wasm), absent")
    return path


def test_page_code_type_checks():
    r = subprocess.run([node(), TSC, "-p", "tsconfig.json"], cwd=WASM,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_page_tools_follow_their_interface():
    """Chaque outil hérite de PageTool et n'a que ses membres ; chaque nom
    de TOOL et clé de FAMILY est implémenté une fois (tools/index.js)."""
    r = subprocess.run([node(), "check-tools.mjs"], cwd=WASM,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "conformes" in r.stdout


def test_page_logic_tests_pass():
    """Les tests Node de la page (proxy/wasm/tests) : rendu Markdown sûr,
    document d'un artifact, enregistrement des conversations…"""
    path = shutil.which("node")
    if not path:
        pytest.skip("Node absent")
    r = subprocess.run([path, "--test", "tests/"], cwd=WASM,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
