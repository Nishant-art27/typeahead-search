#!/usr/bin/env bash
# Start the Search Typeahead app. Creates a venv + installs deps on first run,
# generates the dataset if missing, then launches the FastAPI server.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "[run] Creating virtualenv..."
  "$PYTHON" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "[run] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# Generate the dataset up-front (the server also does this lazily on first run).
if [ ! -f "data/queries.csv" ]; then
  echo "[run] Generating dataset..."
  python scripts/generate_dataset.py --rows "${DATASET_MIN_ROWS:-120000}" --out data/queries.csv
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
echo "[run] Starting server on http://$HOST:$PORT"
exec uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
