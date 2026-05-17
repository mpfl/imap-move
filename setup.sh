#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m venv .venv
# shellcheck source=/dev/null
source .venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

echo "Done. To activate the venv: source .venv/bin/activate"
