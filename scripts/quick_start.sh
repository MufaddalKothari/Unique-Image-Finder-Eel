#!/usr/bin/env bash
# Create venv, install requirements, run the app (Linux / macOS). Run from anywhere.
set -e
cd "$(dirname "$0")/.."
[ -d unq_img ] || python3 -m venv unq_img
source unq_img/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python main.py "$@"
