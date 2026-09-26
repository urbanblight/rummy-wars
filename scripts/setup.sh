#!/usr/bin/env bash
fail() {
  printf 'Setup failed: %s\n' "$1" >&2
  exit 1
}

python3 -m venv .venv/ || fail "creating virtual environment"
source .venv/bin/activate || fail "activating virtual environment"
python3 -m pip install --upgrade pip || fail "upgrading pip"
pip install -r requirements/requirements.txt || fail "installing requirements"
pip install -r requirements/requirements-dev.txt || fail "installing development requirements"
cd docs
make html || fail "building documentation"
cd ..
python3 -m  playwright install --with-deps chromium || fail "installing Playwright with dependencies"
