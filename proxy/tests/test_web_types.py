"""Les types du code de la page (web/static) : les fichiers qui
commencent par `// @ts-check`, vérifiés par TypeScript selon
proxy/wasm/tsconfig.json (npm run check). Ignoré sans Node ou sans
`npm ci` dans proxy/wasm."""

import os
import shutil
import subprocess

import pytest

WASM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wasm")
TSC = os.path.join(WASM, "node_modules", "typescript", "bin", "tsc")


def test_page_code_type_checks():
    node = shutil.which("node")
    if not node or not os.path.isfile(TSC):
        pytest.skip("Node, ou TypeScript (npm ci dans proxy/wasm), absent")
    r = subprocess.run([node, TSC, "-p", "tsconfig.json"], cwd=WASM,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
