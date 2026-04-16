#!/usr/bin/env bash
# Launch the Transact Access Manager on macOS/Linux
# Usage: ./run.sh

set -e

cd "$(dirname "$0")"

# Prefer python3, fall back to python
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10+ is required. Install it and try again."
    exit 1
fi

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv venv
fi

# Activate
# shellcheck disable=SC1091
source venv/bin/activate

echo "Checking dependencies..."
pip install -q -r requirements_transact.txt

echo "Starting Transact Access Manager..."
python transact_access_manager.py
