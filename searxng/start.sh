#!/usr/bin/env sh
# SearXNG indítása + grafikus kezelő (Linux/macOS). Ha nincs telepítve, telepíti.
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9+ nem található. Telepítés: sudo apt install python3 python3-venv  (Fedora: sudo dnf install python3)"
  exit 1
fi
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  echo "Megjegyzés: a natív módhoz kell a venv modul (sudo apt install python3-venv). Docker módban nem szükséges."
fi
exec "$PY" manager.py gui --autostart "$@"
