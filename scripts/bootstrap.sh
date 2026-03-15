#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3 first."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating venv in .venv/"
  python3 -m venv .venv
fi

echo "Activating venv"
source .venv/bin/activate

echo "Upgrading pip"
python -m pip install --upgrade pip

if [ -f "requirements.txt" ]; then
  echo "Installing from requirements.txt"
  pip install -r requirements.txt
else
  echo "requirements.txt not found. Installing baseline deps."
  pip install \
    google-api-python-client \
    google-auth \
    google-auth-oauthlib \
    python-dateutil \
    requests \
    python-dotenv
  echo "Writing requirements.txt"
  pip freeze > requirements.txt
fi

echo ""
echo "Done."
echo "Next:"
echo "  source .venv/bin/activate"
echo "  make doctor"
echo "  make dryrun"
