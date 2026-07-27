#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repository_root"

for tool in shellcheck actionlint hadolint; do
  if [ ! -x ".tools/bin/$tool" ]; then
    echo "Missing .tools/bin/$tool; run scripts/bootstrap-analysis-tools.sh" >&2
    exit 1
  fi
done

if [ ! -x .venv/bin/python ]; then
  echo "Missing .venv; create it and install -e '.[dev]'" >&2
  exit 1
fi
if [ ! -d node_modules ]; then
  echo "Missing node_modules; run npm ci" >&2
  exit 1
fi

.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/bandit -q -c pyproject.toml -r src/stowarr
.venv/bin/pip-audit --strict .
.venv/bin/python scripts/check-mutation-boundaries.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile src/stowarr/*.py

npm run lint
npm run audit:dependencies

.tools/bin/shellcheck docker/entrypoint.sh scripts/*.sh
.tools/bin/actionlint -shellcheck=.tools/bin/shellcheck
.tools/bin/hadolint Dockerfile Dockerfile.web

.venv/bin/python -m json.tool config/config.example.json >/dev/null
docker compose config --quiet
docker compose build
docker run --rm --add-host stowarr-api:127.0.0.1 \
  ghcr.io/slashmad/stowarr-web:latest nginx -t
