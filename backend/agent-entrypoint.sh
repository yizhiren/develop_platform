#!/bin/sh
set -eu

if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  key_file=/tmp/forgeflow-model-key
  umask 077
  printf '%s' "$DEEPSEEK_API_KEY" > "$key_file"
  unset DEEPSEEK_API_KEY
  export DEEPSEEK_API_KEY_FILE="$key_file"
fi

exec env -u DEEPSEEK_API_KEY "$@"
