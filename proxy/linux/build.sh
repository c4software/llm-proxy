#!/usr/bin/env bash
# Construit la VM Linux du navigateur (docs/linux.md) et la pose dans
# proxy/llm_proxy/ui/web/static/vendor/linux/, où la page la trouve.
#
#   proxy/linux/build.sh                       paquets par défaut
#   LINUX_PACKAGES="bash python3 jq sqlite git" proxy/linux/build.sh
#   proxy/linux/build.sh <dossier>             ailleurs que dans vendor/linux
#
# Il faut Docker (avec BuildKit, le défaut). Ce sont les étapes
# linux-guest, linux-initrd et linux de proxy/Dockerfile : l'image du
# proxy construit la même VM, ce script n'en est que le raccourci pour une
# machine de développement. Les autres runtimes de la page : `npm ci &&
# npm run build` dans proxy/wasm.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${1:-$ROOT/proxy/llm_proxy/ui/web/static/vendor/linux}
ARGS=()
[ -n "${LINUX_PACKAGES:-}" ] && ARGS+=(--build-arg "LINUX_PACKAGES=$LINUX_PACKAGES")

mkdir -p "$OUT"
docker build -f "$ROOT/proxy/Dockerfile" --target linux-vendor "${ARGS[@]}" \
  --output "type=local,dest=$OUT" "$ROOT"
echo "VM Linux → $OUT"
grep -E '"(v86|memoryMB|bootMs|restoreMs)"' "$OUT/manifest.json" | tr -d ' ,' | paste -sd ' '
du -h "$OUT/state.bin.zst" | cut -f1 | sed 's/^/état : /'
