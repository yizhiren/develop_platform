#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)

# This repository keeps local Compose interpolation values in .env.local.
# Make the project wrapper safe by default while preserving an explicitly
# supplied --env-file value.
if [ "${1:-}" = "compose" ] && [ -f "$project_dir/.env.local" ]; then
  compose_env_explicit=0
  for argument in "$@"; do
    case "$argument" in
      --env-file|--env-file=*) compose_env_explicit=1 ;;
    esac
  done
  if [ "$compose_env_explicit" -eq 0 ]; then
    shift
    set -- compose --env-file "$project_dir/.env.local" "$@"
  fi
fi

if command -v docker >/dev/null 2>&1; then
  exec docker "$@"
fi

docker_app_cli="/Applications/Docker.app/Contents/Resources/bin/docker"
if [ -x "$docker_app_cli" ]; then
  exec "$docker_app_cli" "$@"
fi

echo "Docker CLI 未找到，请先安装并启动 Docker Desktop。" >&2
exit 1
