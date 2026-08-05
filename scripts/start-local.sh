#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$project_dir"

if [ -f .env.local ]; then
  exec "$script_dir/docker-cli.sh" compose --env-file .env.local up --build
fi
exec "$script_dir/docker-cli.sh" compose up --build
