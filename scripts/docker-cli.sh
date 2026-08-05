#!/usr/bin/env sh
set -eu

if command -v docker >/dev/null 2>&1; then
  exec docker "$@"
fi

docker_app_cli="/Applications/Docker.app/Contents/Resources/bin/docker"
if [ -x "$docker_app_cli" ]; then
  exec "$docker_app_cli" "$@"
fi

echo "Docker CLI 未找到，请先安装并启动 Docker Desktop。" >&2
exit 1
