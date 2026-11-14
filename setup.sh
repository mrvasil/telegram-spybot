#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if ! command -v uv >/dev/null 2>&1; then
  echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi
uv sync --frozen
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
printf '%s\n' 'Dependencies installed. Configure .env, create a separate key with uv run spybot init, then uv run spybot run. No bot was started.'
