#!/bin/sh
set -eu

umask 077

materialize_secret() {
  source_name="$1"
  file_name="$2"
  target_path="$3"
  secret_value="$(printenv "$source_name" 2>/dev/null || true)"
  if [ -n "$secret_value" ]; then
    printf '%s' "$secret_value" > "$target_path"
    export "${file_name}=${target_path}"
  fi
}

materialize_secret DEEPSEEK_API_KEY DEEPSEEK_API_KEY_FILE /tmp/huaban-model-key-default
materialize_secret AGENT1_LLM_API_KEY AGENT1_LLM_API_KEY_FILE /tmp/huaban-model-key-agent1
materialize_secret AGENT2_LLM_API_KEY AGENT2_LLM_API_KEY_FILE /tmp/huaban-model-key-agent2
materialize_secret AGENT3_LLM_API_KEY AGENT3_LLM_API_KEY_FILE /tmp/huaban-model-key-agent3
materialize_secret AGENT4_LLM_API_KEY AGENT4_LLM_API_KEY_FILE /tmp/huaban-model-key-agent4

unset source_name file_name target_path secret_value

exec env \
  -u DEEPSEEK_API_KEY \
  -u AGENT1_LLM_API_KEY \
  -u AGENT2_LLM_API_KEY \
  -u AGENT3_LLM_API_KEY \
  -u AGENT4_LLM_API_KEY \
  "$@"
